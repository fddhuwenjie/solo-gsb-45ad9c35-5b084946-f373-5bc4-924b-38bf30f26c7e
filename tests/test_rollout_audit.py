"""POST /sdk/rollout-audit 测试：序贯折叠、逐波护栏复用、违规即停与请求校验。"""

from __future__ import annotations

import math

import pytest

URL = "/sdk/rollout-audit"

# 标准护栏：p0=0.01, p1=0.02, alpha=0.05, beta=0.1。
P0, MI, ALPHA, BETA = 0.01, 0.01, 0.05, 0.1
UPPER = math.log((1 - BETA) / ALPHA)
LOWER = math.log(BETA / (1 - ALPHA))


def guard(id: str = "m", **overrides) -> dict:
    params = dict(
        id=id,
        baseline_rate=P0,
        max_increase=MI,
        alpha=ALPHA,
        beta=BETA,
    )
    params.update(overrides)
    return params


def wave(
    index: int,
    node_ids: list[str],
    *,
    global_weight: int | None = None,
    regions: dict[str, int] | None = None,
    cumulative: list[str] | None = None,
) -> dict:
    return {
        "wave": index,
        "node_ids": node_ids,
        "global_unavailable_weight": global_weight if global_weight is not None else len(node_ids),
        "region_unavailable_weight": regions or {"r1": len(node_ids)},
        "completed_node_ids": cumulative if cumulative is not None else node_ids,
        "completed_node_count": len(cumulative if cumulative is not None else node_ids),
    }


def plan_dict(waves: list[dict], *, up_to_date: list[str] | None = None) -> dict:
    return {
        "target_digest": "new",
        "status": "planned",
        "up_to_date_nodes": up_to_date or [],
        "planned_node_count": sum(len(w["node_ids"]) for w in waves),
        "waves": waves,
        "blockers": [],
    }


def ev(eid: str, seq: int, type: str, **fields) -> dict:
    data = {"event_id": eid, "sequence": seq, "type": type}
    data.update(fields)
    return data


def start(eid: str, seq: int, wave_: int) -> dict:
    return ev(eid, seq, "wave_started", wave=wave_)


def done(eid: str, seq: int, wave_: int, node_id: str) -> dict:
    return ev(eid, seq, "node_completed", wave=wave_, node_id=node_id)


def batch(
    eid: str,
    seq: int,
    wave_: int,
    metric: str = "m",
    *,
    n: int = 1,
    failures: int = 0,
    total: int = 100,
) -> dict:
    return ev(
        eid,
        seq,
        "metric_batch",
        wave=wave_,
        metric_id=metric,
        batch=n,
        failures=failures,
        total=total,
    )


# 全成功批次：每批 LLR 增量 ≈ -1.0152，3 批跌破下阈值（pass）。
PASS_BATCHES = 3


def promote_wave(prefix: str, wave_: int, nodes: list[str], seq_start: int) -> tuple[list[dict], int]:
    """生成把一波推进到 promote 的完整事件序列（单指标、全成功）。"""
    events: list[dict] = [start(f"{prefix}-s", seq_start, wave_)]
    seq = seq_start + 1
    for node_id in nodes:
        events.append(done(f"{prefix}-n-{node_id}", seq, wave_, node_id))
        seq += 1
    for n in range(1, PASS_BATCHES + 1):
        events.append(batch(f"{prefix}-b{n}", seq, wave_, n=n))
        seq += 1
    return events, seq


def audit(client, payload: dict) -> dict:
    response = client.post(URL, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def two_wave_plan() -> dict:
    return plan_dict(
        [
            wave(1, ["a"], cumulative=["a", "u"]),
            wave(2, ["b"], cumulative=["a", "b", "u"]),
        ],
        up_to_date=["u"],
    )


@pytest.fixture
def full_payload(two_wave_plan) -> dict:
    events, _ = promote_wave("w1", 1, ["a"], 1)
    second, _ = promote_wave("w2", 2, ["b"], len(events) + 1)
    events.extend(second)
    return {"plan": two_wave_plan, "guardrails": [guard()], "events": events}


# ---------------------------------------------------------------------------
# 与 /sdk/rollout-plan 的端到端衔接
# ---------------------------------------------------------------------------


def test_real_planned_plan_flows_into_audit(client):
    """/sdk/rollout-plan 的 planned 响应可直接作为 audit 的 plan。"""
    plan_payload = {
        "target_digest": "new",
        "max_parallel": 1,
        "max_unavailable_weight": 50,
        "region_max_unavailable_weight": {"r1": 50},
        "nodes": [
            {"id": "a", "current_digest": "old", "region": "r1",
             "failure_domain": "fd-a", "traffic_weight": 20},
            {"id": "b", "current_digest": "old", "region": "r1",
             "failure_domain": "fd-b", "traffic_weight": 20},
        ],
    }
    planned = client.post("/sdk/rollout-plan", json=plan_payload)
    assert planned.status_code == 200
    plan = planned.json()
    assert plan["status"] == "planned"

    events: list[dict] = []
    seq = 1
    for wave_view in plan["waves"]:
        index = wave_view["wave"]
        events.append(start(f"s{index}", seq, index))
        seq += 1
        for node_id in wave_view["node_ids"]:
            events.append(done(f"n-{index}-{node_id}", seq, index, node_id))
            seq += 1
        for n in range(1, PASS_BATCHES + 1):
            events.append(batch(f"b-{index}-{n}", seq, index, n=n))
            seq += 1

    data = audit(
        client, {"plan": plan, "guardrails": [guard()], "events": events}
    )
    assert data["valid"] is True
    assert data["state"]["status"] == "completed"
    assert [w["decision"] for w in data["state"]["waves"]] == ["promote", "promote"]


# ---------------------------------------------------------------------------
# 合法日志
# ---------------------------------------------------------------------------


def test_completed_full_rollout(client, full_payload):
    data = audit(client, full_payload)
    assert data["valid"] is True
    assert data["violation"] is None
    state = data["state"]
    assert state["status"] == "completed"
    assert state["current_wave"] == 2
    assert state["replayed_event_count"] == 10
    # 已达目标节点从一开始就计入累计完成。
    assert state["all_completed_node_ids"] == ["a", "b", "u"]

    views = state["waves"]
    assert [w["wave"] for w in views] == [1, 2]
    for view in views:
        assert view["started"] is True
        assert view["status"] == "promoted"
        assert view["decision"] == "promote"
        assert view["batch_count"] == 3
        metric = view["metrics"][0]
        assert metric["id"] == "m"
        assert metric["decision"] == "pass"
        assert metric["stop_batch"] == 3
        assert [entry["batch"] for entry in metric["path"]] == [1, 2, 3]
    assert views[0]["completed_node_ids"] == ["a"]
    assert views[1]["completed_node_ids"] == ["b"]


def test_wave_guardrails_accumulate_independently(client, two_wave_plan):
    """各波护栏从空批次独立累计：第 2 波的 batch 从 1 重新开始。"""
    events, _ = promote_wave("w1", 1, ["a"], 1)
    # 第 1 波 promote 后第 2 波只收 1 批：决策为 hold，路径长度独立。
    events.append(start("w2-s", 10, 2))
    events.append(done("w2-n-b", 11, 2, "b"))
    events.append(batch("w2-b1", 12, 2, n=1))
    data = audit(client, {"plan": two_wave_plan, "guardrails": [guard()], "events": events})

    assert data["valid"] is True
    state = data["state"]
    assert state["status"] == "running"
    first, second = state["waves"]
    assert first["decision"] == "promote"
    assert second["decision"] == "hold"
    assert len(second["metrics"][0]["path"]) == 1
    assert second["metrics"][0]["llr"] == first["metrics"][0]["path"][0]["llr"]
    assert second["status"] == "awaiting_metrics"


def test_running_states_before_first_wave(client, two_wave_plan):
    data = audit(client, {"plan": two_wave_plan, "guardrails": [guard()], "events": []})
    assert data["valid"] is True
    state = data["state"]
    assert state["status"] == "running"
    assert state["current_wave"] is None
    assert state["all_completed_node_ids"] == ["u"]
    pending = state["waves"]
    assert all(w["status"] == "pending" for w in pending)
    assert all(w["metrics"] == [] for w in pending)
    assert all(w["decision"] is None for w in pending)


def test_running_state_wave_started(client, two_wave_plan):
    data = audit(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [start("s", 1, 1)],
        },
    )
    first = data["state"]["waves"][0]
    assert first["status"] == "running"
    # 启动后即给出零状态指标（阈值已就绪，路径为空）。
    assert first["metrics"][0]["decision"] == "hold"
    assert first["metrics"][0]["path"] == []
    assert first["decision"] is None
    assert data["state"]["waves"][1]["status"] == "pending"


def test_empty_plan_is_completed(client):
    """无待更新节点（0 波）：初始即 completed，任何事件都不可能合法。"""
    data = audit(
        client,
        {
            "plan": plan_dict([], up_to_date=["u"]),
            "guardrails": [guard()],
            "events": [],
        },
    )
    assert data["valid"] is True
    assert data["state"]["status"] == "completed"
    assert data["state"]["waves"] == []
    assert data["state"]["all_completed_node_ids"] == ["u"]


def test_events_fold_by_sequence_out_of_order_submission(client, two_wave_plan):
    """请求内事件可乱序提交：按 sequence 折叠，合法序列与提交顺序无关。"""
    events, _ = promote_wave("w1", 1, ["a"], 1)
    ordered = [event for event in events]
    payload = {"plan": two_wave_plan, "guardrails": [guard()], "events": list(reversed(ordered))}
    data = audit(client, payload)
    assert data["valid"] is True
    assert data["state"]["waves"][0]["decision"] == "promote"


def test_same_event_id_same_content_deduped(client, two_wave_plan):
    repeated = [start("s1", 1, 1), start("s1", 1, 1), start("s1", 1, 1)]
    data = audit(
        client, {"plan": two_wave_plan, "guardrails": [guard()], "events": repeated}
    )
    assert data["valid"] is True
    assert data["state"]["replayed_event_count"] == 1


def test_multi_metric_promote_requires_all_pass(client, two_wave_plan):
    events = [start("s", 1, 1), done("n", 2, 1, "a")]
    seq = 3
    for n in range(1, PASS_BATCHES + 1):
        events.append(batch(f"m1-b{n}", seq, 1, metric="m1", n=n))
        seq += 1
    for n in range(1, PASS_BATCHES + 1):
        events.append(batch(f"m2-b{n}", seq, 1, metric="m2", n=n))
        seq += 1
    data = audit(
        client,
        {"plan": two_wave_plan, "guardrails": [guard("m1"), guard("m2")], "events": events},
    )
    assert data["valid"] is True
    view = data["state"]["waves"][0]
    assert view["decision"] == "promote"
    assert [m["id"] for m in view["metrics"]] == ["m1", "m2"]
    assert view["batch_count"] == 3


def test_one_metric_hold_keeps_wave_in_awaiting_metrics(client, two_wave_plan):
    """一个指标 pass、另一个仍 hold：波总决策 hold，不能启动下一波。"""
    events = [start("s", 1, 1), done("n", 2, 1, "a")]
    seq = 3
    for n in range(1, PASS_BATCHES + 1):
        events.append(batch(f"m1-b{n}", seq, 1, metric="m1", n=n))
        seq += 1
    events.append(batch("m2-b1", seq, 1, metric="m2", n=1))
    data = audit(
        client,
        {"plan": two_wave_plan, "guardrails": [guard("m1"), guard("m2")], "events": events},
    )
    view = data["state"]["waves"][0]
    assert view["decision"] == "hold"
    assert view["status"] == "awaiting_metrics"


# ---------------------------------------------------------------------------
# 违规：遇到首个即停止，返回事件与事前状态
# ---------------------------------------------------------------------------


def test_node_completed_before_wave_start(client, two_wave_plan):
    data = audit(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [done("n", 1, 1, "a")],
        },
    )
    assert data["valid"] is False
    assert data["violation"]["kind"] == "node_before_wave_start"
    assert data["violation"]["event"]["event_id"] == "n"
    # 事前状态：0 条事件已重放，波次全部 pending。
    assert data["state"]["replayed_event_count"] == 0
    assert data["state"]["status"] == "running"
    assert data["state"]["current_wave"] is None


def test_metrics_before_wave_start(client, two_wave_plan):
    data = audit(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [batch("x", 1, 1, n=1)],
        },
    )
    assert data["valid"] is False
    assert data["violation"]["kind"] == "metrics_before_wave_start"


def test_metrics_only_after_all_nodes_completed(client, two_wave_plan):
    events, _ = promote_wave("w1", 1, ["a"], 1)
    # 两节点同波：只完成 a 就收指标违规。
    payload = plan_dict(
        [wave(1, ["a", "b"], cumulative=["a", "b"]), wave(2, ["c"], cumulative=["a", "b", "c"])]
    )
    data = audit(
        client,
        {
            "plan": payload,
            "guardrails": [guard()],
            "events": [start("s", 1, 1), done("na", 2, 1, "a"), batch("x", 3, 1, n=1)],
        },
    )
    assert data["valid"] is False
    assert data["violation"]["kind"] == "metrics_before_nodes_complete"
    assert "b" in data["violation"]["message"]
    # 事前状态：start + a 的完成已落状态。
    assert data["state"]["replayed_event_count"] == 2
    assert data["state"]["waves"][0]["completed_node_ids"] == ["a"]


def test_wave_skipped_when_previous_not_started(client, two_wave_plan):
    data = audit(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [start("s2", 1, 2)],
        },
    )
    assert data["valid"] is False
    assert data["violation"]["kind"] == "wave_skipped"
    assert data["state"]["current_wave"] is None


def test_wave_skipped_when_previous_on_hold(client, two_wave_plan):
    """hold 时越波：第 1 波节点全部完成但护栏未 promote 即启动第 2 波。"""
    events = [start("s1", 1, 1), done("n1", 2, 1, "a"), batch("b1", 3, 1, n=1)]
    events.append(start("s2", 4, 2))
    data = audit(
        client, {"plan": two_wave_plan, "guardrails": [guard()], "events": events}
    )
    assert data["valid"] is False
    assert data["violation"]["kind"] == "wave_skipped"
    assert "promote" in data["violation"]["message"]
    # 事前状态停在第 1 波 awaiting_metrics，违规的 wave_started 未落状态。
    assert data["state"]["replayed_event_count"] == 3
    assert data["state"]["current_wave"] == 1
    assert data["state"]["waves"][1]["status"] == "pending"


def test_wave_already_started(client, two_wave_plan):
    data = audit(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [start("s1", 1, 1), start("s1b", 2, 1)],
        },
    )
    assert data["valid"] is False
    assert data["violation"]["kind"] == "wave_already_started"
    assert data["state"]["replayed_event_count"] == 1


def test_node_already_completed(client, two_wave_plan):
    data = audit(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [
                start("s", 1, 1),
                done("n1", 2, 1, "a"),
                done("n2", 3, 1, "a"),
            ],
        },
    )
    assert data["valid"] is False
    assert data["violation"]["kind"] == "node_already_completed"
    assert data["state"]["replayed_event_count"] == 2


def test_metric_batch_gap(client, two_wave_plan):
    events = [
        start("s", 1, 1),
        done("n", 2, 1, "a"),
        batch("b1", 3, 1, n=1),
        batch("b3", 4, 1, n=3),  # 跳过批次 2
    ]
    data = audit(client, {"plan": two_wave_plan, "guardrails": [guard()], "events": events})
    assert data["valid"] is False
    assert data["violation"]["kind"] == "metric_batch_gap"
    assert data["state"]["replayed_event_count"] == 3
    assert data["state"]["waves"][0]["batch_count"] == 1


def test_metric_frozen_after_individual_pass(client, two_wave_plan):
    """单指标 pass 冻结后（其它指标仍 hold）再到批次即 metric_frozen。"""
    events = [start("s", 1, 1), done("n", 2, 1, "a")]
    seq = 3
    for n in range(1, PASS_BATCHES + 1):
        events.append(batch(f"m1-b{n}", seq, 1, metric="m1", n=n))
        seq += 1
    events.append(batch("m2-b1", seq, 1, metric="m2", n=1))
    seq += 1
    events.append(batch("m1-extra", seq, 1, metric="m1", n=PASS_BATCHES + 1))
    data = audit(
        client,
        {"plan": two_wave_plan, "guardrails": [guard("m1"), guard("m2")], "events": events},
    )
    assert data["valid"] is False
    assert data["violation"]["kind"] == "metric_frozen"
    assert "pass" in data["violation"]["message"]
    # 事前 m1 路径恰好 3 步，冻结批次不计入。
    m1 = data["state"]["waves"][0]["metrics"][0]
    assert m1["stop_batch"] == 3
    assert len(m1["path"]) == 3


def test_rollback_marks_state_and_blocks_further_events(client, two_wave_plan):
    """某波 rollback 后：状态变 rollback_required，后续任何事件均违规。"""
    events = [
        start("s", 1, 1),
        done("n", 2, 1, "a"),
        batch("bad", 3, 1, n=1, failures=50, total=100),  # LLR 越过上线
    ]
    data = audit(client, {"plan": two_wave_plan, "guardrails": [guard()], "events": events})
    assert data["valid"] is True
    assert data["state"]["status"] == "rollback_required"
    assert data["state"]["waves"][0]["status"] == "rollback_required"
    assert data["state"]["waves"][0]["decision"] == "rollback"

    # rollback 后再执行：同波后续批次违规。
    events.append(batch("after", 4, 1, n=2))
    data = audit(client, {"plan": two_wave_plan, "guardrails": [guard()], "events": events})
    assert data["valid"] is False
    assert data["violation"]["kind"] == "event_after_rollback"
    assert data["violation"]["event"]["event_id"] == "after"
    assert data["state"]["replayed_event_count"] == 3


def test_event_after_rollback_includes_wave_start(client, two_wave_plan):
    """rollback 后尝试启动下一波同样违规（rollback 后再执行均违规）。"""
    events = [
        start("s", 1, 1),
        done("n", 2, 1, "a"),
        batch("bad", 3, 1, n=1, failures=50, total=100),
        start("s2", 4, 2),
    ]
    data = audit(client, {"plan": two_wave_plan, "guardrails": [guard()], "events": events})
    assert data["valid"] is False
    assert data["violation"]["kind"] == "event_after_rollback"


def test_first_violation_stops_replay(client, two_wave_plan):
    """首个违规即停止：其后的合法事件不再处理。"""
    events = [
        done("early", 1, 1, "a"),  # 违规：波未启动
        start("late", 2, 1),
    ]
    data = audit(client, {"plan": two_wave_plan, "guardrails": [guard()], "events": events})
    assert data["valid"] is False
    assert data["violation"]["event"]["event_id"] == "early"
    assert data["state"]["current_wave"] is None
    assert data["state"]["replayed_event_count"] == 0


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------


def post(client, payload: dict):
    return client.post(URL, json=payload)


def test_plan_must_be_planned(client, two_wave_plan):
    two_wave_plan["status"] = "blocked"
    two_wave_plan["blockers"] = [
        {
            "kind": "global_weight_exceeded",
            "node_id": "a",
            "region": "r1",
            "traffic_weight": 99,
            "limit": 1,
        }
    ]
    response = post(client, {"plan": two_wave_plan, "guardrails": [guard()], "events": []})
    assert response.status_code == 422


def test_unknown_wave_reference_rejected(client, two_wave_plan):
    response = post(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [start("s", 1, 3)],
        },
    )
    assert response.status_code == 422


def test_node_not_in_wave_rejected(client, two_wave_plan):
    response = post(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [start("s", 1, 1), done("n", 2, 1, "b")],
        },
    )
    assert response.status_code == 422


def test_node_in_other_wave_rejected(client, two_wave_plan):
    # b 属于第 2 波，挂在第 1 波的完成事件上拒绝。
    response = post(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [start("s", 2, 2), done("n", 1, 1, "b")],
        },
    )
    assert response.status_code == 422


def test_unknown_metric_rejected(client, two_wave_plan):
    response = post(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [
                start("s", 1, 1),
                done("n", 2, 1, "a"),
                batch("x", 3, 1, metric="other", n=1),
            ],
        },
    )
    assert response.status_code == 422


def test_duplicate_sequence_rejected(client, two_wave_plan):
    response = post(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [start("s1", 1, 1), start("s2", 1, 2)],
        },
    )
    assert response.status_code == 422


def test_event_id_content_conflict_rejected(client, two_wave_plan):
    response = post(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [start("s1", 1, 1), start("s1", 2, 1)],
        },
    )
    assert response.status_code == 422


def test_duplicate_guardrail_id_rejected(client, two_wave_plan):
    response = post(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard("m"), guard("m")],
            "events": [],
        },
    )
    assert response.status_code == 422


def test_event_shape_violations_rejected(client, two_wave_plan):
    base = {"plan": two_wave_plan, "guardrails": [guard()]}
    # wave_started 携带 node_id。
    response = post(
        client, {**base, "events": [{"event_id": "e", "sequence": 0, "type": "wave_started", "wave": 1, "node_id": "a"}]}
    )
    assert response.status_code == 422
    # node_completed 缺 node_id。
    response = post(
        client, {**base, "events": [{"event_id": "e", "sequence": 0, "type": "node_completed", "wave": 1}]}
    )
    assert response.status_code == 422
    # metric_batch 缺 total。
    response = post(
        client,
        {
            **base,
            "events": [
                {"event_id": "e", "sequence": 0, "type": "metric_batch", "wave": 1,
                 "metric_id": "m", "batch": 1, "failures": 0}
            ],
        },
    )
    assert response.status_code == 422
    # failures > total。
    response = post(
        client,
        {
            **base,
            "events": [
                {"event_id": "e", "sequence": 0, "type": "metric_batch", "wave": 1,
                 "metric_id": "m", "batch": 1, "failures": 9, "total": 1}
            ],
        },
    )
    assert response.status_code == 422


def test_plan_consistency_violations_rejected(client):
    # 波次号不连续。
    bad = plan_dict([wave(1, ["a"]), wave(3, ["b"])])
    response = post(client, {"plan": bad, "guardrails": [guard()], "events": []})
    assert response.status_code == 422

    # 节点跨波重复。
    bad = plan_dict([wave(1, ["a"]), wave(2, ["a"])])
    response = post(client, {"plan": bad, "guardrails": [guard()], "events": []})
    assert response.status_code == 422

    # planned_node_count 不一致。
    bad = plan_dict([wave(1, ["a"])])
    bad["planned_node_count"] = 2
    response = post(client, {"plan": bad, "guardrails": [guard()], "events": []})
    assert response.status_code == 422


def test_invalid_guardrail_params_rejected(client, two_wave_plan):
    response = post(
        client,
        {"plan": two_wave_plan, "guardrails": [guard(alpha=0.8, beta=0.8)], "events": []},
    )
    assert response.status_code == 422


def test_up_to_date_node_in_waves_rejected(client, two_wave_plan):
    # two_wave_plan 的 up_to_date_nodes 含 "u"，把 u 再放进波次拒绝。
    bad = plan_dict([wave(1, ["u"])], up_to_date=["u"])
    response = post(client, {"plan": bad, "guardrails": [guard()], "events": []})
    assert response.status_code == 422


def test_extra_fields_rejected(client, two_wave_plan):
    response = post(
        client,
        {
            "plan": two_wave_plan,
            "guardrails": [guard()],
            "events": [],
            "unexpected": 1,
        },
    )
    assert response.status_code == 422
