"""配置校验：依赖图（未知依赖 / 环）、规则引用、权重总和等。"""

from __future__ import annotations

from collections.abc import Iterator

from app.models import BUCKETS, FlagConfig, ValidationIssue


def validate_config(config: FlagConfig) -> list[ValidationIssue]:
    """对整份配置做跨实体校验，返回问题列表（空列表表示通过）。

    单个实体内的类型/约束错误在 Pydantic 解析阶段即被拦截；
    这里负责需要看到全局才能判断的规则：
      1. 开关 ID 唯一；
      2. 默认变体与规则 allocation 必须引用本开关内存在的变体；
      3. allocation 的 variant_id 不重复，权重总和恰为 10000；
      4. 规则 ID 在开关内唯一；
      5. 依赖必须指向已存在的开关，期望变体必须在目标开关内存在；
      6. 依赖图不允许出现环（自依赖也算环）。
    """
    issues: list[ValidationIssue] = []
    switches_by_id: dict[str, int] = {}

    # ---- 1. 开关 ID 唯一性 ----
    for idx, sw in enumerate(config.switches):
        if sw.id in switches_by_id:
            issues.append(
                ValidationIssue(
                    code="duplicate_switch",
                    message=f"开关 ID 重复: '{sw.id}'",
                    path=f"switches[{idx}].id",
                )
            )
        else:
            switches_by_id[sw.id] = idx

    def _add(code: str, message: str, path: str) -> None:
        issues.append(ValidationIssue(code=code, message=message, path=path))

    for idx, sw in enumerate(config.switches):
        base = f"switches[{idx}]"

        # ---- 2a. 变体 ID 唯一 ----
        variant_ids: set[str] = set()
        for vi, variant in enumerate(sw.variants):
            if variant.id in variant_ids:
                _add(
                    "duplicate_variant",
                    f"开关 '{sw.id}' 内变体 ID 重复: '{variant.id}'",
                    f"{base}.variants[{vi}].id",
                )
            variant_ids.add(variant.id)

        # ---- 2b. 默认变体引用 ----
        if sw.default_variant not in variant_ids:
            _add(
                "unknown_default_variant",
                f"开关 '{sw.id}' 的默认变体 '{sw.default_variant}' 未在 variants 中声明",
                f"{base}.default_variant",
            )

        # ---- 3. 规则：引用 + 权重 ----
        rule_ids: set[str] = set()
        for ri, rule in enumerate(sw.rules):
            rpath = f"{base}.rules[{ri}]"
            if rule.id in rule_ids:
                _add(
                    "duplicate_rule",
                    f"开关 '{sw.id}' 内规则 ID 重复: '{rule.id}'",
                    f"{rpath}.id",
                )
            rule_ids.add(rule.id)

            total = 0
            allocated_variants: set[str] = set()
            for ai, alloc in enumerate(rule.allocations):
                apath = f"{rpath}.allocations[{ai}]"
                if alloc.variant_id not in variant_ids:
                    _add(
                        "unknown_rule_variant",
                        f"规则 '{rule.id}' 引用了开关 '{sw.id}' 内不存在的变体 "
                        f"'{alloc.variant_id}'",
                        f"{apath}.variant_id",
                    )
                if alloc.variant_id in allocated_variants:
                    _add(
                        "duplicate_allocation",
                        f"规则 '{rule.id}' 内变体 '{alloc.variant_id}' 被重复分配权重",
                        f"{apath}.variant_id",
                    )
                allocated_variants.add(alloc.variant_id)
                total += alloc.weight

            if total != BUCKETS:
                _add(
                    "invalid_weight_sum",
                    f"开关 '{sw.id}' 的规则 '{rule.id}' 权重总和为 {total}，"
                    f"必须等于 {BUCKETS}",
                    f"{rpath}.allocations",
                )

        # ---- 4. 依赖：未知开关 ----
        for di, dep in enumerate(sw.dependencies):
            dpath = f"{base}.dependencies[{di}]"
            # switches_by_id 保留各 ID 的首个下标，与原线性扫描语义一致。
            target_idx = switches_by_id.get(dep.switch)
            if target_idx is None:
                _add(
                    "unknown_dependency",
                    f"开关 '{sw.id}' 依赖了不存在的开关 '{dep.switch}'",
                    f"{dpath}.switch",
                )
                continue

            target_switch = config.switches[target_idx]
            if dep.variant is not None and not any(
                v.id == dep.variant for v in target_switch.variants
            ):
                _add(
                    "unknown_dependency_variant",
                    f"开关 '{sw.id}' 的依赖 '{dep.switch}' 中不存在期望变体 "
                    f"'{dep.variant}'",
                    f"{dpath}.variant",
                )

    # ---- 5. 依赖图环检测（迭代 DFS 三色标记） ----
    graph: dict[str, list[str]] = {}
    for sw in config.switches:
        # 重复 ID 的开关不参与建图，duplicate_switch 已单独报错。
        graph.setdefault(sw.id, [])
        for dep in sw.dependencies:
            if dep.switch in switches_by_id:
                graph[sw.id].append(dep.switch)

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    reported_cycles: set[frozenset[str]] = set()

    # 显式栈替代递归：合法深链（数千层）不应触发 Python 递归上限。
    # 遍历顺序与递归版完全一致：按建图顺序取起点、按依赖声明顺序访问邻居。
    for root in graph:
        if color[root] != WHITE:
            continue
        color[root] = GRAY
        # path 为当前 DFS 路径，iterators 为路径各层尚未耗尽的邻居迭代器。
        path: list[str] = [root]
        iterators: list[Iterator[str]] = [iter(graph.get(root, []))]
        while iterators:
            descended = False
            for nxt in iterators[-1]:
                state = color.get(nxt)
                if state == GRAY:
                    cycle = path[path.index(nxt) :] + [nxt]
                    key = frozenset(cycle)
                    if key not in reported_cycles:
                        reported_cycles.add(key)
                        _add(
                            "dependency_cycle",
                            "依赖图存在环: " + " -> ".join(cycle),
                            None,
                        )
                elif state == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    iterators.append(iter(graph.get(nxt, [])))
                    descended = True
                    break
                # BLACK：子树已完成，跳过。
            if descended:
                continue
            # 当前层邻居耗尽：回溯，标记节点子树完成。
            iterators.pop()
            color[path.pop()] = BLACK

    return issues
