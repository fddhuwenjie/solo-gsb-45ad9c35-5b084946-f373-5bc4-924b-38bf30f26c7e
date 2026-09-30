"""POST /flags/retirement-plan 测试：终态满足/不满足、级联下线、稳定排序与请求校验。"""

from __future__ import annotations

import copy

URL = "/flags/retirement-plan"
VALIDATE_URL = "/validate"
EVALUATE_URL = "/evaluate"


def dep(switch: str, variant: str | None = None) -> dict:
    return {"switch": switch, "variant": variant}


def rule(rule_id: str, allocations: list[tuple[str, int]]) -> dict:
    return {
        "id": rule_id,
        "conditions": [],
        "allocations": [
            {"variant_id": variant_id, "weight": weight}
            for variant_id, weight in allocations
        ],
    }


def make_switch(
    switch_id: str,
    *,
    enabled: bool = False,
    default: str = "off",
    variants: tuple[str, ...] = ("on", "off"),
    dependencies: list[dict] | None = None,
    rules: list[dict] | None = None,
) -> dict:
    """构造与响应序列化形状一致的开关字典（显式携带 null 字段）。"""
    return {
        "id": switch_id,
        "enabled": enabled,
        "salt": None,
        "default_variant": default,
        "variants": [{"id": v, "payload": None} for v in variants],
        "dependencies": dependencies or [],
        "rules": rules or [],
    }


def config(*switches: dict) -> dict:
    return {"version": 1, "switches": list(switches)}


def ref(client_id: str, switch_id: str) -> dict:
    return {"client_id": client_id, "switch_id": switch_id}


def body(cfg: dict, switch_ids, client_references=()) -> dict:
    return {
        "config": cfg,
        "switch_ids": list(switch_ids),
        "client_references": list(client_references),
    }


def plan(client, payload: dict) -> dict:
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 终态满足：外部依赖显式期望终态 → 自动改写
# ---------------------------------------------------------------------------


def test_terminal_state_dependency_is_rewritten(client):
    """外部依赖显式期望候选默认变体（终态）：改写删除依赖边后下线候选。"""
    cfg = config(
        make_switch("old_flag", default="off"),
        make_switch(
            "consumer",
            enabled=True,
            default="c_off",
            variants=("c_on", "c_off"),
            dependencies=[dep("old_flag", "off")],
            rules=[rule("all", [("c_on", 10_000)])],
        ),
    )
    data = plan(client, body(cfg, ["old_flag"]))

    assert data["status"] == "ready"
    assert data["blockers"] == []
    assert data["rewrites"] == [
        {"dependent_id": "consumer", "removed_dependency": "old_flag"}
    ]
    assert data["deletion_layers"] == [["old_flag"]]
    # 结果配置：候选被删除，外部开关的依赖边被移除，其余原样保留。
    assert data["result_config"] == config(
        make_switch(
            "consumer",
            enabled=True,
            default="c_off",
            variants=("c_on", "c_off"),
            rules=[rule("all", [("c_on", 10_000)])],
        )
    )


def test_rewrite_preserves_dependent_behavior(client):
    """改写前后外部开关评估结果一致：终态依赖恒满足，移除不改变行为。"""
    cfg = config(
        make_switch("old_flag", default="off"),
        make_switch(
            "consumer",
            enabled=True,
            default="c_off",
            variants=("c_on", "c_off"),
            dependencies=[dep("old_flag", "off")],
            rules=[rule("all", [("c_on", 10_000)])],
        ),
    )
    before = client.post(
        EVALUATE_URL,
        json={
            "config": cfg,
            "switch_id": "consumer",
            "user_id": "u1",
            "context": {},
        },
    ).json()
    # 候选已关闭，恒解析到终态 off，依赖满足，consumer 走到规则。
    assert before["variant_id"] == "c_on"
    assert before["served_default"] is False

    data = plan(client, body(cfg, ["old_flag"]))
    after = client.post(
        EVALUATE_URL,
        json={
            "config": data["result_config"],
            "switch_id": "consumer",
            "user_id": "u1",
            "context": {},
        },
    ).json()
    assert after["variant_id"] == before["variant_id"]
    assert after["served_default"] == before["served_default"]


def test_duplicate_dependency_edges_rewrite_once(client):
    """同一外部开关对同一候选的多条相同依赖边：改写只出现一次。"""
    cfg = config(
        make_switch("old_flag"),
        make_switch(
            "consumer",
            enabled=True,
            dependencies=[dep("old_flag", "off"), dep("old_flag", "off")],
        ),
    )
    data = plan(client, body(cfg, ["old_flag"]))

    assert data["status"] == "ready"
    assert data["rewrites"] == [
        {"dependent_id": "consumer", "removed_dependency": "old_flag"}
    ]
    # 两条依赖边都被移除。
    assert data["result_config"]["switches"][0]["dependencies"] == []


# ---------------------------------------------------------------------------
# 不满足终态与活动客户端引用 → blocker（不出部分方案）
# ---------------------------------------------------------------------------


def test_dependency_without_variant_is_blocker(client):
    """未指定 variant 的依赖期望任意非默认变体：候选恒为终态，永不满足 → blocker。"""
    cfg = config(
        make_switch("old_flag"),
        make_switch("consumer", enabled=True, dependencies=[dep("old_flag")]),
    )
    data = plan(client, body(cfg, ["old_flag"]))

    assert data["status"] == "blocked"
    assert data["blockers"] == [
        {
            "kind": "external_dependency",
            "switch_id": "old_flag",
            "terminal_variant": "off",
            "client_id": None,
            "dependent_id": "consumer",
            "expected_variant": None,
        }
    ]
    # 有 blocker 时不得给出部分方案。
    assert data["rewrites"] == []
    assert data["deletion_layers"] == []
    assert data["result_config"] is None


def test_dependency_expecting_other_variant_is_blocker(client):
    """依赖期望的变体不是候选终态 → blocker。"""
    cfg = config(
        make_switch("old_flag"),
        make_switch("consumer", enabled=True, dependencies=[dep("old_flag", "on")]),
    )
    data = plan(client, body(cfg, ["old_flag"]))

    assert data["status"] == "blocked"
    assert data["blockers"] == [
        {
            "kind": "external_dependency",
            "switch_id": "old_flag",
            "terminal_variant": "off",
            "client_id": None,
            "dependent_id": "consumer",
            "expected_variant": "on",
        }
    ]
    assert data["result_config"] is None


def test_active_client_reference_is_blocker(client):
    """活动客户端引用候选开关 → blocker。"""
    cfg = config(make_switch("old_flag"), make_switch("other"))
    data = plan(client, body(cfg, ["old_flag"], [ref("web-app", "old_flag")]))

    assert data["status"] == "blocked"
    assert data["blockers"] == [
        {
            "kind": "client_reference",
            "switch_id": "old_flag",
            "terminal_variant": "off",
            "client_id": "web-app",
            "dependent_id": None,
            "expected_variant": None,
        }
    ]
    assert data["result_config"] is None


def test_client_reference_to_non_candidate_does_not_block(client):
    """引用未在下线名单中的开关不构成 blocker。"""
    cfg = config(make_switch("old_flag"), make_switch("kept", enabled=True))
    data = plan(client, body(cfg, ["old_flag"], [ref("web-app", "kept")]))

    assert data["status"] == "ready"
    assert data["deletion_layers"] == [["old_flag"]]


def test_single_blocker_suppresses_whole_plan(client):
    """一个候选被阻断时，其余可下线的候选也不给部分方案。"""
    cfg = config(
        make_switch("blocked_flag"),
        make_switch("free_flag"),
        make_switch("consumer", enabled=True, dependencies=[dep("blocked_flag")]),
    )
    data = plan(client, body(cfg, ["blocked_flag", "free_flag"]))

    assert data["status"] == "blocked"
    assert [b["switch_id"] for b in data["blockers"]] == ["blocked_flag"]
    assert data["rewrites"] == []
    assert data["deletion_layers"] == []
    assert data["result_config"] is None


# ---------------------------------------------------------------------------
# 级联下线：依赖方先于被依赖方
# ---------------------------------------------------------------------------


def test_cascade_dependent_deleted_before_dependency(client):
    """zeta 依赖 alpha：先删 zeta 再删 alpha（字母序相反，证明按依赖分层）。"""
    cfg = config(
        make_switch("alpha", default="a_off", variants=("a_on", "a_off")),
        make_switch(
            "zeta",
            default="z_off",
            variants=("z_on", "z_off"),
            dependencies=[dep("alpha", "a_off")],
        ),
    )
    data = plan(client, body(cfg, ["alpha", "zeta"]))

    assert data["status"] == "ready"
    # 候选之间的依赖不需要改写，由删除顺序保证合法性。
    assert data["rewrites"] == []
    assert data["deletion_layers"] == [["zeta"], ["alpha"]]
    assert data["result_config"] == {"version": 1, "switches": []}


def test_cascade_diamond_layers(client):
    """菱形依赖 d→{b,c}→a：分三层删除，同层 ID 升序。"""
    cfg = config(
        make_switch("a"),
        make_switch("b", dependencies=[dep("a", "off")]),
        make_switch("c", dependencies=[dep("a", "off")]),
        make_switch("d", dependencies=[dep("b", "off"), dep("c", "off")]),
    )
    data = plan(client, body(cfg, ["a", "b", "c", "d"]))

    assert data["deletion_layers"] == [["d"], ["b", "c"], ["a"]]


def test_candidate_to_candidate_dependency_never_blocks(client):
    """候选依赖候选（即使期望非终态）不构成 blocker：依赖边随删除一并消失。"""
    cfg = config(
        make_switch("alpha", default="a_off", variants=("a_on", "a_off")),
        make_switch(
            "zeta",
            default="z_off",
            variants=("z_on", "z_off"),
            dependencies=[dep("alpha", "a_on")],
        ),
    )
    data = plan(client, body(cfg, ["alpha", "zeta"]))

    assert data["status"] == "ready"
    assert data["deletion_layers"] == [["zeta"], ["alpha"]]


def test_mixed_external_rewrite_and_cascade(client):
    """外部改写与级联删除同时存在：先改写外部依赖，再分层删除候选。"""
    cfg = config(
        make_switch("base"),
        make_switch("mid", dependencies=[dep("base", "off")]),
        make_switch(
            "app",
            enabled=True,
            dependencies=[dep("mid", "off"), dep("base", "off")],
        ),
    )
    data = plan(client, body(cfg, ["base", "mid"]))

    assert data["status"] == "ready"
    assert data["rewrites"] == [
        {"dependent_id": "app", "removed_dependency": "base"},
        {"dependent_id": "app", "removed_dependency": "mid"},
    ]
    assert data["deletion_layers"] == [["mid"], ["base"]]
    assert data["result_config"] == config(make_switch("app", enabled=True))


def test_every_plan_step_passes_validation(client):
    """按“改写 → 逐层删除”重放方案：每一步的配置都通过 /validate，终态即 result_config。"""
    cfg = config(
        make_switch("base"),
        make_switch("mid", dependencies=[dep("base", "off")]),
        make_switch(
            "app",
            enabled=True,
            dependencies=[dep("mid", "off"), dep("base", "off")],
            rules=[rule("all", [("on", 10_000)])],
        ),
    )
    payload = body(cfg, ["base", "mid"])
    data = plan(client, payload)
    assert data["status"] == "ready"

    current = copy.deepcopy(payload["config"])
    assert client.post(VALIDATE_URL, json=current).json()["valid"] is True
    # 第一步：应用全部外部依赖改写。
    for rewrite in data["rewrites"]:
        for sw in current["switches"]:
            if sw["id"] == rewrite["dependent_id"]:
                sw["dependencies"] = [
                    d
                    for d in sw["dependencies"]
                    if d["switch"] != rewrite["removed_dependency"]
                ]
        assert client.post(VALIDATE_URL, json=current).json()["valid"] is True
    # 随后逐层删除候选。
    for layer in data["deletion_layers"]:
        current["switches"] = [
            sw for sw in current["switches"] if sw["id"] not in layer
        ]
        assert client.post(VALIDATE_URL, json=current).json()["valid"] is True
    # 重放终点与返回的完整结果配置一致。
    assert current == data["result_config"]


# ---------------------------------------------------------------------------
# 稳定排序
# ---------------------------------------------------------------------------


def test_rewrites_sorted_by_dependent_then_candidate(client):
    """改写按 (依赖方, 候选) 升序，与声明顺序无关。"""
    cfg = config(
        make_switch("c1"),
        make_switch("c2"),
        make_switch(
            "z_consumer",
            enabled=True,
            dependencies=[dep("c2", "off"), dep("c1", "off")],
        ),
        make_switch("a_consumer", enabled=True, dependencies=[dep("c1", "off")]),
    )
    data = plan(client, body(cfg, ["c2", "c1"]))

    assert data["rewrites"] == [
        {"dependent_id": "a_consumer", "removed_dependency": "c1"},
        {"dependent_id": "z_consumer", "removed_dependency": "c1"},
        {"dependent_id": "z_consumer", "removed_dependency": "c2"},
    ]
    assert data["deletion_layers"] == [["c1", "c2"]]


def test_layer_ids_sorted_ascending(client):
    """无相互依赖的候选同层删除，层内 ID 升序（与请求顺序无关）。"""
    cfg = config(make_switch("delta"), make_switch("bravo"), make_switch("alpha"))
    data = plan(client, body(cfg, ["delta", "alpha", "bravo"]))

    assert data["deletion_layers"] == [["alpha", "bravo", "delta"]]
    assert data["result_config"] == {"version": 1, "switches": []}


def test_blockers_sorted_stably(client):
    """blocker 按 (候选 ID, 类型, 引用方) 升序。"""
    cfg = config(
        make_switch("b_flag"),
        make_switch("a_flag"),
        make_switch(
            "consumer",
            enabled=True,
            dependencies=[dep("a_flag"), dep("b_flag", "on")],
        ),
    )
    data = plan(
        client,
        body(cfg, ["a_flag", "b_flag"], [ref("web", "b_flag"), ref("app", "a_flag")]),
    )

    assert data["status"] == "blocked"
    assert [
        (b["switch_id"], b["kind"], b["client_id"] or b["dependent_id"])
        for b in data["blockers"]
    ] == [
        ("a_flag", "client_reference", "app"),
        ("a_flag", "external_dependency", "consumer"),
        ("b_flag", "client_reference", "web"),
        ("b_flag", "external_dependency", "consumer"),
    ]


def test_response_independent_of_candidate_and_reference_order(client):
    """候选与客户端引用的书写顺序不影响响应。"""
    cfg = config(
        make_switch("c1"),
        make_switch("c2"),
        make_switch("consumer", enabled=True, dependencies=[dep("c1", "off")]),
    )
    first = plan(client, body(cfg, ["c1", "c2"], [ref("web", "consumer")]))
    second = plan(client, body(cfg, ["c2", "c1"], [ref("web", "consumer")]))
    assert first == second


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------


def test_unknown_candidate_returns_422(client):
    payload = body(config(make_switch("a")), ["ghost"])
    assert client.post(URL, json=payload).status_code == 422


def test_duplicate_candidate_returns_422(client):
    payload = body(config(make_switch("a")), ["a", "a"])
    assert client.post(URL, json=payload).status_code == 422


def test_enabled_candidate_returns_422(client):
    """候选必须已关闭（enabled=false）。"""
    payload = body(config(make_switch("a", enabled=True)), ["a"])
    assert client.post(URL, json=payload).status_code == 422


def test_empty_candidate_list_returns_422(client):
    payload = body(config(make_switch("a")), [])
    assert client.post(URL, json=payload).status_code == 422


def test_duplicate_client_reference_returns_422(client):
    payload = body(config(make_switch("a")), ["a"], [ref("web", "a"), ref("web", "a")])
    assert client.post(URL, json=payload).status_code == 422


def test_same_client_referencing_different_switches_is_allowed(client):
    """重复判定针对 (client_id, switch_id) 对：同客户端引用不同开关合法。"""
    cfg = config(make_switch("a"), make_switch("b"))
    data = plan(client, body(cfg, ["a"], [ref("web", "a"), ref("web", "b")]))
    # web 仍引用候选 a → blocked，但请求本身合法（不是 422）。
    assert data["status"] == "blocked"
    assert len(data["blockers"]) == 1


def test_same_switch_referenced_by_different_clients_is_not_duplicate(client):
    """不同客户端引用同一开关是两条独立引用，各产生一个 blocker。"""
    cfg = config(make_switch("a"))
    data = plan(client, body(cfg, ["a"], [ref("web", "a"), ref("app", "a")]))
    assert data["status"] == "blocked"
    assert [b["client_id"] for b in data["blockers"]] == ["app", "web"]


def test_client_reference_to_unknown_switch_returns_422(client):
    payload = body(config(make_switch("a")), ["a"], [ref("web", "ghost")])
    assert client.post(URL, json=payload).status_code == 422


def test_invalid_config_returns_422(client):
    """配置未通过 /validate 同款校验 → 422（携带全部校验错误）。"""
    cfg = config(make_switch("a"))
    cfg["switches"][0]["default_variant"] = "missing"
    response = client.post(URL, json=body(cfg, ["a"]))
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "校验未通过" in detail["message"]
    assert any(e["code"] == "unknown_default_variant" for e in detail["errors"])


def test_extra_fields_return_422(client):
    payload = body(config(make_switch("a")), ["a"])
    payload["note"] = "unexpected"
    assert client.post(URL, json=payload).status_code == 422

    payload = body(config(make_switch("a")), ["a"], [ref("web", "a")])
    payload["client_references"][0]["comment"] = "unexpected"
    assert client.post(URL, json=payload).status_code == 422
