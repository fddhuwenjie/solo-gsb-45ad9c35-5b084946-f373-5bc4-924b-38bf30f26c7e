"""最小充分上下文：为一次灰度评估求仍需保留的最小属性集合。

先以当前 context 复用评估引擎得到基准结果（baseline），再按
（属性数升序、同数时属性名字典序）枚举保留集合；对每个保留集合，把其余
属性依次替换为其域内所有候选 JSON 值与缺失态并重新评估，只有
``variant_id``、``served_default`` 与 ``reason`` 始终与基准一致，该保留
集合才算充分。返回枚举到的首个最小集合（并列最小集合按字典序取前者）。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from app.evaluator import EvaluationResult, Evaluator
from app.models import MinimizeContextRequest


class MinimizeRequestError(Exception):
    """请求与配置不匹配（如属性域未覆盖被引用属性），由路由层转 422。"""


@dataclass(frozen=True)
class _Missing:
    """缺失态哨兵：试算组合中表示该属性应从 context 移除。"""


# 唯一哨兵；JSON 解出的候选值不可能与它同对象。
_MISSING = _Missing()


def _referenced_attributes(request: MinimizeContextRequest) -> set[str]:
    """收集目标开关及其传递依赖的规则条件所引用的全部属性。

    调用前配置必须已通过校验（开关存在、无未知依赖、无环），因此这里直接
    沿依赖边遍历即可（显式栈迭代，深链不触发递归上限）。即使被依赖开关关闭，
    其规则属性仍计入——请求要求按传递依赖所引用的属性声明域，与评估时是否
    实际走到无关。
    """
    switches = {sw.id: sw for sw in request.config.switches}
    referenced: set[str] = set()
    seen: set[str] = set()
    stack = [request.switch_id]

    while stack:
        switch_id = stack.pop()
        if switch_id in seen:
            continue
        seen.add(switch_id)
        sw = switches[switch_id]
        for rule in sw.rules:
            for condition in rule.conditions:
                referenced.add(condition.attribute)
        for dep in sw.dependencies:
            stack.append(dep.switch)

    return referenced


@dataclass
class MinimizeOutcome:
    """最小化搜索结果。"""

    kept: list[str]
    removed: list[str]
    checked_combinations: int


def minimize_context(
    request: MinimizeContextRequest,
) -> tuple[EvaluationResult, MinimizeOutcome]:
    """在已校验配置上求最小充分上下文，返回基准结果与搜索结果。

    开关不存在时抛 ``KeyError``（与 Evaluator.evaluate 一致，路由层转 404）。
    """
    referenced = _referenced_attributes(request)
    domains = {domain.attribute: domain for domain in request.domains}

    uncovered = sorted(referenced - domains.keys())
    if uncovered:
        raise MinimizeRequestError(
            "属性域未覆盖被引用属性: " + ", ".join(uncovered)
        )
    extra = sorted(domains.keys() - referenced)
    if extra:
        raise MinimizeRequestError(
            "属性域声明了未被目标开关及传递依赖引用的属性: "
            + ", ".join(extra)
        )

    evaluator = Evaluator(request.config)
    baseline = evaluator.evaluate(
        switch_id=request.switch_id,
        user_id=request.user_id,
        context=request.context,
    )

    def matches_baseline(context: dict[str, Any]) -> bool:
        result = evaluator.evaluate(
            switch_id=request.switch_id,
            user_id=request.user_id,
            context=context,
        )
        return (
            result.variant_id == baseline.variant_id
            and result.served_default == baseline.served_default
            and result.reason == baseline.reason
        )

    attributes = sorted(referenced)
    # 每个属性的试算状态：候选值按请求声明顺序，缺失态（若允许）排在末尾。
    states: dict[str, list[Any]] = {
        domain.attribute: (
            [*domain.values, _MISSING]
            if domain.allow_missing
            else list(domain.values)
        )
        for domain in request.domains
    }

    checked = 0
    for size in range(len(attributes) + 1):
        for kept in itertools.combinations(attributes, size):
            kept_set = frozenset(kept)
            removed = [attr for attr in attributes if attr not in kept_set]
            sufficient = True
            for combo in itertools.product(*(states[attr] for attr in removed)):
                checked += 1
                trial = dict(request.context)
                for attr, value in zip(removed, combo):
                    if value is _MISSING:
                        trial.pop(attr, None)
                    else:
                        trial[attr] = value
                if not matches_baseline(trial):
                    sufficient = False
                    break
            if sufficient:
                return baseline, MinimizeOutcome(
                    kept=list(kept),
                    removed=removed,
                    checked_combinations=checked,
                )

    # 保留全部属性时试算 context 即基准 context（removed 为空，恰重评一次），
    # 必然充分，逻辑上不会执行到这里。
    raise RuntimeError("最小充分集合搜索未收敛")  # pragma: no cover
