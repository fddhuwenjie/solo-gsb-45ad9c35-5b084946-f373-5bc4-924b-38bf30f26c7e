"""POST /sdk/rollout-plan 测试：最少波次、字典序平局、已达目标跳过、blocker 与请求校验。"""

from __future__ import annotations

import pytest

URL = "/sdk/rollout-plan"


def node(
    id: str,
    *,
    digest: str = "old",
    region: str = "r1",
    fd: str | None = None,
    weight: int = 1,
) -> dict:
    return {
        "id": id,
        "current_digest": digest,
        "region": region,
        "failure_domain": fd if fd is not None else f"fd-{id}",
        "traffic_weight": weight,
    }


def body(
    nodes: list[dict],
    *,
    target: str = "new",
    parallel: int = 16,
    global_limit: int = 1_000_000,
    region_limits: dict[str, int] | None = None,
) -> dict:
    regions = {n["region"] for n in nodes}
    return {
        "target_digest": target,
        "nodes": nodes,
        "max_parallel": parallel,
        "max_unavailable_weight": global_limit,
        "region_max_unavailable_weight": region_limits
        or {region: 1_000_000 for region in regions},
    }


def plan(client, payload: dict) -> dict:
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def assert_wave_invariants(data: dict, payload: dict) -> None:
    """通用不变量：只规划摘要不同的节点；波内约束合法；累计完成单调且覆盖全集。"""
    by_id = {n["id"]: n for n in payload["nodes"]}
    planned = [
        nid for nid, n in by_id.items() if n["current_digest"] != payload["target_digest"]
    ]
    assert data["planned_node_count"] == len(planned)
    assert data["up_to_date_nodes"] == sorted(
        nid for nid, n in by_id.items() if n["current_digest"] == payload["target_digest"]
    )

    waves = data["waves"]
    seen: set[str] = set(data["up_to_date_nodes"])
    for index, wave in enumerate(waves, start=1):
        assert wave["wave"] == index
        ids = wave["node_ids"]
        assert ids == sorted(ids) and len(ids) == len(set(ids))
        assert not (seen & set(ids))  # 节点不重复出现
        # 并发上限。
        assert len(ids) <= payload["max_parallel"]
        # 全局/分区不可用权重上限。
        assert (
            wave["global_unavailable_weight"] <= payload["max_unavailable_weight"]
        )
        assert wave["global_unavailable_weight"] == sum(by_id[i]["traffic_weight"] for i in ids)
        region_sums: dict[str, int] = {}
        domains: set[str] = set()
        for nid in ids:
            n = by_id[nid]
            region_sums[n["region"]] = region_sums.get(n["region"], 0) + n["traffic_weight"]
            assert n["failure_domain"] not in domains
            domains.add(n["failure_domain"])
        assert wave["region_unavailable_weight"] == {
            region: region_sums[region] for region in sorted(region_sums)
        }
        for region, value in region_sums.items():
            assert value <= payload["region_max_unavailable_weight"][region]
        # 累计完成节点。
        seen.update(ids)
        assert wave["completed_node_ids"] == sorted(seen)
        assert wave["completed_node_count"] == len(seen)
    assert seen == set(by_id)


# ---------------------------------------------------------------------------
# 已达目标 / 空规划
# ---------------------------------------------------------------------------


def test_all_nodes_up_to_date_yields_empty_plan(client):
    """全部节点当前摘要已等于目标：无波次，但仍列出 up_to_date_nodes。"""
    payload = body(
        [node("a", digest="t"), node("b", digest="t")], target="t", parallel=1
    )
    data = plan(client, payload)

    assert data["status"] == "planned"
    assert data["waves"] == []
    assert data["up_to_date_nodes"] == ["a", "b"]
    assert data["planned_node_count"] == 0
    assert data["blockers"] == []


def test_up_to_date_nodes_preseed_completed_list(client):
    """已达目标节点不参与分波，但从第一波起就计入累计完成节点。"""
    payload = body(
        [node("a", digest="t"), node("b"), node("c")], target="t", parallel=1
    )
    data = plan(client, payload)

    assert [w["node_ids"] for w in data["waves"]] == [["b"], ["c"]]
    assert data["waves"][0]["completed_node_ids"] == ["a", "b"]
    assert data["waves"][0]["completed_node_count"] == 2
    assert data["waves"][1]["completed_node_ids"] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# 最少波次（最优性）
# ---------------------------------------------------------------------------


def test_single_wave_when_everything_fits(client):
    """所有约束宽松时全部待更新节点同一波铺开。"""
    payload = body([node("a"), node("b"), node("c")], parallel=3, global_limit=10)
    data = plan(client, payload)

    assert len(data["waves"]) == 1
    assert data["waves"][0]["node_ids"] == ["a", "b", "c"]
    assert data["waves"][0]["global_unavailable_weight"] == 3


def test_max_parallel_forces_minimum_waves(client):
    """并发数 2 与 4 个节点精确要求 2 波，不允许多拆。"""
    payload = body(
        [node("a", weight=1), node("b", weight=1), node("c", weight=1), node("d", weight=1)],
        parallel=2,
        global_limit=10,
    )
    data = plan(client, payload)

    assert [w["node_ids"] for w in data["waves"]] == [["a", "b"], ["c", "d"]]


def test_parallel_one_gives_singleton_waves(client):
    """max_parallel=1：每波恰好一个节点，按 ID 升序铺开。"""
    payload = body([node("c"), node("a"), node("b")], parallel=1, global_limit=10)
    data = plan(client, payload)

    assert [w["node_ids"] for w in data["waves"]] == [["a"], ["b"], ["c"]]


def test_global_weight_bound_forces_extra_wave(client):
    """全局权重上限让“并发放得下”的节点不能同波：必须 2 波而非贪心拆成 3 波。

    权重 [50,40,30,20]、上限 60、并发 4：任意两个节点都不能随意同波
    （50+20=70 超限），可行的 2 波划分只有 [a,c]=80?——见下：真正可行的
    配对为 [a,?]：a 只能配 d(20)=70 超限，故 a 独占；b 配 d=60 合法、
    c 独占会得到 3 波——精确解为 [a,c?]…。用 [45,40,20,15]、上限 60：
    [a,d]=60 与 [b,c]=60 恰好 2 波；贪心把 a 配到任何更重节点都会超限，
    朴素“逐个填满首波”会退化成更多波。
    """
    payload = body(
        [
            node("a", weight=45),
            node("b", weight=40),
            node("c", weight=20),
            node("d", weight=15),
        ],
        parallel=4,
        global_limit=60,
    )
    data = plan(client, payload)

    assert len(data["waves"]) == 2
    assert_wave_invariants(data, payload)
    # 最优双波划分是 [a,d]=60 与 [b,c]=60；含最小 ID a 的可行首波只有 [a,d]。
    assert data["waves"][0]["node_ids"] == ["a", "d"]
    assert data["waves"][1]["node_ids"] == ["b", "c"]


def test_region_weight_bound_forces_wave_split(client):
    """分区权重上限独立约束：单分区超限同样要求拆波。"""
    payload = body(
        [
            node("a", region="east", weight=60),
            node("b", region="east", weight=60),
            node("c", region="west", weight=60),
        ],
        parallel=3,
        global_limit=1000,
        region_limits={"east": 60, "west": 1000},
    )
    data = plan(client, payload)

    # east 两个 60 不可同波；west 可与任一 east 同波 → 2 波。
    assert len(data["waves"]) == 2
    for wave in data["waves"]:
        east_in_wave = [nid for nid in wave["node_ids"] if nid in ("a", "b")]
        assert len(east_in_wave) <= 1
    assert_wave_invariants(data, payload)


def test_failure_domain_at_most_one_per_wave(client):
    """同一 failure_domain 的节点不能同波，即使并发与权重都允许。"""
    payload = body(
        [
            node("a", fd="rack-1"),
            node("b", fd="rack-1"),
            node("c", fd="rack-2"),
        ],
        parallel=3,
        global_limit=10,
    )
    data = plan(client, payload)

    assert len(data["waves"]) == 2
    for wave in data["waves"]:
        domains = [
            {"a": "rack-1", "b": "rack-1", "c": "rack-2"}[nid]
            for nid in wave["node_ids"]
        ]
        assert len(domains) == len(set(domains))
    assert_wave_invariants(data, payload)


def test_failure_domain_does_not_create_blocker(client):
    """只有同域冲突时逐节点单独成波总能铺开：不是 blocker。"""
    payload = body(
        [node("a", fd="same", weight=100), node("b", fd="same", weight=100)],
        parallel=2,
        global_limit=1000,
    )
    data = plan(client, payload)

    assert data["status"] == "planned"
    assert len(data["waves"]) == 2
    assert data["blockers"] == []


def test_three_waves_required(client):
    """并发 1 的三节点：恰好三波，顺序即 ID 升序。"""
    payload = body(
        [node("x"), node("y"), node("z")], parallel=1, global_limit=10
    )
    data = plan(client, payload)
    assert [w["node_ids"] for w in data["waves"]] == [["x"], ["y"], ["z"]]


# ---------------------------------------------------------------------------
# 平局：每波 ID 升序后波次序列字典序最小
# ---------------------------------------------------------------------------


def test_tie_prefers_singleton_first_wave(client):
    """两种 2 波划分都最优时，首波 ID 元组字典序更小者胜。

    权重 [40,30,20,10]、全局 60：划分 [a]|[b,c,d]=60 与 [a,d]|[b,c]
    都为 2 波；(a,) < (a,d)，故首波取单节点 a。
    """
    payload = body(
        [
            node("a", weight=40),
            node("b", weight=30),
            node("c", weight=20),
            node("d", weight=10),
        ],
        parallel=4,
        global_limit=60,
    )
    data = plan(client, payload)

    assert [w["node_ids"] for w in data["waves"]] == [["a"], ["b", "c", "d"]]


def test_tie_different_cardinality_compares_elementwise(client):
    """长度不同的首波元组按位置比较，而不是按子集掩码数值比较。

    权重 [1,2,3,4]、全局上限 6、并发 4（单分区、故障域互不相同）。
    最优 2 波划分中含 n0 的合法首波候选为：
      {n0,n1,n2}=6（掩码 0111=7）、{n0,n2}=4（掩码 0101=5）、{n0,n3}=5；
    其中 (n0,n1,n2) 字典序最小——但它的子集掩码数值反而比 {n0,n2} 大。
    朴素按“最小掩码”取首波会错选 {n0,n2}。注意 {n0,n1} 虽可行，但余下的
    {n2,n3}=7 超限，无法支撑 2 波划分。
    """
    payload = body(
        [
            node("n0", weight=1),
            node("n1", weight=2),
            node("n2", weight=3),
            node("n3", weight=4),
        ],
        parallel=4,
        global_limit=6,
    )
    data = plan(client, payload)

    assert len(data["waves"]) == 2
    assert data["waves"][0]["node_ids"] == ["n0", "n1", "n2"]
    assert data["waves"][1]["node_ids"] == ["n3"]


def test_tie_result_independent_of_submission_order(client):
    """节点乱序提交不影响规划：结果与按 ID 排序提交完全一致。"""
    ordered = body(
        [node("d"), node("b"), node("a"), node("c")], parallel=2, global_limit=10
    )
    shuffled = body(
        [node("c"), node("a"), node("d"), node("b")], parallel=2, global_limit=10
    )
    assert plan(client, ordered)["waves"] == plan(client, shuffled)["waves"]


def test_each_wave_ids_sorted_and_waves_sorted(client):
    """返回的每一波内部 ID 升序，波次之间也按首波起字典序排列。"""
    payload = body(
        [node(f"n{i}") for i in range(6)], parallel=2, global_limit=10
    )
    data = plan(client, payload)

    sequences = [tuple(w["node_ids"]) for w in data["waves"]]
    assert sequences == sorted(sequences)
    assert sequences == [("n0", "n1"), ("n2", "n3"), ("n4", "n5")]


# ---------------------------------------------------------------------------
# blocker：任一节点单独更新也超限
# ---------------------------------------------------------------------------


def test_single_node_over_global_limit_is_blocker(client):
    """节点自身权重超过全局上限：blocked，且不给任何波次。"""
    payload = body([node("a", weight=20)], parallel=1, global_limit=10)
    data = plan(client, payload)

    assert data["status"] == "blocked"
    assert data["waves"] == []
    assert data["blockers"] == [
        {
            "kind": "global_weight_exceeded",
            "node_id": "a",
            "region": "r1",
            "traffic_weight": 20,
            "limit": 10,
        }
    ]


def test_single_node_over_region_limit_is_blocker(client):
    """节点自身权重超过其分区上限：blocked（全局未超）。"""
    payload = body(
        [node("a", region="east", weight=20)],
        parallel=1,
        global_limit=100,
        region_limits={"east": 10},
    )
    data = plan(client, payload)

    assert data["status"] == "blocked"
    assert data["waves"] == []
    assert data["blockers"] == [
        {
            "kind": "region_weight_exceeded",
            "node_id": "a",
            "region": "east",
            "traffic_weight": 20,
            "limit": 10,
        }
    ]


def test_blocker_over_both_limits_emits_two_entries(client):
    """同时超全局与分区上限：同一节点给出两条 blocker（先全局后分区）。"""
    payload = body(
        [node("a", weight=50)], parallel=1, global_limit=10,
        region_limits={"r1": 20},
    )
    data = plan(client, payload)

    assert data["status"] == "blocked"
    assert [b["kind"] for b in data["blockers"]] == [
        "global_weight_exceeded",
        "region_weight_exceeded",
    ]


def test_any_blocker_aborts_entire_plan(client):
    """一个 blocker 即否决全部规划：其它本可更新的节点也不出现在 waves 中。"""
    payload = body(
        [node("a", weight=1), node("b", weight=99), node("c", weight=1)],
        parallel=3,
        global_limit=50,
    )
    data = plan(client, payload)

    assert data["status"] == "blocked"
    assert data["waves"] == []
    assert [b["node_id"] for b in data["blockers"]] == ["b"]
    # up_to_date 与 planned 计数仍如实给出。
    assert data["planned_node_count"] == 3


def test_blockers_sorted_by_node_id(client):
    """blocker 顺序与提交顺序无关：按节点 ID（再按 kind）升序。"""
    payload = body(
        [node("z", weight=99), node("a", weight=99), node("m", weight=99)],
        parallel=3,
        global_limit=50,
    )
    data = plan(client, payload)

    assert [b["node_id"] for b in data["blockers"]] == ["a", "m", "z"]


def test_blocker_coexists_with_up_to_date_nodes(client):
    """blocked 时仍如实列出已达目标节点，但不出任何波次。"""
    payload = body(
        [
            node("a", digest="t", weight=1),
            node("b", weight=99),
        ],
        target="t",
        parallel=2,
        global_limit=50,
    )
    data = plan(client, payload)

    assert data["status"] == "blocked"
    assert data["waves"] == []
    assert data["up_to_date_nodes"] == ["a"]
    assert data["planned_node_count"] == 1
    assert [b["node_id"] for b in data["blockers"]] == ["b"]


def test_node_equal_to_limit_is_allowed_not_blocker(client):
    """权重恰好等于上限是合法的（闭区间），单节点一波。"""
    payload = body([node("a", weight=10)], parallel=1, global_limit=10)
    data = plan(client, payload)

    assert data["status"] == "planned"
    assert [w["node_ids"] for w in data["waves"]] == [["a"]]


def test_region_sum_equal_to_limit_shares_one_wave(client):
    """同分区多节点权重之和恰好等于分区上限（闭区间）可同波。"""
    payload = body(
        [node("a", region="east", weight=25), node("b", region="east", weight=25)],
        parallel=2,
        global_limit=100,
        region_limits={"east": 50},
    )
    data = plan(client, payload)

    assert len(data["waves"]) == 1
    assert data["waves"][0]["region_unavailable_weight"] == {"east": 50}


# ---------------------------------------------------------------------------
# 综合约束交互
# ---------------------------------------------------------------------------


def test_combined_constraints_interaction(client):
    """并发 + 全局 + 分区 + 故障域同时收紧时仍给出精确最少波次。"""
    payload = body(
        [
            node("a", region="east", fd="r1", weight=30),
            node("b", region="east", fd="r2", weight=30),
            node("c", region="west", fd="r1", weight=30),
            node("d", region="west", fd="r2", weight=30),
        ],
        parallel=3,
        global_limit=70,
        region_limits={"east": 30, "west": 60},
    )
    data = plan(client, payload)
    assert_wave_invariants(data, payload)

    # east 上限 30：a、b 永不与同区节点同波；
    # 至少 2 波（a、b 必分开），验证精确解达到该下界。
    assert len(data["waves"]) == 2


def test_region_weights_reported_per_wave(client):
    """每波只报告实际涉及分区的权重和，跨波分区累加不跨波累计。"""
    payload = body(
        [
            node("a", region="east", weight=4),
            node("b", region="west", weight=7),
            node("c", region="east", weight=5),
        ],
        parallel=1,
        global_limit=100,
        region_limits={"east": 100, "west": 100},
    )
    data = plan(client, payload)

    assert data["waves"][0]["region_unavailable_weight"] == {"east": 4}
    assert data["waves"][1]["region_unavailable_weight"] == {"west": 7}
    assert data["waves"][2]["region_unavailable_weight"] == {"east": 5}


def test_sixteen_nodes_accepted_and_exact(client):
    """16 个节点为允许上限：精确求解并满足全部不变量。"""
    payload = body(
        [
            node(f"n{i:02d}", region=f"r{i % 3}", fd=f"fd{i % 5}", weight=(i % 7) + 1)
            for i in range(16)
        ],
        parallel=3,
        global_limit=12,
        region_limits={"r0": 12, "r1": 12, "r2": 12},
    )
    data = plan(client, payload)
    assert data["status"] == "planned"
    assert_wave_invariants(data, payload)
    assert len(data["waves"]) >= 1


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_parallel", 0),
        ("max_parallel", -1),
        ("max_unavailable_weight", 0),
        ("max_unavailable_weight", -3),
    ],
)
def test_non_positive_limits_rejected(client, field, value):
    payload = body([node("a")])
    payload[field] = value
    response = client.post(URL, json=payload)
    assert response.status_code == 422


def test_non_positive_traffic_weight_rejected(client):
    payload = body([node("a", weight=0)])
    assert client.post(URL, json=payload).status_code == 422
    payload = body([node("a", weight=-5)])
    assert client.post(URL, json=payload).status_code == 422


def test_non_positive_region_limit_rejected(client):
    payload = body([node("a", region="east")], region_limits={"east": 0})
    assert client.post(URL, json=payload).status_code == 422


def test_duplicate_node_id_rejected(client):
    payload = body([node("a"), node("a")])
    assert client.post(URL, json=payload).status_code == 422


def test_unknown_region_limit_rejected(client):
    """节点所在分区未在 region_max_unavailable_weight 中声明 → 422。"""
    payload = body(
        [node("a", region="east")], region_limits={"west": 100}
    )
    response = client.post(URL, json=payload)
    assert response.status_code == 422


def test_empty_region_limit_map_rejected(client):
    payload = body([node("a")])
    payload["region_max_unavailable_weight"] = {}
    assert client.post(URL, json=payload).status_code == 422


def test_empty_node_list_rejected(client):
    payload = body([])
    assert client.post(URL, json=payload).status_code == 422


def test_more_than_sixteen_nodes_rejected(client):
    payload = body([node(f"n{i}") for i in range(17)])
    assert client.post(URL, json=payload).status_code == 422


def test_sixteen_nodes_boundary_accepted(client):
    payload = body([node(f"n{i}") for i in range(16)])
    assert client.post(URL, json=payload).status_code == 200


def test_blank_identifiers_rejected(client):
    base = node("a")
    for field in ("id", "current_digest", "region", "failure_domain"):
        bad = dict(base)
        bad[field] = ""
        response = client.post(URL, json=body([bad]))
        assert response.status_code == 422, field
    payload = body([node("a")])
    payload["target_digest"] = ""
    assert client.post(URL, json=payload).status_code == 422


def test_extra_fields_rejected(client):
    payload = body([node("a")])
    payload["unexpected"] = 1
    assert client.post(URL, json=payload).status_code == 422

    payload = body([node("a")])
    payload["nodes"][0]["unexpected"] = 1
    assert client.post(URL, json=payload).status_code == 422


def test_missing_required_field_rejected(client):
    payload = body([node("a")])
    del payload["max_parallel"]
    assert client.post(URL, json=payload).status_code == 422


def test_unused_region_limit_allowed(client):
    """声明了但没有节点使用的分区限额是合法的。"""
    payload = body(
        [node("a", region="east")], region_limits={"east": 100, "ghost": 100}
    )
    assert client.post(URL, json=payload).status_code == 200
