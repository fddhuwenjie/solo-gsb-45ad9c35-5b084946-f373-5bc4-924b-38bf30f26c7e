"""POST /rollouts/rebalance 测试：最少扰动重排、并列择优、新增/删除变体与请求校验。"""

from __future__ import annotations

URL = "/rollouts/rebalance"


def alloc(variant_id: str, weight: int) -> dict:
    return {"variant_id": variant_id, "weight": weight}


def body(current: list[dict], target: list[dict]) -> dict:
    return {"current": current, "target": target}


def rebalance(client, payload: dict) -> dict:
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def assert_partition(intervals: list[dict]) -> None:
    """区间列表完整划分 [0, 9999]：首尾贴边、段间相接，每个桶恰好归属一段。"""
    assert intervals[0]["start"] == 0
    assert intervals[-1]["end"] == 9999
    for previous, current in zip(intervals, intervals[1:]):
        assert current["start"] == previous["end"] + 1


# ---------------------------------------------------------------------------
# 基本布局与不变量
# ---------------------------------------------------------------------------


def test_identical_layout_is_fully_stable(client):
    """目标与当前完全一致：全部桶稳定，零迁移。"""
    payload = body(
        [alloc("A", 5000), alloc("B", 5000)],
        [alloc("A", 5000), alloc("B", 5000)],
    )
    data = rebalance(client, payload)

    assert data["order"] == ["A", "B"]
    assert data["stable_buckets"] == 10_000
    assert data["migrated_buckets"] == 0
    assert data["segments"] == [
        {"variant_id": "A", "start": 0, "end": 4999, "weight": 5000},
        {"variant_id": "B", "start": 5000, "end": 9999, "weight": 5000},
    ]
    assert data["matrix"] == [
        {"from_variant": "A", "to_variant": "A", "buckets": 5000},
        {"from_variant": "B", "to_variant": "B", "buckets": 5000},
    ]
    assert data["migrations"] == [
        {"from_variant": "A", "to_variant": "A", "start": 0, "end": 4999, "buckets": 5000},
        {"from_variant": "B", "to_variant": "B", "start": 5000, "end": 9999, "buckets": 5000},
    ]


def test_single_variant_replaced(client):
    """单变体整体替换：稳定 0，矩阵只有一项。"""
    data = rebalance(client, body([alloc("old", 10_000)], [alloc("new", 10_000)]))

    assert data["order"] == ["new"]
    assert data["stable_buckets"] == 0
    assert data["migrated_buckets"] == 10_000
    assert data["segments"] == [
        {"variant_id": "new", "start": 0, "end": 9999, "weight": 10_000}
    ]
    assert data["matrix"] == [
        {"from_variant": "old", "to_variant": "new", "buckets": 10_000}
    ]
    assert data["migrations"] == [
        {"from_variant": "old", "to_variant": "new", "start": 0, "end": 9999, "buckets": 10_000}
    ]


def test_segments_and_migrations_partition_buckets(client):
    """每个桶恰好归属一个变体/一个迁移方向：分段与迁移区间都完整划分桶域。"""
    payload = body(
        [alloc("A", 1000), alloc("B", 5000), alloc("C", 4000)],
        [alloc("A", 4500), alloc("B", 4000), alloc("C", 1500)],
    )
    data = rebalance(client, payload)

    assert_partition(data["segments"])
    assert_partition(data["migrations"])
    # 每段权重与闭区间长度一致，权重合计 10000。
    for segment in data["segments"]:
        assert segment["weight"] == segment["end"] - segment["start"] + 1
    assert sum(s["weight"] for s in data["segments"]) == 10_000
    # 迁移区间长度与桶数一致；矩阵桶数合计 10000。
    for interval in data["migrations"]:
        assert interval["buckets"] == interval["end"] - interval["start"] + 1
    assert sum(i["buckets"] for i in data["migrations"]) == 10_000
    assert sum(m["buckets"] for m in data["matrix"]) == 10_000
    # 矩阵对角线（from == to）即稳定桶。
    diagonal = sum(
        m["buckets"] for m in data["matrix"] if m["from_variant"] == m["to_variant"]
    )
    assert diagonal == data["stable_buckets"]
    assert data["stable_buckets"] + data["migrated_buckets"] == 10_000


def test_response_independent_of_target_request_order(client):
    """目标列表的书写顺序不影响结果（枚举全排列 + 固定平局规则）。"""
    current = [alloc("A", 1000), alloc("B", 5000), alloc("C", 4000)]
    first = rebalance(
        client, body(current, [alloc("A", 4500), alloc("B", 4000), alloc("C", 1500)])
    )
    second = rebalance(
        client, body(current, [alloc("C", 1500), alloc("A", 4500), alloc("B", 4000)])
    )
    assert first == second


# ---------------------------------------------------------------------------
# 重排优于原顺序
# ---------------------------------------------------------------------------


def test_reorder_beats_request_order(client):
    """保持请求顺序 (A,B,C) 只能稳定 4000 桶；重排为 (B,A,C) 稳定 4500 桶。"""
    payload = body(
        [alloc("A", 1000), alloc("B", 5000), alloc("C", 4000)],
        [alloc("A", 4500), alloc("B", 4000), alloc("C", 1500)],
    )
    data = rebalance(client, payload)

    assert data["order"] == ["B", "A", "C"]
    assert data["stable_buckets"] == 4500
    assert data["migrated_buckets"] == 5500
    assert data["segments"] == [
        {"variant_id": "B", "start": 0, "end": 3999, "weight": 4000},
        {"variant_id": "A", "start": 4000, "end": 8499, "weight": 4500},
        {"variant_id": "C", "start": 8500, "end": 9999, "weight": 1500},
    ]
    assert data["matrix"] == [
        {"from_variant": "A", "to_variant": "B", "buckets": 1000},
        {"from_variant": "B", "to_variant": "A", "buckets": 2000},
        {"from_variant": "B", "to_variant": "B", "buckets": 3000},
        {"from_variant": "C", "to_variant": "A", "buckets": 2500},
        {"from_variant": "C", "to_variant": "C", "buckets": 1500},
    ]
    # 相邻且迁移方向相同的桶已合并为区间。
    assert data["migrations"] == [
        {"from_variant": "A", "to_variant": "B", "start": 0, "end": 999, "buckets": 1000},
        {"from_variant": "B", "to_variant": "B", "start": 1000, "end": 3999, "buckets": 3000},
        {"from_variant": "B", "to_variant": "A", "start": 4000, "end": 5999, "buckets": 2000},
        {"from_variant": "C", "to_variant": "A", "start": 6000, "end": 8499, "buckets": 2500},
        {"from_variant": "C", "to_variant": "C", "start": 8500, "end": 9999, "buckets": 1500},
    ]


# ---------------------------------------------------------------------------
# 并列择优（稳定桶数并列时按目标 ID 序列字典序取最小）
# ---------------------------------------------------------------------------


def test_tie_broken_by_target_id_sequence(client):
    """(A,B,C) 与 (A,C,B) 并列稳定 5000：字典序更小的 (A,B,C) 胜出。"""
    payload = body(
        [alloc("A", 2500), alloc("B", 2500), alloc("C", 5000)],
        # 请求顺序故意打乱，结果仍应为字典序最小并列者。
        [alloc("C", 2500), alloc("A", 5000), alloc("B", 2500)],
    )
    data = rebalance(client, payload)

    assert data["order"] == ["A", "B", "C"]
    assert data["stable_buckets"] == 5000
    assert data["segments"] == [
        {"variant_id": "A", "start": 0, "end": 4999, "weight": 5000},
        {"variant_id": "B", "start": 5000, "end": 7499, "weight": 2500},
        {"variant_id": "C", "start": 7500, "end": 9999, "weight": 2500},
    ]
    assert data["matrix"] == [
        {"from_variant": "A", "to_variant": "A", "buckets": 2500},
        {"from_variant": "B", "to_variant": "A", "buckets": 2500},
        {"from_variant": "C", "to_variant": "B", "buckets": 2500},
        {"from_variant": "C", "to_variant": "C", "buckets": 2500},
    ]


def test_all_new_variants_pick_lexicographic_order(client):
    """目标全是新变体：任何顺序稳定桶都为 0，取字典序最小的 ID 序列。"""
    payload = body(
        [alloc("A", 5000), alloc("B", 5000)],
        [alloc("D", 5000), alloc("C", 5000)],
    )
    data = rebalance(client, payload)

    assert data["order"] == ["C", "D"]
    assert data["stable_buckets"] == 0
    assert data["migrated_buckets"] == 10_000
    assert data["matrix"] == [
        {"from_variant": "A", "to_variant": "C", "buckets": 5000},
        {"from_variant": "B", "to_variant": "D", "buckets": 5000},
    ]


# ---------------------------------------------------------------------------
# 新增 / 删除变体
# ---------------------------------------------------------------------------


def test_add_and_remove_variants(client):
    """删除 B、新增 C/D：被删变体的桶全部迁出，新变体只有迁入。"""
    payload = body(
        [alloc("A", 6000), alloc("B", 4000)],
        [alloc("A", 3000), alloc("C", 3000), alloc("D", 4000)],
    )
    data = rebalance(client, payload)

    # 并列 3000 的 (A,C,D) / (A,D,C) / (C,A,D) 中取字典序最小者。
    assert data["order"] == ["A", "C", "D"]
    assert data["stable_buckets"] == 3000
    assert data["migrated_buckets"] == 7000
    assert data["segments"] == [
        {"variant_id": "A", "start": 0, "end": 2999, "weight": 3000},
        {"variant_id": "C", "start": 3000, "end": 5999, "weight": 3000},
        {"variant_id": "D", "start": 6000, "end": 9999, "weight": 4000},
    ]
    assert data["matrix"] == [
        {"from_variant": "A", "to_variant": "A", "buckets": 3000},
        {"from_variant": "A", "to_variant": "C", "buckets": 3000},
        {"from_variant": "B", "to_variant": "D", "buckets": 4000},
    ]
    assert data["migrations"] == [
        {"from_variant": "A", "to_variant": "A", "start": 0, "end": 2999, "buckets": 3000},
        {"from_variant": "A", "to_variant": "C", "start": 3000, "end": 5999, "buckets": 3000},
        {"from_variant": "B", "to_variant": "D", "start": 6000, "end": 9999, "buckets": 4000},
    ]
    # 被删除的 B 只作为 from 出现；新增的 C/D 只作为 to 出现。
    from_variants = {m["from_variant"] for m in data["matrix"]}
    to_variants = {m["to_variant"] for m in data["matrix"]}
    assert "B" not in to_variants
    assert from_variants <= {"A", "B"}
    assert to_variants <= {"A", "C", "D"}


def test_eight_targets_accepted(client):
    """目标变体数上限为 8：恰好 8 个合法。"""
    payload = body(
        [alloc("A", 10_000)],
        [alloc(f"v{i}", 1250) for i in range(8)],
    )
    data = rebalance(client, payload)

    assert data["order"] == [f"v{i}" for i in range(8)]
    assert data["stable_buckets"] == 0
    assert_partition(data["segments"])


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------


def test_duplicate_variant_in_current_returns_422(client):
    payload = body(
        [alloc("A", 5000), alloc("A", 5000)],
        [alloc("A", 10_000)],
    )
    assert client.post(URL, json=payload).status_code == 422


def test_duplicate_variant_in_target_returns_422(client):
    payload = body(
        [alloc("A", 10_000)],
        [alloc("A", 4000), alloc("A", 6000)],
    )
    assert client.post(URL, json=payload).status_code == 422


def test_zero_or_negative_weight_returns_422(client):
    for bad_weight in (0, -1):
        payload = body(
            [alloc("A", bad_weight), alloc("B", 10_000)],
            [alloc("A", 10_000)],
        )
        assert client.post(URL, json=payload).status_code == 422
        payload = body(
            [alloc("A", 10_000)],
            [alloc("A", bad_weight), alloc("B", 10_000)],
        )
        assert client.post(URL, json=payload).status_code == 422


def test_weight_sum_must_be_10000(client):
    """任一侧权重总和不等于 10000 即拒绝。"""
    payload = body([alloc("A", 9999)], [alloc("A", 10_000)])
    assert client.post(URL, json=payload).status_code == 422
    payload = body([alloc("A", 10_000)], [alloc("A", 5000), alloc("B", 5001)])
    assert client.post(URL, json=payload).status_code == 422


def test_too_many_targets_returns_422(client):
    payload = body(
        [alloc("A", 10_000)],
        [alloc(f"v{i}", 1000) for i in range(9)] + [alloc("v9", 1000)],
    )
    assert client.post(URL, json=payload).status_code == 422


def test_empty_allocations_return_422(client):
    assert client.post(URL, json=body([], [alloc("A", 10_000)])).status_code == 422
    assert client.post(URL, json=body([alloc("A", 10_000)], [])).status_code == 422


def test_empty_variant_id_returns_422(client):
    payload = body([alloc("", 10_000)], [alloc("A", 10_000)])
    assert client.post(URL, json=payload).status_code == 422


def test_extra_fields_return_422(client):
    payload = body([alloc("A", 10_000)], [alloc("A", 10_000)])
    payload["note"] = "unexpected"
    assert client.post(URL, json=payload).status_code == 422

    payload = body([alloc("A", 10_000)], [alloc("A", 10_000)])
    payload["current"][0]["comment"] = "unexpected"
    assert client.post(URL, json=payload).status_code == 422
