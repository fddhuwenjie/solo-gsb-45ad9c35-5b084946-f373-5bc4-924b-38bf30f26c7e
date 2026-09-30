"""配置制品在 SDK 节点间的分波铺开规划：精确求最少波次，字典序破平局。

语义概述：

- 只规划当前摘要不同于 target_digest 的节点；摘要已等于目标的节点无需
  更新（计入 up_to_date_nodes，并计入累计完成节点）；
- 同一波节点同时不可用，一波必须同时满足：
    - 节点数不超过 max_parallel；
    - 全部节点流量权重之和不超过全局 max_unavailable_weight；
    - 各分区权重和不超过 region_max_unavailable_weight[region]；
    - 同一 failure_domain 至多一个节点；
- 任一节点“单独更新”（独占一波）即超过全局或其分区权重上限时无法铺开，
  列为 blocker，且不给出任何部分计划；
- 否则用节点子集精确枚举（每个候选子集是否可单独成波）+ 子集 DP
  求最少波次；平局时把每波 ID 升序后取波次序列字典序最小者——节点数
  最多 16（2^16 个子集），枚举规模有界。
"""

from __future__ import annotations

from app.models import (
    RolloutBlocker,
    RolloutBlockerKind,
    RolloutNode,
    RolloutPlanRequest,
    RolloutPlanResponse,
    RolloutStatus,
    RolloutWave,
)


def build_rollout_plan(request: RolloutPlanRequest) -> RolloutPlanResponse:
    """生成最少波次的铺开规划；存在不可单独更新的节点时只给 blockers。"""
    # 只规划摘要不同的节点；已达目标的节点无需更新，按 ID 升序记录。
    pending = [
        node
        for node in request.nodes
        if node.current_digest != request.target_digest
    ]
    up_to_date = sorted(
        node.id
        for node in request.nodes
        if node.current_digest == request.target_digest
    )

    # 规划与平局比较都以 ID 升序为基准：待规划节点按 ID 排序后编号，
    # 每个子集按位展开天然得到升序 ID 列表。
    pending.sort(key=lambda node: node.id)

    blockers = _blockers(request, pending)
    if blockers:
        # 有 blocker 时不得给出部分计划：只返回全部阻断项。
        return RolloutPlanResponse(
            target_digest=request.target_digest,
            status=RolloutStatus.BLOCKED,
            up_to_date_nodes=up_to_date,
            planned_node_count=len(pending),
            blockers=blockers,
        )

    waves_ids = _minimum_waves(request, pending)
    return RolloutPlanResponse(
        target_digest=request.target_digest,
        status=RolloutStatus.PLANNED,
        up_to_date_nodes=up_to_date,
        planned_node_count=len(pending),
        waves=_build_waves(waves_ids, pending, up_to_date),
    )


def _blockers(
    request: RolloutPlanRequest, pending: list[RolloutNode]
) -> list[RolloutBlocker]:
    """收集“独占一波仍超限”的节点：全局或其分区权重上限被单节点突破。

    并发数与故障域约束永远可以通过逐节点单独成波满足，不产生 blocker。
    """
    blockers: list[RolloutBlocker] = []
    for node in pending:
        if node.traffic_weight > request.max_unavailable_weight:
            blockers.append(
                RolloutBlocker(
                    kind=RolloutBlockerKind.GLOBAL_WEIGHT_EXCEEDED,
                    node_id=node.id,
                    region=node.region,
                    traffic_weight=node.traffic_weight,
                    limit=request.max_unavailable_weight,
                )
            )
        region_limit = request.region_max_unavailable_weight[node.region]
        if node.traffic_weight > region_limit:
            blockers.append(
                RolloutBlocker(
                    kind=RolloutBlockerKind.REGION_WEIGHT_EXCEEDED,
                    node_id=node.id,
                    region=node.region,
                    traffic_weight=node.traffic_weight,
                    limit=region_limit,
                )
            )
    # 排序与书写顺序无关：先按节点 ID（升序），同节点先全局后分区。
    blockers.sort(key=lambda blocker: (blocker.node_id, blocker.kind.value))
    return blockers


def _minimum_waves(
    request: RolloutPlanRequest, pending: list[RolloutNode]
) -> list[tuple[str, ...]]:
    """求最少波次划分，并在多解中取波次序列字典序最小者。

    可行性先做一次包含剪枝的 DFS：按位决定纳入/不纳入，任何中间选择一旦
    超过并发数、全局/分区权重或撞上同故障域即剪去纳入分支，只访问可行
    前缀，得到 feasible[mask] 与每个掩码的权重统计/理论下界。

    子集 DP：dp[mask] = 覆盖 mask 的最少波数
      dp[mask] = 1 + min(dp[mask \\ wave])，wave 取遍含 mask 最低位的
      可成波子集（任何划分都可把含最低位的波旋转到首位，波数不变）。
    候选波同样由包含优先的 DFS 生成（大子集先出），dp 一旦达到理论下界
    （并发数/全局权重/各分区权重/故障域占用给出的松弛下界）即短路。

    贪心重建：从全集起在“仍保持 dp 最优波数”的首波候选中，取升序 ID
    元组字典序最小者（注意子集数值升序并不等价于元组字典序——元素个数
    不同的两个元组按位置比较，如 (n0,n1,n2) < (n0,n2) 但前者掩码更大，
    因此对候选按其 ID 元组显式取 min），再对剩余集合递归，即得字典序最小
    的最优波序列。
    """
    size = len(pending)
    if size == 0:
        return []
    total = 1 << size

    weights = [node.traffic_weight for node in pending]
    # 分区按名字排序编号，掩码统计列与限额列表按同一顺序对齐。
    regions = sorted({node.region for node in pending})
    region_code = {region: i for i, region in enumerate(regions)}
    region_of_bit = [region_code[node.region] for node in pending]
    region_limits = [
        request.region_max_unavailable_weight[region] for region in regions
    ]

    domain_of_bit: list[int] = []
    domain_index: dict[str, int] = {}
    for node in pending:
        index = domain_index.get(node.failure_domain)
        if index is None:
            index = len(domain_index)
            domain_index[node.failure_domain] = index
        domain_of_bit.append(index)
    # 每个故障域占用的比特集合：用于理论下界（同域节点至少逐波各取一个）。
    domain_bits = [0] * len(domain_index)
    for bit, domain in enumerate(domain_of_bit):
        domain_bits[domain] |= 1 << bit

    # 逐掩码最低位递推的权重统计（整数数组，供可行性与理论下界使用）。
    global_weights = [0] * total
    region_weights = [[0] * total for _ in range(len(region_limits))]
    for mask in range(1, total):
        lowbit = mask & -mask
        bit = lowbit.bit_length() - 1
        rest = mask ^ lowbit
        global_weights[mask] = global_weights[rest] + weights[bit]
        for column, column_weights in enumerate(region_weights):
            value = column_weights[rest]
            if column == region_of_bit[bit]:
                value += weights[bit]
            column_weights[mask] = value

    # 理论松弛下界：并发数、全局权重、各分区权重、各故障域占用数的最大上取整。
    global_limit = request.max_unavailable_weight
    parallel = min(request.max_parallel, size)
    lower_bounds = [0] * total
    for mask in range(1, total):
        bound = (
            (mask.bit_count() + parallel - 1) // parallel,
            (global_weights[mask] + global_limit - 1) // global_limit,
        )
        region_bound = 0
        for column, column_weights in enumerate(region_weights):
            value = column_weights[mask]
            if value:
                required = (value + region_limits[column] - 1) // region_limits[column]
                if required > region_bound:
                    region_bound = required
        domain_bound = 0
        for group in domain_bits:
            count = (mask & group).bit_count()
            if count > domain_bound:
                domain_bound = count
        lower_bounds[mask] = max(bound[0], bound[1], region_bound, domain_bound)

    # 可行性 DFS：逐位纳入/不纳入，包含分支先入（大子集先生成），越界即剪枝。
    feasible = bytearray(total)
    # 闭包内全部使用局部别名，降低递归开销；分区权重走可变列表回溯。
    w = weights
    rb = region_of_bit
    db = domain_of_bit
    max_parallel = request.max_parallel
    g_limit = global_limit
    r_limits = region_limits
    r_weights = [0] * len(r_limits)

    def dfs(
        bit: int, selected: int, count: int, g_weight: int, d_used: int
    ) -> None:
        if bit == size:
            feasible[selected] = 1
            return
        # 不纳入当前位。
        dfs(bit + 1, selected, count, g_weight, d_used)
        # 纳入当前位：任一约束越界即剪枝（该位的纳入分支整个消失）。
        region = rb[bit]
        domain = db[bit]
        new_g = g_weight + w[bit]
        new_r = r_weights[region] + w[bit]
        if (
            count < max_parallel
            and new_g <= g_limit
            and new_r <= r_limits[region]
            and not (d_used & (1 << domain))
        ):
            r_weights[region] = new_r
            dfs(
                bit + 1,
                selected | (1 << bit),
                count + 1,
                new_g,
                d_used | (1 << domain),
            )
            r_weights[region] -= w[bit]

    dfs(0, 0, 0, 0, 0)

    # 子集 DP：候选波由包含优先 DFS 生成，达到下界即短路。
    infinity = size + 1
    dp = [infinity] * total
    dp[0] = 0
    for mask in range(1, total):
        lowbit = mask & -mask
        first_bit = lowbit.bit_length() - 1
        other_bits = [
            bit for bit in range(size) if bit != first_bit and mask & (1 << bit)
        ]
        bound = lower_bounds[mask]
        best = infinity

        def search(position: int, wave: int) -> None:
            nonlocal best
            if best == bound:
                return
            if position == len(other_bits):
                candidate = dp[mask ^ wave] + 1
                if candidate < best:
                    best = candidate
                return
            bit = other_bits[position]
            # 包含优先（大子集先出）：仅当 wave 仍可能成波时才纳入。
            new_wave = wave | (1 << bit)
            if feasible[new_wave]:
                search(position + 1, new_wave)
                if best == bound:
                    return
            # 不纳入。
            search(position + 1, wave)

        search(0, lowbit)
        dp[mask] = best

    # 贪心重建字典序最小的最优波序列：每步在仍保持 dp 最优的前提下，
    # 取升序 ID 元组字典序最小的首波（枚举含最低位的全部子集）。
    ids = [node.id for node in pending]
    waves: list[tuple[str, ...]] = []
    remaining = total - 1
    while remaining:
        lowbit = remaining & -remaining
        choices = remaining ^ lowbit
        chosen = 0
        chosen_key: tuple[str, ...] | None = None
        sub = choices
        while True:
            wave = sub | lowbit
            if feasible[wave] and dp[remaining ^ wave] + 1 == dp[remaining]:
                key = tuple(
                    ids[bit]
                    for bit in range(size)
                    if wave & (1 << bit)
                )
                if chosen_key is None or key < chosen_key:
                    chosen_key = key
                    chosen = wave
            if sub == 0:
                break
            sub = (sub - 1) & choices
        # 无 blocker 时单节点子集必可行，chosen 必然被赋值。
        assert chosen_key is not None, (
            "无可成波子集，但 blocker 检查未发现阻断节点"
        )
        waves.append(chosen_key)
        remaining ^= chosen
    return waves


def _build_waves(
    waves_ids: list[tuple[str, ...]],
    pending: list[RolloutNode],
    up_to_date: list[str],
) -> list[RolloutWave]:
    """把 ID 序列展开为响应波次：权重和与累计完成节点列表。"""
    by_id = {node.id: node for node in pending}
    completed = set(up_to_date)

    waves: list[RolloutWave] = []
    for index, wave_ids in enumerate(waves_ids, start=1):
        completed.update(wave_ids)
        region_weights: dict[str, int] = {}
        global_weight = 0
        for node_id in wave_ids:
            node = by_id[node_id]
            global_weight += node.traffic_weight
            region_weights[node.region] = (
                region_weights.get(node.region, 0) + node.traffic_weight
            )
        completed_ids = sorted(completed)
        waves.append(
            RolloutWave(
                wave=index,
                node_ids=list(wave_ids),
                global_unavailable_weight=global_weight,
                region_unavailable_weight={
                    region: region_weights[region]
                    for region in sorted(region_weights)
                },
                completed_node_ids=completed_ids,
                completed_node_count=len(completed_ids),
            )
        )
    return waves
