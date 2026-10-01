"""跨入口契约测试：三个事件入口共享 app.event_fold 的统一事件身份与折叠语义。

覆盖三类事件入口（POST /experiments/attribute、POST /sdk/reconcile、
POST /sdk/rollout-audit）共享的折叠契约：

1. 乱序提交：折叠后排序/重放，输出与提交顺序无关；
2. 等价时区：同一 UTC 时刻的不同偏移写法视为同一内容（精确重投去重）；
3. JSON 键序：context 键序不同但值相同视为同一内容（仅核对接口带 context）；
4. 冲突重投：跨类型（仅归因）或任一字段不同 → 422；
5. 同 sequence 的不同事件：铺开审计在折叠后检查 sequence 唯一性 → 422。

HTTP 路径、200/422 状态码与响应字段在三个接口上保持不变。
"""

from __future__ import annotations

import pytest

from app.artifacts import config_digest
from app.models import (
    Allocation,
    Condition,
    FlagConfig,
    Operator,
    Rule,
    Switch,
    Variant,
)

ATTRIBUTE_URL = "/experiments/attribute"
RECONCILE_URL = "/sdk/reconcile"
AUDIT_URL = "/sdk/rollout-audit"

# ---------------------------------------------------------------------------
# 归因请求构造
# ---------------------------------------------------------------------------


def exposure(event_id: str, timestamp: str, *, user: str = "u1", variant: str = "A") -> dict:
    return {
        "event_id": event_id,
        "user_id": user,
        "timestamp": timestamp,
        "variant_id": variant,
    }


def conversion(event_id: str, timestamp: str, *, user: str = "u1", value: float = 1.0) -> dict:
    return {
        "event_id": event_id,
        "user_id": user,
        "timestamp": timestamp,
        "value": value,
    }


def attribute_body(exposures: list[dict], conversions: list[dict]) -> dict:
    return {
        "switch_id": "new_checkout",
        "window_seconds": 3600,
        "washout_seconds": 0,
        "exposures": exposures,
        "conversions": conversions,
    }


# ---------------------------------------------------------------------------
# 核对请求构造
# ---------------------------------------------------------------------------


def one_hundred_percent_config() -> FlagConfig:
    """单开关：tier=gold 命中后 100% on，否则默认 off。"""
    return FlagConfig(
        switches=[
            Switch(
                id="new_checkout",
                default_variant="off",
                variants=[Variant(id="on"), Variant(id="off")],
                rules=[
                    Rule(
                        id="gold_users",
                        conditions=[Condition(
                            attribute="tier", operator=Operator.EQ, value="gold"
                        )],
                        allocations=[Allocation(variant_id="on", weight=10_000)],
                    )
                ],
            )
        ]
    )


CONFIG = one_hundred_percent_config()
DIGEST = config_digest(CONFIG.model_dump(mode="json"))
ACTIVATED = "2026-03-01T00:00:00Z"


def node_event(
    event_id: str,
    timestamp: str,
    *,
    node: str = "node-a",
    digest: str = DIGEST,
    context: dict | None = None,
    variant: str = "on",
) -> dict:
    return {
        "event_id": event_id,
        "node_id": node,
        "timestamp": timestamp,
        "digest": digest,
        "switch_id": "new_checkout",
        "user_id": "user-1",
        "context": context if context is not None else {"tier": "gold"},
        "variant_id": variant,
        "served_default": False,
        "reason": None,
    }


def reconcile_body(events: list[dict], *, grace: int = 300, skew: int = 60) -> dict:
    return {
        "snapshots": [
            {"config": CONFIG.model_dump(mode="json"), "activated_at": ACTIVATED}
        ],
        "grace_seconds": grace,
        "clock_skew_seconds": skew,
        "events": events,
    }


# ---------------------------------------------------------------------------
# 铺开审计请求构造
# ---------------------------------------------------------------------------


def audit_wave(index: int, node_ids: list[str]) -> dict:
    return {
        "wave": index,
        "node_ids": node_ids,
        "global_unavailable_weight": len(node_ids),
        "region_unavailable_weight": {"r1": len(node_ids)},
        "completed_node_ids": node_ids,
        "completed_node_count": len(node_ids),
    }


def audit_plan() -> dict:
    return {
        "target_digest": "new",
        "status": "planned",
        "up_to_date_nodes": [],
        "planned_node_count": 2,
        "waves": [audit_wave(1, ["a"]), audit_wave(2, ["b"])],
        "blockers": [],
    }


def guard() -> dict:
    return {
        "id": "m",
        "baseline_rate": 0.01,
        "max_increase": 0.01,
        "alpha": 0.05,
        "beta": 0.1,
    }


def audit_body(events: list[dict]) -> dict:
    return {"plan": audit_plan(), "guardrails": [guard()], "events": events}


def start(eid: str, seq: int, wave_: int) -> dict:
    return {"event_id": eid, "sequence": seq, "type": "wave_started", "wave": wave_}


# ---------------------------------------------------------------------------
# 1. 乱序提交：折叠 + 业务排序/重放，输出与提交顺序无关
# ---------------------------------------------------------------------------


def test_attribute_out_of_order_submission_is_order_independent(client):
    forward = attribute_body(
        exposures=[exposure("e2", "2026-01-01T00:00:20Z"),
                    exposure("e1", "2026-01-01T00:00:00Z")],
        conversions=[conversion("c1", "2026-01-01T00:00:10Z")],
    )
    reversed_body = attribute_body(
        exposures=[exposure("e1", "2026-01-01T00:00:00Z"),
                    exposure("e2", "2026-01-01T00:00:20Z")],
        conversions=[conversion("c1", "2026-01-01T00:00:10Z")],
    )
    first = client.post(ATTRIBUTE_URL, json=forward)
    second = client.post(ATTRIBUTE_URL, json=reversed_body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()


def test_reconcile_out_of_order_submission_is_order_independent(client):
    events = [
        node_event("e2", "2026-03-01T00:20:00Z", node="node-b"),
        node_event("e1", "2026-03-01T00:10:00Z", node="node-a"),
    ]
    first = client.post(RECONCILE_URL, json=reconcile_body(events))
    second = client.post(RECONCILE_URL, json=reconcile_body(list(reversed(events))))
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    # 结果固定按 (node_id, timestamp, event_id) 排序。
    assert [item["event_id"] for item in first.json()["results"]] == ["e1", "e2"]


def test_audit_out_of_order_submission_replays_by_sequence(client):
    # 同一事件（wave_started=1）的精确重投 + 一条独立的波内节点完成事件；
    # 事件按乱序提交，折叠后必须按 sequence（而非提交顺序）重放。
    node_done = {
        "event_id": "n1",
        "sequence": 2,
        "type": "node_completed",
        "wave": 1,
        "node_id": "a",
    }
    events = [node_done, start("s1", 1, 1)]
    response = client.post(AUDIT_URL, json=audit_body(events))
    assert response.status_code == 200
    data = response.json()
    # 按 sequence 重放：s1 先启动波 1，n1 再完成节点，因此合法。
    assert data["valid"] is True
    assert data["state"]["replayed_event_count"] == 2
    assert data["state"]["waves"][0]["completed_node_ids"] == ["a"]


# ---------------------------------------------------------------------------
# 2. 等价时区：同一 UTC 时刻的不同偏移写法视为同一内容（精确重投去重）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first_ts,second_ts",
    [
        ("2026-01-01T00:00:00Z", "2026-01-01T08:00:00+08:00"),
        ("2026-01-01T01:00:00+01:00", "2026-01-01T00:00:00Z"),
        ("2026-01-01T00:00:00", "2026-01-01T00:00:00Z"),
    ],
)
def test_attribute_equivalent_timezone_dedupes(client, first_ts, second_ts):
    body = attribute_body(
        exposures=[exposure("e1", first_ts), exposure("e1", second_ts)],
        conversions=[conversion("c1", "2026-01-01T00:00:10Z")],
    )
    response = client.post(ATTRIBUTE_URL, json=body)
    assert response.status_code == 200
    data = response.json()
    assert data["variants"][0]["exposed_users"] == 1
    assert len(data["attributions"]) == 1


@pytest.mark.parametrize(
    "first_ts,second_ts",
    [
        ("2026-03-01T00:30:00Z", "2026-03-01T08:30:00+08:00"),
        ("2026-03-01T01:30:00+01:00", "2026-03-01T00:30:00Z"),
        ("2026-03-01T00:30:00", "2026-03-01T00:30:00Z"),
    ],
)
def test_reconcile_equivalent_timezone_dedupes(client, first_ts, second_ts):
    body = reconcile_body(
        [node_event("e1", first_ts), node_event("e1", second_ts)]
    )
    response = client.post(RECONCILE_URL, json=body)
    assert response.status_code == 200
    assert response.json()["event_count"] == 1
    assert len(response.json()["results"]) == 1


# ---------------------------------------------------------------------------
# 3. JSON 键序：context 键序不同但值相同视为同一内容（仅核对接口）
# ---------------------------------------------------------------------------


def test_reconcile_context_key_order_dedupes(client):
    body = reconcile_body(
        [
            node_event("e1", "2026-03-01T00:30:00Z", context={"tier": "gold", "n": 1}),
            node_event("e1", "2026-03-01T00:30:00Z", context={"n": 1, "tier": "gold"}),
        ]
    )
    response = client.post(RECONCILE_URL, json=body)
    assert response.status_code == 200
    assert response.json()["event_count"] == 1


def test_reconcile_nested_context_key_order_dedupes(client):
    first = {"outer": {"b": 2, "a": 1}, "tier": "gold"}
    second = {"tier": "gold", "outer": {"a": 1, "b": 2}}
    body = reconcile_body(
        [
            node_event("e1", "2026-03-01T00:30:00Z", context=first),
            node_event("e1", "2026-03-01T00:30:00Z", context=second),
        ]
    )
    response = client.post(RECONCILE_URL, json=body)
    assert response.status_code == 200
    assert response.json()["event_count"] == 1


# ---------------------------------------------------------------------------
# 4. 冲突重投：跨类型或字段不同 → 422
# ---------------------------------------------------------------------------


def test_attribute_field_conflict_returns_422(client):
    body = attribute_body(
        exposures=[
            exposure("e1", "2026-01-01T00:00:00Z", variant="A"),
            exposure("e1", "2026-01-01T00:00:00Z", variant="B"),
        ],
        conversions=[],
    )
    assert client.post(ATTRIBUTE_URL, json=body).status_code == 422


def test_attribute_same_instant_but_different_timestamp_returns_422(client):
    # 不同 UTC 时刻即使使用不同偏移写法，也不是等价时刻，仍是字段冲突。
    body = attribute_body(
        exposures=[
            exposure("e1", "2026-01-01T00:00:00Z"),
            exposure("e1", "2026-01-01T01:00:00+01:00"),  # = 00:00Z，等价时刻
        ],
        conversions=[],
    )
    assert client.post(ATTRIBUTE_URL, json=body).status_code == 200

    body = attribute_body(
        exposures=[
            exposure("e1", "2026-01-01T00:00:00Z"),
            exposure("e1", "2026-01-01T00:01:00Z"),
        ],
        conversions=[],
    )
    assert client.post(ATTRIBUTE_URL, json=body).status_code == 422


def test_attribute_cross_type_same_id_returns_422(client):
    body = attribute_body(
        exposures=[exposure("x", "2026-01-01T00:00:00Z")],
        conversions=[conversion("x", "2026-01-01T00:00:00Z")],
    )
    assert client.post(ATTRIBUTE_URL, json=body).status_code == 422


def test_reconcile_field_conflict_returns_422(client):
    body = reconcile_body(
        [
            node_event("e1", "2026-03-01T00:30:00Z", variant="on"),
            node_event("e1", "2026-03-01T00:30:00Z", variant="off"),
        ]
    )
    assert client.post(RECONCILE_URL, json=body).status_code == 422


def test_reconcile_context_value_conflict_returns_422(client):
    body = reconcile_body(
        [
            node_event("e1", "2026-03-01T00:30:00Z", context={"tier": "gold"}),
            node_event("e1", "2026-03-01T00:30:00Z", context={"tier": "silver"}),
        ]
    )
    assert client.post(RECONCILE_URL, json=body).status_code == 422


def test_audit_field_conflict_returns_422(client):
    # 同 event_id 但 sequence 不同：内容冲突 → 422。
    body = audit_body([start("e1", 1, 1), start("e1", 2, 1)])
    assert client.post(AUDIT_URL, json=body).status_code == 422

    # 同 event_id 同 sequence 但 wave 字段不同：字段冲突 → 422。
    body = audit_body([start("e1", 1, 1), start("e1", 1, 2)])
    assert client.post(AUDIT_URL, json=body).status_code == 422


# ---------------------------------------------------------------------------
# 5. 同 sequence 的不同事件：铺开审计在折叠后检查 sequence 唯一性 → 422
# ---------------------------------------------------------------------------


def test_audit_distinct_events_same_sequence_return_422(client):
    body = audit_body([start("e1", 1, 1), start("e2", 1, 1)])
    assert client.post(AUDIT_URL, json=body).status_code == 422


def test_audit_same_sequence_checked_after_fold(client):
    # 前两条是同一事件的精确重投（折叠后只剩 1 条，不占 sequence），
    # 与 e2 同 sequence 的是不同事件，必须在“折叠之后”仍被检出 → 422。
    body = audit_body([start("e1", 1, 1), start("e1", 1, 1), start("e2", 1, 1)])
    assert client.post(AUDIT_URL, json=body).status_code == 422


def test_audit_duplicate_sequence_but_identical_redelivery_is_fine(client):
    # 对照组：同 event_id 同内容的重投折叠成一条，不存在重复 sequence。
    body = audit_body([start("e1", 1, 1), start("e1", 1, 1)])
    response = client.post(AUDIT_URL, json=body)
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["state"]["replayed_event_count"] == 1


# ---------------------------------------------------------------------------
# 精确重投只保留首次：跨入口统一语义
# ---------------------------------------------------------------------------


def test_attribute_precise_redelivery_counted_once(client):
    body = attribute_body(
        [exposure("e1", "2026-01-01T00:00:00Z")],
        [conversion("c1", "2026-01-01T00:00:10Z")],
    )
    body["exposures"].append(dict(body["exposures"][0]))
    body["conversions"].append(dict(body["conversions"][0]))
    response = client.post(ATTRIBUTE_URL, json=body)
    assert response.status_code == 200
    assert len(response.json()["attributions"]) == 1


def test_reconcile_precise_redelivery_counted_once(client):
    event = node_event("e1", "2026-03-01T00:30:00Z")
    body = reconcile_body([event, dict(event)])
    response = client.post(RECONCILE_URL, json=body)
    assert response.status_code == 200
    assert response.json()["event_count"] == 1


def test_audit_precise_redelivery_counted_once(client):
    body = audit_body([start("e1", 1, 1), start("e1", 1, 1), start("e1", 1, 1)])
    response = client.post(AUDIT_URL, json=body)
    assert response.status_code == 200
    assert response.json()["state"]["replayed_event_count"] == 1
