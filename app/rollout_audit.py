"""SDK 分波铺开执行审计：把执行事件按 sequence 折叠重放，逐波独立复用序贯护栏。

语义概述：

- 请求携带 status=planned 的原计划（/sdk/rollout-plan 的输出）、护栏指标
  声明（参数同 /guardrails/evaluate）与 wave_started / node_completed /
  metric_batch 事件；事件可乱序提交，按 sequence 升序折叠重放，同 event_id
  同内容去重（内容冲突、sequence 重复在模型层即 422）；
- 序贯约束（遇到首个违规即停止，返回违规事件与事前状态，违规事件不落状态）：
    - wave_started 必须严格按波次顺序：上一波 promote 前启动下一波即
      wave_skipped（hold 时越波），重复启动已启动波为 wave_already_started；
    - node_completed 只能发生在所属波启动之后（node_before_wave_start），
      同一节点重复完成为 node_already_completed；
    - metric_batch 只能在所属波启动、且该波全部节点完成后接收
      （metrics_before_wave_start / metrics_before_nodes_complete）；
    - 每波每指标的 batch 必须从 1 起逐批递增（metric_batch_gap）；指标一旦
      首次越界即冻结，冻结后再到批次为 metric_frozen；
    - 任一事件发生在某波护栏判定 rollback 之后均为 event_after_rollback
      （rollback 后再执行均违规，含同波后续批次与越波启动）；
- 护栏累计各波彼此独立：每波的指标从空批次开始，复用 app.guardrails 的
  Wald SPRT 计算（evaluate_metric）与总决策汇总（任一 rollback → rollback、
  全部 pass → promote、其余 hold）；promote 后才允许启动下一波；
- 合法日志返回各波完成节点、护栏路径与总状态 running / completed /
  rollback_required。
"""

from __future__ import annotations

import math

from app.guardrails import evaluate_metric
from app.models import (
    GuardrailBatch,
    GuardrailDecision,
    GuardrailMetric,
    GuardrailMetricResult,
    OverallDecision,
    RolloutAuditEvent,
    RolloutAuditEventType,
    RolloutAuditRequest,
    RolloutAuditResponse,
    RolloutAuditState,
    RolloutAuditStatus,
    RolloutGuardrailSpec,
    RolloutViolation,
    RolloutViolationKind,
    RolloutWaveAudit,
    RolloutWaveStatus,
)


def _initial_metric(spec: RolloutGuardrailSpec) -> GuardrailMetricResult:
    """波次尚未收到任何批次时的零状态指标视图（阈值仍照常给出）。"""
    p0 = spec.baseline_rate
    p1 = p0 + spec.max_increase
    return GuardrailMetricResult(
        id=spec.id,
        decision=GuardrailDecision.HOLD,
        stop_batch=None,
        failures=0,
        total=0,
        p0=p0,
        p1=p1,
        lower_threshold=math.log(spec.beta / (1 - spec.alpha)),
        upper_threshold=math.log((1 - spec.beta) / spec.alpha),
        llr=0.0,
        path=[],
    )


class _WaveRuntime:
    """单个波次在重放过程中的可变状态。"""

    def __init__(self, node_ids: list[str]) -> None:
        self.node_ids = set(node_ids)
        self.started = False
        self.completed: set[str] = set()
        # metric_id -> 已接收批次（递增校验前的原始批次列表）。
        self.batches: dict[str, list[GuardrailBatch]] = {}

    def all_nodes_completed(self) -> bool:
        return self.started and self.completed == self.node_ids


def _metric_results(
    runtime: _WaveRuntime, specs: list[RolloutGuardrailSpec]
) -> list[GuardrailMetricResult]:
    """复用 evaluate_metric 计算本波各指标的独立累计判定与路径。

    尚无任何批次的指标返回零状态视图（GuardrailMetric 至少要求 1 批）。
    """
    results: list[GuardrailMetricResult] = []
    for spec in specs:
        batches = runtime.batches.get(spec.id, [])
        if not batches:
            results.append(_initial_metric(spec))
            continue
        metric = GuardrailMetric(
            id=spec.id,
            baseline_rate=spec.baseline_rate,
            max_increase=spec.max_increase,
            alpha=spec.alpha,
            beta=spec.beta,
            batches=batches,
        )
        results.append(evaluate_metric(metric))
    return results


def _overall_decision(results: list[GuardrailMetricResult]) -> OverallDecision | None:
    """与 /guardrails/evaluate 同一汇总规则；本波未收任何批次时为 None。"""
    if not results or all(not result.path for result in results):
        return None
    if any(result.decision == GuardrailDecision.ROLLBACK for result in results):
        return OverallDecision.ROLLBACK
    if all(result.decision == GuardrailDecision.PASS for result in results):
        return OverallDecision.PROMOTE
    return OverallDecision.HOLD


class _AuditEngine:
    """按 sequence 折叠事件流并维护各波执行状态。"""

    def __init__(self, request: RolloutAuditRequest) -> None:
        self.request = request
        self.specs = list(request.guardrails)
        self.runtimes = [
            _WaveRuntime(wave.node_ids) for wave in request.plan.waves
        ]
        self.replayed = 0
        # 首个判定 rollback 的波次；一旦出现，后续任何事件均违规。
        self.rolled_back_wave: int | None = None

    def violation(
        self, kind: RolloutViolationKind, message: str, event: RolloutAuditEvent
    ) -> RolloutAuditResponse:
        return RolloutAuditResponse(
            valid=False,
            state=self.snapshot(),
            violation=RolloutViolation(kind=kind, message=message, event=event),
        )

    def snapshot(self) -> RolloutAuditState:
        """构造当前状态视图：各波阶段、完成节点与护栏累计。"""
        wave_views: list[RolloutWaveAudit] = []
        current_wave: int | None = None
        completed = set(self.request.plan.up_to_date_nodes)
        # 0 波计划（无待更新节点）初始即 completed；任何 pending 波都会改回 False。
        all_done = True

        total_batches = 0

        for index, runtime in enumerate(self.runtimes, start=1):
            if runtime.started:
                current_wave = index
                results = _metric_results(runtime, self.specs)
                decision = _overall_decision(results)
                batch_count = max(
                    (len(runtime.batches.get(spec.id, [])) for spec in self.specs),
                    default=0,
                )

                if decision == OverallDecision.ROLLBACK:
                    status = RolloutWaveStatus.ROLLBACK_REQUIRED
                elif decision == OverallDecision.PROMOTE:
                    status = RolloutWaveStatus.PROMOTED
                elif runtime.all_nodes_completed():
                    status = RolloutWaveStatus.AWAITING_METRICS
                else:
                    status = RolloutWaveStatus.RUNNING
                metrics = results
                completed_nodes = sorted(runtime.completed)
                completed.update(runtime.completed)
                if (
                    decision != OverallDecision.PROMOTE
                    or not runtime.all_nodes_completed()
                ):
                    all_done = False
            else:
                status = RolloutWaveStatus.PENDING
                decision = None
                batch_count = 0
                metrics = []
                completed_nodes = []
                all_done = False

            wave_views.append(
                RolloutWaveAudit(
                    wave=index,
                    status=status,
                    started=runtime.started,
                    completed_node_ids=completed_nodes,
                    batch_count=batch_count,
                    decision=decision,
                    metrics=metrics,
                )
            )

        if self.rolled_back_wave is not None:
            overall = RolloutAuditStatus.ROLLBACK_REQUIRED
        elif all_done:
            overall = RolloutAuditStatus.COMPLETED
        else:
            overall = RolloutAuditStatus.RUNNING

        return RolloutAuditState(
            status=overall,
            current_wave=current_wave,
            all_completed_node_ids=sorted(completed),
            replayed_event_count=self.replayed,
            waves=wave_views,
        )

    def replay(self) -> RolloutAuditResponse:
        # 按 event_id 去重后按 sequence 升序折叠。
        events = sorted(
            self.request._deduped().values(), key=lambda event: event.sequence
        )
        for event in events:
            response = self._apply(event)
            if response is not None:
                return response
            self.replayed += 1
        return RolloutAuditResponse(valid=True, state=self.snapshot())

    def _apply(self, event: RolloutAuditEvent) -> RolloutAuditResponse | None:
        """处理单条事件；返回违规响应即停止，None 表示合法已落状态。"""
        if self.rolled_back_wave is not None:
            return self.violation(
                RolloutViolationKind.EVENT_AFTER_ROLLBACK,
                f"第 {self.rolled_back_wave} 波护栏已判定 rollback，"
                f"rollback 后不允许再执行任何事件（event_id={event.event_id}）",
                event,
            )

        wave_index = event.wave  # 类型校验保证三类事件都带 wave
        runtime = self.runtimes[wave_index - 1]  # type: ignore[operator]

        if event.type == RolloutAuditEventType.WAVE_STARTED:
            if runtime.started:
                return self.violation(
                    RolloutViolationKind.WAVE_ALREADY_STARTED,
                    f"第 {wave_index} 波已启动，不允许重复启动"
                    f"（event_id={event.event_id}）",
                    event,
                )
            # 只能启动“下一波”：前序波必须全部启动，且紧邻上一波已 promote。
            previous = self.runtimes[wave_index - 2] if wave_index > 1 else None
            if previous is not None and not previous.started:
                return self.violation(
                    RolloutViolationKind.WAVE_SKIPPED,
                    f"第 {wave_index - 1} 波尚未启动，不允许越波启动第 {wave_index} 波"
                    f"（event_id={event.event_id}）",
                    event,
                )
            if previous is not None:
                decision = _overall_decision(
                    _metric_results(previous, self.specs)
                )
                if decision != OverallDecision.PROMOTE:
                    return self.violation(
                        RolloutViolationKind.WAVE_SKIPPED,
                        f"第 {wave_index - 1} 波护栏尚未 promote（当前判定："
                        f"{decision.value if decision is not None else 'running'}），"
                        f"不允许启动第 {wave_index} 波（event_id={event.event_id}）",
                        event,
                    )
            runtime.started = True
            for spec in self.specs:
                runtime.batches.setdefault(spec.id, [])
            return None

        if event.type == RolloutAuditEventType.NODE_COMPLETED:
            if not runtime.started:
                return self.violation(
                    RolloutViolationKind.NODE_BEFORE_WAVE_START,
                    f"节点 '{event.node_id}' 只能在所属第 {wave_index} 波启动后完成"
                    f"（event_id={event.event_id}）",
                    event,
                )
            if event.node_id in runtime.completed:
                return self.violation(
                    RolloutViolationKind.NODE_ALREADY_COMPLETED,
                    f"节点 '{event.node_id}' 已完成，不允许重复完成"
                    f"（event_id={event.event_id}）",
                    event,
                )
            runtime.completed.add(event.node_id)  # type: ignore[arg-type]
            return None

        # metric_batch
        if not runtime.started:
            return self.violation(
                RolloutViolationKind.METRICS_BEFORE_WAVE_START,
                f"第 {wave_index} 波尚未启动，不允许接收指标批次"
                f"（event_id={event.event_id}）",
                event,
            )
        if not runtime.all_nodes_completed():
            pending = sorted(runtime.node_ids - runtime.completed)
            return self.violation(
                RolloutViolationKind.METRICS_BEFORE_NODES_COMPLETE,
                f"第 {wave_index} 波尚有节点未完成（{', '.join(pending)}），"
                f"全部节点完成后才可接收指标（event_id={event.event_id}）",
                event,
            )

        metric_id = event.metric_id  # type: ignore[assignment]
        batches = runtime.batches.setdefault(metric_id, [])  # type: ignore[arg-type]
        expected = len(batches) + 1
        if event.batch != expected:
            return self.violation(
                RolloutViolationKind.METRIC_BATCH_GAP,
                f"第 {wave_index} 波指标 '{metric_id}' 的批次号必须从 1 起连续递增，"
                f"期望批次 {expected}，实际 {event.batch}"
                f"（event_id={event.event_id}）",
                event,
            )

        # 冻结判定：以已有批次重算（不含本批），越界后不再消费后续批次。
        results = {
            result.id: result
            for result in _metric_results(runtime, self.specs)
        }
        current = results[metric_id]  # type: ignore[index]
        if current.stop_batch is not None:
            return self.violation(
                RolloutViolationKind.METRIC_FROZEN,
                f"第 {wave_index} 波指标 '{metric_id}' 已在批次 "
                f"{current.stop_batch} 判定 {current.decision.value} 并冻结，"
                f"不允许再接收批次（event_id={event.event_id}）",
                event,
            )

        batches.append(
            GuardrailBatch(failures=event.failures, total=event.total)  # type: ignore[arg-type]
        )
        new_results = {
            result.id: result
            for result in _metric_results(runtime, self.specs)
        }
        if (
            new_results[metric_id].decision  # type: ignore[index]
            == GuardrailDecision.ROLLBACK
        ):
            self.rolled_back_wave = wave_index
        return None


def audit_rollout(request: RolloutAuditRequest) -> RolloutAuditResponse:
    """折叠执行事件流，逐波独立累计护栏，返回合法状态或首个违规。"""
    return _AuditEngine(request).replay()
