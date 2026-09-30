"""SDK 决策日志核对：按配置快照的激活时间窗分类节点事件，并重算决策比对。

语义概述：

- 每个配置快照的摘要沿用制品的规范化 SHA-256（app.artifacts.config_digest），
  由服务端重算，节点事件以 digest 指认自己使用的配置版本；
- 快照按 activated_at 升序排列后，第 i 版摘要的允许区间为
  [activated_at[i], activated_at[i+1] + grace_seconds)：新版自激活起生效，
  旧版合法上报延续到“下一版激活 + 宽限”；最后一版的上界为 +∞；
- 事件时间按 clock_skew_seconds 扩为 ±偏差闭区间 [t−s, t+s] 后与允许区间比较：
  完全落入 → consistent；相交但未完全落入 → ambiguous_time；
  整体在允许区间之前 → early_version；之后 → stale_version；
  所报摘要不在任何快照中 → unknown_digest（不参与后续重算）；
- 可识别摘要的事件复用评估器在对应快照配置上重算
  variant_id / served_default / reason，任一不符即 decision_mismatch=true；
- 事件全局按 event_id 去重（同 ID 内容一致只计一次，冲突在模型层即 422）；
- 结果按 (node_id, timestamp, event_id) 升序，并按节点汇总各分类计数，
  输出顺序与事件提交顺序无关。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.artifacts import config_digest
from app.evaluator import Evaluator
from app.models import (
    ConfigSnapshot,
    NodeDecisionEvent,
    NodeReconcileSummary,
    ReconcileClassification,
    ReconcileRequest,
    ReconcileResponse,
    ReconcileResult,
    RecomputedDecision,
)


class ReconcileRequestError(Exception):
    """请求与快照不一致（如快照摘要重复、事件引用快照中不存在的开关）。"""


def _classify(
    timestamp: datetime,
    skew: timedelta,
    start: datetime,
    end: datetime | None,
) -> ReconcileClassification:
    """把 ±skew 扩界后的事件区间与允许区间 [start, end) 比较（end=None 表示 +∞）。"""
    interval_lo = timestamp - skew
    interval_hi = timestamp + skew
    if interval_lo >= start and (end is None or interval_hi < end):
        return ReconcileClassification.CONSISTENT
    if interval_hi < start:
        return ReconcileClassification.EARLY_VERSION
    if end is not None and interval_lo >= end:
        return ReconcileClassification.STALE_VERSION
    return ReconcileClassification.AMBIGUOUS_TIME


def _dedupe(events: list[NodeDecisionEvent]) -> list[NodeDecisionEvent]:
    """全局按 event_id 去重（同 ID 内容一致，冲突已在模型层拒绝）。"""
    return list({event.event_id: event for event in events}.values())


def reconcile(request: ReconcileRequest) -> ReconcileResponse:
    """对整批节点决策事件做时间分类与决策重算比对，并按节点汇总。"""
    snapshots = sorted(request.snapshots, key=lambda snap: snap.activated_at)
    grace = timedelta(seconds=request.grace_seconds)
    skew = timedelta(seconds=request.clock_skew_seconds)

    # 摘要 -> (允许区间起点, 终点)；终点 None 表示 +∞（最后一版）。
    windows: dict[str, tuple[datetime, datetime | None]] = {}
    configs: dict[str, ConfigSnapshot] = {}
    for index, snapshot in enumerate(snapshots):
        digest = config_digest(snapshot.config.model_dump(mode="json"))
        if digest in windows:
            # 同一配置再次激活（回滚）会让“所报摘要的允许区间”不唯一，拒绝。
            raise ReconcileRequestError(f"配置快照摘要重复: '{digest}'")
        end = (
            snapshots[index + 1].activated_at + grace
            if index + 1 < len(snapshots)
            else None
        )
        windows[digest] = (snapshot.activated_at, end)
        configs[digest] = snapshot

    events = _dedupe(request.events)

    evaluators = {
        digest: Evaluator(snapshot.config) for digest, snapshot in configs.items()
    }
    switch_ids = {
        digest: {sw.id for sw in snapshot.config.switches}
        for digest, snapshot in configs.items()
    }
    # 可识别摘要的事件必须引用对应快照配置中存在的开关，否则无法重算（422）。
    for event in events:
        known = switch_ids.get(event.digest)
        if known is not None and event.switch_id not in known:
            raise ReconcileRequestError(
                f"事件 '{event.event_id}' 引用的开关 '{event.switch_id}' "
                f"在其所报摘要 '{event.digest}' 的配置快照中不存在"
            )

    results: list[ReconcileResult] = []
    for event in events:
        window = windows.get(event.digest)
        if window is None:
            results.append(
                ReconcileResult(
                    event_id=event.event_id,
                    node_id=event.node_id,
                    timestamp=event.timestamp,
                    digest=event.digest,
                    classification=ReconcileClassification.UNKNOWN_DIGEST,
                )
            )
            continue

        classification = _classify(event.timestamp, skew, window[0], window[1])
        outcome = evaluators[event.digest].evaluate(
            switch_id=event.switch_id,
            user_id=event.user_id,
            context=event.context,
        )
        mismatch = (
            outcome.variant_id != event.variant_id
            or outcome.served_default != event.served_default
            or outcome.reason != event.reason
        )
        results.append(
            ReconcileResult(
                event_id=event.event_id,
                node_id=event.node_id,
                timestamp=event.timestamp,
                digest=event.digest,
                classification=classification,
                decision_mismatch=mismatch,
                recomputed=RecomputedDecision(
                    variant_id=outcome.variant_id,
                    served_default=outcome.served_default,
                    reason=outcome.reason,
                ),
            )
        )

    # 固定输出顺序，与事件提交顺序无关。
    results.sort(key=lambda item: (item.node_id, item.timestamp, item.event_id))

    summaries: dict[str, NodeReconcileSummary] = {}
    for item in results:
        summary = summaries.get(item.node_id)
        if summary is None:
            summary = NodeReconcileSummary(node_id=item.node_id, events=0)
            summaries[item.node_id] = summary
        summary.events += 1
        # 分类枚举值与计数字段同名，直接按名累加。
        setattr(
            summary,
            item.classification.value,
            getattr(summary, item.classification.value) + 1,
        )
        if item.decision_mismatch:
            summary.decision_mismatches += 1

    return ReconcileResponse(
        event_count=len(results),
        results=results,
        nodes=[summaries[node_id] for node_id in sorted(summaries)],
    )
