"""开关下线方案：为已关闭（enabled=false）的开关生成可执行的分层删除计划。

语义概述：

- 候选开关必须已关闭：关闭开关评估时恒返回默认变体，因此其默认变体
  视为“终态”；
- 外部（非候选）开关依赖候选时，仅当依赖显式期望该终态
  （variant == 候选默认变体）才可自动删除该依赖边（改写）：候选删除后
  该依赖恒满足，移除不改变外部开关行为；其余情形（未指定 variant 或
  期望其它变体）移除依赖会改变行为，连同仍引用候选的活动客户端一起
  列为 blocker；
- 候选之间的依赖不构成 blocker：依赖边随候选删除一并消失，由删除顺序
  保证不产生悬空引用；
- 存在任一 blocker 时不给出任何部分方案；
- 无 blocker 时：先输出外部依赖改写，再按“依赖方先于被依赖方”对候选
  分层删除（同层 ID 升序），并给出完整结果配置——按“改写 → 逐层删除”
  的顺序应用，每一步的配置都能通过 /validate 的现有校验。
"""

from __future__ import annotations

from app.models import (
    BlockerKind,
    DependencyRewrite,
    FlagConfig,
    RetirementBlocker,
    RetirementPlanRequest,
    RetirementPlanResponse,
    RetirementStatus,
)


class RetirementRequestError(Exception):
    """请求与配置不一致（未知 ID、候选未关闭等），路由层转为 422。"""


def build_retirement_plan(request: RetirementPlanRequest) -> RetirementPlanResponse:
    """生成下线方案：先收集 blocker，存在任一 blocker 即不出方案。"""
    switches = {sw.id: sw for sw in request.config.switches}

    # 候选必须存在且已关闭（关闭开关的默认变体即终态）。
    for switch_id in request.switch_ids:
        target = switches.get(switch_id)
        if target is None:
            raise RetirementRequestError(f"候选开关不存在: '{switch_id}'")
        if target.enabled:
            raise RetirementRequestError(
                f"候选开关 '{switch_id}' 仍处于开启状态（enabled=true），不能下线"
            )

    # 客户端引用必须指向已知开关，否则无法判断它是否阻断下线。
    for ref in request.client_references:
        if ref.switch_id not in switches:
            raise RetirementRequestError(
                f"客户端引用指向不存在的开关: '{ref.switch_id}'"
            )

    candidates = set(request.switch_ids)
    terminal = {sid: switches[sid].default_variant for sid in candidates}

    blockers: list[RetirementBlocker] = []
    # 活动客户端引用候选：该客户端仍在使用，阻断下线。
    for ref in request.client_references:
        if ref.switch_id in candidates:
            blockers.append(
                RetirementBlocker(
                    kind=BlockerKind.CLIENT_REFERENCE,
                    switch_id=ref.switch_id,
                    terminal_variant=terminal[ref.switch_id],
                    client_id=ref.client_id,
                )
            )

    # 外部依赖：显式期望终态的可改写，其余为 blocker。
    rewrite_pairs: set[tuple[str, str]] = set()
    for sw in request.config.switches:
        if sw.id in candidates:
            # 候选之间的依赖随候选删除一并消失，由分层删除顺序保证合法性。
            continue
        for dep in sw.dependencies:
            if dep.switch not in candidates:
                continue
            if dep.variant is not None and dep.variant == terminal[dep.switch]:
                rewrite_pairs.add((sw.id, dep.switch))
            else:
                blockers.append(
                    RetirementBlocker(
                        kind=BlockerKind.EXTERNAL_DEPENDENCY,
                        switch_id=dep.switch,
                        terminal_variant=terminal[dep.switch],
                        dependent_id=sw.id,
                        expected_variant=dep.variant,
                    )
                )

    if blockers:
        # 有 blocker 时不得给出部分方案：只返回全部阻断项。
        blockers.sort(
            key=lambda b: (
                b.switch_id,
                b.kind.value,
                b.client_id or "",
                b.dependent_id or "",
                b.expected_variant or "",
            )
        )
        return RetirementPlanResponse(
            status=RetirementStatus.BLOCKED, blockers=blockers
        )

    rewrites = [
        DependencyRewrite(dependent_id=dependent_id, removed_dependency=candidate)
        for dependent_id, candidate in sorted(rewrite_pairs)
    ]
    return RetirementPlanResponse(
        status=RetirementStatus.READY,
        rewrites=rewrites,
        deletion_layers=_deletion_layers(request.config, candidates),
        result_config=_result_config(request.config, candidates),
    )


def _deletion_layers(config: FlagConfig, candidates: set[str]) -> list[list[str]]:
    """把候选按“依赖方先于被依赖方”分层：同层 ID 升序。

    每层为当前剩余候选中不再被其它剩余候选依赖的开关——删除它们不会
    留下悬空的依赖边。配置已通过环校验，候选子图无环，分层必然收敛。
    """
    dependents: dict[str, set[str]] = {sid: set() for sid in candidates}
    for sw in config.switches:
        if sw.id not in candidates:
            continue
        for dep in sw.dependencies:
            if dep.switch in candidates:
                dependents[dep.switch].add(sw.id)

    remaining = set(candidates)
    layers: list[list[str]] = []
    while remaining:
        layer = sorted(sid for sid in remaining if not (dependents[sid] & remaining))
        # 无环图每层必非空；空层只可能由环导致（校验阶段已拦截）。
        assert layer, "候选子图存在环，无法分层"
        layers.append(layer)
        remaining.difference_update(layer)
    return layers


def _result_config(config: FlagConfig, candidates: set[str]) -> FlagConfig:
    """应用全部改写与删除后的完整配置：移除候选开关及指向候选的依赖边。"""
    switches = []
    for sw in config.switches:
        if sw.id in candidates:
            continue
        kept = [dep for dep in sw.dependencies if dep.switch not in candidates]
        switches.append(sw.model_copy(update={"dependencies": kept}))
    return FlagConfig(version=config.version, switches=switches)
