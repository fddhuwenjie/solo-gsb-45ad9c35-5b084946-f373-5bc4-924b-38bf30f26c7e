"""权重重排：为变体权重调整生成最少扰动现有用户的连续分桶方案。

语义概述：

- 桶域固定为 [0, 9999]（共 10000 桶）。当前有序 allocations 的区间布局
  固定不变：按给定顺序依次铺排权重，每个变体占据一段闭区间；
- 目标只给出 {variant_id, weight}（允许新增/删除变体），服务端枚举目标
  变体的全部排列，每种排列对应一种连续分桶布局（按排列顺序铺排权重）；
- 首选“分配结果不变的桶数（稳定桶）最大”的排列，并列时按目标 ID 序列
  的字典序取最小者；
- 响应给出推荐顺序、每段闭区间、稳定/迁移桶数、from_variant -> to_variant
  迁移矩阵（仅非零项），以及把相邻且迁移方向相同的桶合并后的迁移区间；
  推荐分段与迁移区间都完整划分桶域——每个桶恰好归属一个变体/一个方向。
"""

from __future__ import annotations

from itertools import permutations

from app.models import (
    BUCKETS,
    MigrationInterval,
    MigrationMatrixEntry,
    RebalanceAllocation,
    RebalanceRequest,
    RebalanceResponse,
    RebalanceSegment,
)


def _layout(allocations: list[RebalanceAllocation]) -> list[tuple[str, int, int]]:
    """把有序权重铺成连续闭区间段，返回 (variant_id, start, end) 列表。"""
    segments: list[tuple[str, int, int]] = []
    start = 0
    for allocation in allocations:
        end = start + allocation.weight - 1
        segments.append((allocation.variant_id, start, end))
        start = end + 1
    return segments


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    """两个闭区间的重叠长度（不相交为 0）。"""
    return max(0, min(a_end, b_end) - max(a_start, b_start) + 1)


def _stable_buckets(
    current_by_variant: dict[str, tuple[int, int]],
    order: tuple[str, ...],
    target_weights: dict[str, int],
) -> int:
    """给定目标排列，计算分配结果不变的桶数（同变体区间重叠长度之和）。

    新增变体没有当前区间、被删除变体不在目标中，二者都不贡献稳定桶。
    """
    stable = 0
    start = 0
    for variant_id in order:
        end = start + target_weights[variant_id] - 1
        current_interval = current_by_variant.get(variant_id)
        if current_interval is not None:
            stable += _overlap(current_interval[0], current_interval[1], start, end)
        start = end + 1
    return stable


def choose_order(request: RebalanceRequest) -> tuple[tuple[str, ...], int]:
    """枚举目标变体的全部排列，返回 (稳定桶最大的排列, 稳定桶数)。

    并列时按目标 ID 序列的字典序取最小者（元组比较即逐元素字典序）。
    """
    current_by_variant = {
        variant_id: (start, end)
        for variant_id, start, end in _layout(request.current)
    }
    target_weights = {a.variant_id: a.weight for a in request.target}

    best_order: tuple[str, ...] | None = None
    best_stable = -1
    for permutation in permutations(target_weights):
        stable = _stable_buckets(current_by_variant, permutation, target_weights)
        if (
            best_order is None
            or stable > best_stable
            or (stable == best_stable and permutation < best_order)
        ):
            best_order = permutation
            best_stable = stable
    # target 至少 1 个变体（模型已保证），best_order 必然被赋值。
    assert best_order is not None
    return best_order, best_stable


def rebalance(request: RebalanceRequest) -> RebalanceResponse:
    """生成最少扰动的连续分桶方案及迁移明细。"""
    order, stable = choose_order(request)
    target_weights = {a.variant_id: a.weight for a in request.target}

    segments: list[RebalanceSegment] = []
    start = 0
    for variant_id in order:
        weight = target_weights[variant_id]
        end = start + weight - 1
        segments.append(
            RebalanceSegment(
                variant_id=variant_id, start=start, end=end, weight=weight
            )
        )
        start = end + 1

    # 双指针扫描当前段与目标段：每个基本区间内 (from, to) 方向恒定；
    # 相邻且迁移方向相同的基本区间合并为一段输出。
    current_segments = _layout(request.current)
    target_segments = [(s.variant_id, s.start, s.end) for s in segments]
    matrix: dict[tuple[str, str], int] = {}
    migrations: list[MigrationInterval] = []
    i = j = 0
    while i < len(current_segments) and j < len(target_segments):
        from_id, current_start, current_end = current_segments[i]
        to_id, target_start, target_end = target_segments[j]
        lo = max(current_start, target_start)
        hi = min(current_end, target_end)
        if lo <= hi:
            buckets = hi - lo + 1
            matrix[(from_id, to_id)] = matrix.get((from_id, to_id), 0) + buckets
            last = migrations[-1] if migrations else None
            if (
                last is not None
                and last.from_variant == from_id
                and last.to_variant == to_id
                and last.end == lo - 1
            ):
                # 相邻且同向：并入上一段。
                migrations[-1] = MigrationInterval(
                    from_variant=from_id,
                    to_variant=to_id,
                    start=last.start,
                    end=hi,
                    buckets=last.buckets + buckets,
                )
            else:
                migrations.append(
                    MigrationInterval(
                        from_variant=from_id,
                        to_variant=to_id,
                        start=lo,
                        end=hi,
                        buckets=buckets,
                    )
                )
        if current_end < target_end:
            i += 1
        elif target_end < current_end:
            j += 1
        else:
            i += 1
            j += 1

    return RebalanceResponse(
        order=list(order),
        segments=segments,
        stable_buckets=stable,
        migrated_buckets=BUCKETS - stable,
        matrix=[
            MigrationMatrixEntry(
                from_variant=from_id, to_variant=to_id, buckets=buckets
            )
            for (from_id, to_id), buckets in sorted(matrix.items())
        ],
        migrations=migrations,
    )
