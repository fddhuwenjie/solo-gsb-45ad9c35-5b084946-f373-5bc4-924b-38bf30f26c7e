"""评估引擎：迭代依赖（显式栈）-> 有序属性规则 -> SHA-256 分桶 -> 累计权重选变体。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from app.models import (
    BUCKETS,
    Condition,
    FlagConfig,
    Operator,
    Switch,
    TraceEntry,
)


def hash_bucket(salt: str, switch_id: str, user_id: str) -> int:
    """以“盐值、开关 ID、用户 ID”的 SHA-256 摘要映射到 0~9999。

    摘要前 8 个十六进制字符（32 bit）取模 BUCKETS。
    """
    digest = hashlib.sha256(
        f"{salt}:{switch_id}:{user_id}".encode("utf-8")
    ).hexdigest()
    return int(digest[:8], 16) % BUCKETS


@dataclass
class EvaluationResult:
    """单次开关评估的内部结果。"""

    switch_id: str
    variant_id: str
    payload: dict[str, Any] | None
    served_default: bool
    reason: str | None
    trace: list[TraceEntry] = field(default_factory=list)


class ConfigError(Exception):
    """配置未通过校验，无法评估。"""


@dataclass
class _ChildTrace:
    """已完成的依赖子评估的轨迹段。

    展开为最终轨迹时，整段步骤名统一加上 `dep:<switch_id>:` 前缀。
    用引用代替逐条复制：每个开关自己的步骤只创建一次，深层链路的
    轨迹组装从 O(深度²) 降为 O(深度)，输出内容不变。
    """

    switch_id: str
    segments: list[_Segment]


# 轨迹段：本开关自己的一步，或一个待加前缀的依赖子树。
_Segment = TraceEntry | _ChildTrace


@dataclass
class _FrameResult:
    """一次开关评估的内部结果（轨迹以段树暂存，根帧结束时才展开）。"""

    switch_id: str
    variant_id: str
    payload: dict[str, Any] | None
    served_default: bool
    reason: str | None
    segments: list[_Segment]


@dataclass
class _Frame:
    """一次开关评估的显式栈帧。

    替代原来的递归调用帧：`segments` 累积本开关自己的轨迹段，
    `dep_index` 指向下一条待处理的依赖（恢复执行时从这里继续）。
    """

    switch: Switch
    segments: list[_Segment] = field(default_factory=list)
    dep_index: int = 0


def _flatten_trace(segments: list[_Segment]) -> list[TraceEntry]:
    """把段树展开为扁平轨迹：每深入一层依赖，步骤名加 `dep:<id>:` 前缀。

    显式栈迭代展开（先序遍历，兄弟段严格按声明顺序），深度不受递归上限约束。
    """
    trace: list[TraceEntry] = []
    iterators: list[tuple[Iterator[_Segment], str]] = [(iter(segments), "")]
    while iterators:
        it, prefix = iterators[-1]
        seg = next(it, None)
        if seg is None:  # 当前层段已耗尽，回溯（段树中不会出现 None 段）。
            iterators.pop()
            continue
        if isinstance(seg, TraceEntry):
            trace.append(TraceEntry(step=prefix + seg.step, detail=seg.detail))
        else:
            iterators.append((iter(seg.segments), f"{prefix}dep:{seg.switch_id}:"))
    return trace


class _ConditionEvaluator:
    """对用户上下文计算单个条件。"""

    def evaluate(self, cond: Condition, context: dict[str, Any]) -> bool:
        present = cond.attribute in context
        actual = context.get(cond.attribute)
        op = cond.operator

        if op == Operator.EXISTS:
            return present if cond.value else not present
        if not present:
            return False

        if op == Operator.EQ:
            return actual == cond.value
        if op == Operator.NEQ:
            return actual != cond.value
        if op == Operator.IN:
            return self._safe(lambda: actual in cond.value)
        if op == Operator.NOT_IN:
            return self._safe(lambda: actual not in cond.value)
        if op == Operator.CONTAINS:
            # 字符串子串 / 列表成员 / 字典键 都走 in。
            return self._safe(lambda: cond.value in actual)
        if op in (Operator.GT, Operator.GTE, Operator.LT, Operator.LTE):
            return self._compare(op, actual, cond.value)
        return False

    @staticmethod
    def _safe(fn: Any) -> bool:
        """类型不合时（如对 None 做成员判断）按不命中处理。"""
        try:
            return bool(fn())
        except TypeError:
            return False

    @staticmethod
    def _compare(op: Operator, actual: Any, expected: Any) -> bool:
        try:
            if op == Operator.GT:
                return actual > expected
            if op == Operator.GTE:
                return actual >= expected
            if op == Operator.LT:
                return actual < expected
            return actual <= expected
        except TypeError:
            return False


class Evaluator:
    """在一份已校验配置上迭代评估开关（显式帧栈，深度不受递归上限约束）。"""

    def __init__(self, config: FlagConfig):
        self._config = config
        self._switches = {sw.id: sw for sw in config.switches}
        self._conditions = _ConditionEvaluator()

    def evaluate(
        self,
        switch_id: str,
        user_id: str,
        context: dict[str, Any],
        _stack: list[str] | None = None,
    ) -> EvaluationResult:
        # _stack 保留原语义：调用方给定的祖先路径，仅参与环兜底判定；
        # 常规调用（路由层、解释模块）不传。
        base_path = list(_stack) if _stack else []

        if switch_id in base_path:
            cycle = base_path[base_path.index(switch_id) :] + [switch_id]
            # 校验阶段应已拦截环；运行时再遇到（如直接调用）按失败兜底。
            raise ConfigError("依赖图存在环: " + " -> ".join(cycle))
        if switch_id not in self._switches:
            raise KeyError(switch_id)

        frames: list[_Frame] = []
        # 与帧栈同步的祖先路径：path_ids 保序（环报错用），path_set 供 O(1) 判环。
        # 帧内开关 ID 与 base_path 互不相交（入栈前判环），帧之间也不会重复。
        path_ids: list[str] = list(base_path)
        path_set: set[str] = set(base_path)

        def push(sw: Switch) -> None:
            frames.append(self._new_frame(sw))
            path_ids.append(sw.id)
            path_set.add(sw.id)

        def pop() -> None:
            frames.pop()
            path_set.discard(path_ids.pop())

        push(self._switches[switch_id])
        # 最近弹出的帧的结果，等待并入新的栈顶帧（即其父帧）。
        completed: _FrameResult | None = None

        while frames:
            frame = frames[-1]
            sw = frame.switch
            segments = frame.segments

            # 子帧刚完成：其轨迹段挂到本帧（展开时统一加前缀），再做依赖判定。
            if completed is not None:
                dep = sw.dependencies[frame.dep_index]
                dep_result = completed
                completed = None
                segments.append(
                    _ChildTrace(switch_id=dep.switch, segments=dep_result.segments)
                )

                if dep.variant is None:
                    satisfied = not dep_result.served_default
                    expected = None
                else:
                    satisfied = dep_result.variant_id == dep.variant
                    expected = dep.variant

                segments.append(
                    TraceEntry(
                        step=f"dep:{dep.switch}:check",
                        detail={
                            "dependency": dep.switch,
                            "expected_variant": expected,
                            "resolved_variant": dep_result.variant_id,
                            "satisfied": satisfied,
                        },
                    )
                )
                frame.dep_index += 1

                if not satisfied:
                    completed = self._default_result(
                        sw,
                        segments,
                        "dependency_not_satisfied",
                        {
                            "dependency": dep.switch,
                            "expected_variant": expected,
                            "resolved_variant": dep_result.variant_id,
                            "default_variant": sw.default_variant,
                        },
                    )
                    pop()
                continue

            # 1) 总开关关闭 -> 默认变体。
            if not sw.enabled:
                completed = self._default_result(
                    sw,
                    segments,
                    "switch_disabled",
                    {"default_variant": sw.default_variant},
                )
                pop()
                continue

            # 2) 按声明顺序检查依赖：任一不满足 -> 默认变体（短路）。
            if frame.dep_index < len(sw.dependencies):
                dep = sw.dependencies[frame.dep_index]
                if dep.switch not in self._switches:
                    completed = self._default_result(
                        sw,
                        segments,
                        "unknown_dependency",
                        {"dependency": dep.switch},
                    )
                    pop()
                    continue
                if dep.switch in path_set:
                    cycle = (
                        path_ids[path_ids.index(dep.switch) :] + [dep.switch]
                    )
                    # 校验阶段应已拦截环；运行时再遇到（如直接调用）按失败兜底。
                    raise ConfigError("依赖图存在环: " + " -> ".join(cycle))
                push(self._switches[dep.switch])
                continue

            # 3) 有序规则：首条全部条件命中的规则生效。
            matched_rule = None
            for rule in sw.rules:
                results = [
                    self._conditions.evaluate(c, context) for c in rule.conditions
                ]
                hit = all(results)
                segments.append(
                    TraceEntry(
                        step="rule_evaluated",
                        detail={
                            "rule_id": rule.id,
                            "conditions": [
                                {
                                    "attribute": c.attribute,
                                    "operator": c.operator.value,
                                    "value": c.value,
                                    "matched": matched,
                                }
                                for c, matched in zip(rule.conditions, results)
                            ],
                            "matched": hit,
                        },
                    )
                )
                if hit:
                    matched_rule = rule
                    segments.append(
                        TraceEntry(step="rule_matched", detail={"rule_id": rule.id})
                    )
                    break

            if matched_rule is None:
                completed = self._default_result(
                    sw,
                    segments,
                    "no_rule_matched",
                    {"default_variant": sw.default_variant},
                )
                pop()
                continue

            # 4) SHA-256(盐值、开关 ID、用户 ID) -> 0~9999 -> 累计权重区间。
            salt = sw.salt if sw.salt is not None else sw.id
            bucket = hash_bucket(salt, sw.id, user_id)
            cumulative = 0
            chosen: str | None = None
            ranges: list[dict[str, Any]] = []
            for alloc in matched_rule.allocations:
                start = cumulative
                cumulative += alloc.weight
                end = cumulative - 1
                ranges.append(
                    {
                        "variant_id": alloc.variant_id,
                        "start": start,
                        "end": end,
                        "weight": alloc.weight,
                    }
                )
                if chosen is None and start <= bucket <= end:
                    chosen = alloc.variant_id

            segments.append(
                TraceEntry(
                    step="bucket",
                    detail={
                        "salt": salt,
                        "user_id": user_id,
                        "bucket": bucket,
                        "buckets": BUCKETS,
                        "ranges": ranges,
                    },
                )
            )

            # 配置已校验权重总和 == BUCKETS，chosen 理论上必然存在。
            if chosen is None:
                completed = self._default_result(
                    sw,
                    segments,
                    "bucket_out_of_range",
                    {"default_variant": sw.default_variant},
                )
                pop()
                continue

            segments.append(
                TraceEntry(step="variant_selected", detail={"variant_id": chosen})
            )
            completed = _FrameResult(
                switch_id=sw.id,
                variant_id=chosen,
                payload=self._payload_of(sw, chosen),
                served_default=False,
                reason=None,
                segments=segments,
            )
            pop()

        # 栈空时 completed 必为根帧结果（根帧入栈后循环至少执行一轮）。
        assert completed is not None
        return EvaluationResult(
            switch_id=completed.switch_id,
            variant_id=completed.variant_id,
            payload=completed.payload,
            served_default=completed.served_default,
            reason=completed.reason,
            trace=_flatten_trace(completed.segments),
        )

    @staticmethod
    def _new_frame(sw: Switch) -> _Frame:
        """开启一次开关评估：压入 start 轨迹段。"""
        frame = _Frame(switch=sw)
        frame.segments.append(
            TraceEntry(
                step="start",
                detail={"switch_id": sw.id, "enabled": sw.enabled},
            )
        )
        return frame

    def _default_result(
        self,
        sw: Switch,
        segments: list[_Segment],
        reason: str,
        detail: dict[str, Any],
    ) -> _FrameResult:
        """构造默认变体结果：追加 default 轨迹段并返回。"""
        segments.append(
            TraceEntry(step="default", detail={"reason": reason, **detail})
        )
        return _FrameResult(
            switch_id=sw.id,
            variant_id=sw.default_variant,
            payload=self._payload_of(sw, sw.default_variant),
            served_default=True,
            reason=reason,
            segments=segments,
        )

    @staticmethod
    def _payload_of(sw: Switch, variant_id: str) -> dict[str, Any] | None:
        for variant in sw.variants:
            if variant.id == variant_id:
                return variant.payload
        return None
