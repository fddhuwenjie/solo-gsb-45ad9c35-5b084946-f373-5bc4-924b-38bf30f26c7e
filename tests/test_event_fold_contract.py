"""跨入口事件身份契约测试。

三个事件入口共享同一套折叠语义（app.event_fold）：

- 乱序提交：折叠结果与提交顺序无关；
- UTC 等价时刻：naive 视为 UTC、不同偏移写法（如 Z 与 +08:00）视为同内容；
- JSON 键序：context（或任意 JSON 对象字段）键序不同但值相同视为同内容；
- 冲突重投：跨类型或任一字段不同的同 event_id 重投 → 422；
- 精确重投：同内容重投只保留首次；
- 铺开审计额外要求：折叠后 sequence 必须唯一（同 sequence 的不同事件 → 422）。

本文件通过三个 HTTP 接口（/experiments/attribute、/sdk/reconcile、
/sdk/rollout-audit）交叉验证上述统一语义；HTTP 路径、状态码与响应字段
均与各入口原有契约一致。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.artifacts import config_digest
from app.event_fold import (
    EventContentConflict,
    canonical_value,
    fold_events,
    fold_streams,
)

ATTRIBUTE_URL = "/experiments/attribute"
RECONCILE_URL = "/sdk/reconcile"
AUDIT_URL = "/sdk/rollout-audit"

T0 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
# 护栏参数与 /guardrails/evaluate 测试一致：3 批全成功即 pass。
P0, MI, ALPHA, BETA = 0.01, 0.01, 0.05, 0.1


# ---------------------------------------------------------------------------
# 公共载荷构造
# ---------------------------------------------------------------------------


def attr_payload(exposures: list[dict], conversions: list[dict]) -> dict:
    return {
        "switch_id": "new_checkout",
        "window_seconds": 3600,
        "washout_seconds": 0,
        "exposures": exposures,
        "conversions": conversions,
    }


def exposure(event_id: str, timestamp: str, *, user: str = "u1", variant: str = "on") -> dict:
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


GOLD_RULE = {
    "id": "gold_users",
    "conditions": [{"attribute": "tier", "operator": "eq", "value": "gold"}],
    "allocations": [{"variant_id": "on", "weight": 10000}],
}


def config(version: int = 1) -> dict:
    return {
        "version": version,
        "switches": [
            {
                "id": "new_checkout",
                "default_variant": "off",
                "variants": [{"id": "on"}, {"id": "off"}],
                "rules": [GOLD_RULE],
            }
        ],
    }


CONFIG_V1 = config(1)
DIGEST_V1 = config_digest(CONFIG_V1)


def reconcile_event(
    event_id: str,
    timestamp: str,
    *,
    node: str = "node-a",
    digest: str = DIGEST_V1,
    switch: str = "new_checkout",
    user: str = "user-1",
    context: dict | None = None,
    variant: str = "on",
    served_default: bool = False,
    reason: str | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "node_id": node,
        "timestamp": timestamp,
        "digest": digest,
        "switch_id": switch,
        "user_id": user,
        "context": {"tier": "gold"} if context is None else context,
        "variant_id": variant,
        "served_default": served_default,
        "reason": reason,
    }


def reconcile_payload(events: list[dict]) -> dict:
    return {
        "snapshots": [{"config": CONFIG_V1, "activated_at": "2026-03-01T00:00:00Z"}],
        "grace_seconds": 300,
        "clock_skew_seconds": 60,
        "events": events,
    }


def audit_plan(nodes: list[str] | None = None, *, up_to_date: list[str] | None = None) -> dict:
    nodes = nodes if nodes is not None else ["a"]
    return {
        "target_digest": "new",
        "status": "planned",
        "up_to_date_nodes": up_to_date or [],
        "planned_node_count": len(nodes),
        "waves": [
            {
                "wave": 1,
                "node_ids": nodes,
                "global_unavailable_weight": len(nodes),
                "region_unavailable_weight": {"r1": len(nodes)},
                "completed_node_ids": nodes,
                "completed_node_count": len(nodes),
            }
        ],
        "blockers": [],
    }


def audit_guardrail() -> dict:
    return {
        "id": "m",
        "baseline_rate": P0,
        "max_increase": MI,
        "alpha": ALPHA,
        "beta": BETA,
    }


def wave_started(event_id: str, sequence: int, wave: int = 1) -> dict:
    return {"event_id": event_id, "sequence": sequence, "type": "wave_started", "wave": wave}


def node_completed(event_id: str, sequence: int, node: str = "a", wave: int = 1) -> dict:
    return {
        "event_id": event_id,
        "sequence": sequence,
        "type": "node_completed",
        "wave": wave,
        "node_id": node,
    }


def metric_batch(
    event_id: str,
    sequence: int,
    batch: int,
    *,
    wave: int = 1,
    metric: str = "m",
    failures: int = 0,
    total: int = 100,
) -> dict:
    return {
        "event_id": event_id,
        "sequence": sequence,
        "type": "metric_batch",
        "wave": wave,
        "metric_id": metric,
        "batch": batch,
        "failures": failures,
        "total": total,
    }


def audit_payload(events: list[dict], *, plan: dict | None = None) -> dict:
    return {
        "plan": plan or audit_plan(),
        "guardrails": [audit_guardrail()],
        "events": events,
    }


# ---------------------------------------------------------------------------
# 共享层单元语义
# ---------------------------------------------------------------------------


def test_canonical_utc_equivalent_moments():
    zulu = T0
    plus_eight = datetime(2026, 3, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    naive = datetime(2026, 3, 1, 0, 0)
    assert canonical_value(zulu) == canonical_value(plus_eight)
    assert canonical_value(zulu) == canonical_value(naive)
    # 不同时刻不相等。
    assert canonical_value(zulu) != canonical_value(T0 + timedelta(seconds=1))


def test_canonical_json_key_order_insensitive_but_array_order_sensitive():
    left = {"b": [1, 2, {"c": True}], "a": None}
    right = {"a": None, "b": [1, 2, {"c": True}]}
    assert canonical_value(left) == canonical_value(right)
    assert canonical_value([1, 2]) != canonical_value([2, 1])
    assert canonical_value({"a": 1}) != canonical_value({"a": 2})


def test_fold_keeps_first_exact_repost():
    class Item:
        def __init__(self, event_id, when):
            self.event_id = event_id
            self.when = when

    first = Item("e1", T0)
    # 不同时区写法、但 UTC 时刻等价 → 精确重投，保留首次对象。
    second = Item("e1", datetime(2026, 3, 1, 8, 0, tzinfo=timezone(timedelta(hours=8))))
    folded = fold_events([first, second], lambda item: (item.when,))
    assert folded == [first]

    with pytest.raises(EventContentConflict, match="内容冲突"):
        fold_events(
            [Item("e1", T0), Item("e1", T0 + timedelta(seconds=1))],
            lambda item: (item.when,),
        )


def test_fold_streams_preserves_submission_order():
    assert list(fold_streams([1, 2], [3])) == [1, 2, 3]
    assert list(fold_streams()) == []


# ---------------------------------------------------------------------------
# /experiments/attribute：乱序、等价时区、键序无关（归因无 JSON 对象字段，
# 键序语义在 reconcile 的 context 上覆盖）、冲突重投
# ---------------------------------------------------------------------------


def test_attribute_out_of_order_exact_reposts_dedupe(client):
    base_exposures = [exposure("e1", "2026-03-01T00:00:00Z")]
    base_conversions = [conversion("c1", "2026-03-01T00:10:00Z")]
    body = attr_payload(
        exposures=list(reversed(base_exposures * 3)),
        conversions=list(reversed(base_conversions * 3)),
    )
    resp = client.post(ATTRIBUTE_URL, json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["attributions"]) == 1
    assert data["variants"][0]["exposed_users"] == 1


def test_attribute_equivalent_timezone_duplicate_dedupes(client):
    # Z 与 +01:00 表示同一 UTC 时刻：跨曝光/转化各自重投均折叠为一条。
    body = attr_payload(
        exposures=[
            exposure("e1", "2026-03-01T00:00:00Z"),
            exposure("e1", "2026-03-01T01:00:00+01:00"),
        ],
        conversions=[
            conversion("c1", "2026-03-01T00:10:00Z"),
            conversion("c1", "2026-03-01T08:10:00+08:00"),
        ],
    )
    resp = client.post(ATTRIBUTE_URL, json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["attributions"]) == 1
    assert data["attributions"][0]["status"] == "attributed"
    assert data["variants"][0]["exposed_users"] == 1


def test_attribute_conflicting_repost_is_422(client):
    # 同 event_id 字段冲突（variant 不同）。
    resp = client.post(
        ATTRIBUTE_URL,
        json=attr_payload(
            exposures=[
                exposure("e1", "2026-03-01T00:00:00Z", variant="on"),
                exposure("e1", "2026-03-01T00:00:00Z", variant="off"),
            ],
            conversions=[],
        ),
    )
    assert resp.status_code == 422

    # 不同 UTC 时刻的同 ID 重投同样冲突（即便用时区写法伪装）。
    resp = client.post(
        ATTRIBUTE_URL,
        json=attr_payload(
            exposures=[
                exposure("e1", "2026-03-01T00:00:00Z"),
                exposure("e1", "2026-03-01T01:00:01+01:00"),
            ],
            conversions=[],
        ),
    )
    assert resp.status_code == 422


def test_attribute_cross_type_same_id_is_422(client):
    # 曝光与转化共用 event_id：跨类型冲突。
    resp = client.post(
        ATTRIBUTE_URL,
        json=attr_payload(
            exposures=[exposure("x", "2026-03-01T00:00:00Z")],
            conversions=[conversion("x", "2026-03-01T00:00:00Z")],
        ),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /sdk/reconcile：乱序、等价时区、context 键序、冲突重投
# ---------------------------------------------------------------------------


def test_reconcile_out_of_order_exact_reposts_dedupe(client):
    event = reconcile_event("e1", "2026-03-01T00:10:00Z")
    resp = client.post(RECONCILE_URL, json=reconcile_payload([event, event, event]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["event_count"] == 1
    assert len(data["results"]) == 1
    assert data["nodes"][0]["events"] == 1


def test_reconcile_equivalent_timezone_duplicate_dedupes(client):
    # Z 与 +08:00 同一 UTC 时刻（naive 视为 UTC 由入口原契约覆盖）。
    utc = reconcile_event("e1", "2026-03-01T00:01:40Z")
    offset = reconcile_event("e1", "2026-03-01T08:01:40+08:00")
    resp = client.post(RECONCILE_URL, json=reconcile_payload([offset, utc]))
    assert resp.status_code == 200
    assert resp.json()["event_count"] == 1
    # 响应时间统一为 UTC 的 Z 写法。
    assert resp.json()["results"][0]["timestamp"] == "2026-03-01T00:01:40Z"


def test_reconcile_context_key_order_duplicate_dedupes(client):
    first = reconcile_event(
        "e1",
        "2026-03-01T00:10:00Z",
        context={"tier": "gold", "bucket": {"a": 1, "b": [1, 2]}},
    )
    second = reconcile_event(
        "e1",
        "2026-03-01T00:10:00Z",
        context={"bucket": {"b": [1, 2], "a": 1}, "tier": "gold"},
    )
    resp = client.post(RECONCILE_URL, json=reconcile_payload([second, first]))
    assert resp.status_code == 200
    assert resp.json()["event_count"] == 1


def test_reconcile_conflicting_repost_is_422(client):
    # 字段冲突（variant 不同）。
    resp = client.post(
        RECONCILE_URL,
        json=reconcile_payload(
            [
                reconcile_event("e1", "2026-03-01T00:10:00Z", variant="on"),
                reconcile_event("e1", "2026-03-01T00:10:00Z", variant="off"),
            ]
        ),
    )
    assert resp.status_code == 422

    # context 值不同（键序相同、值不同）也冲突。
    resp = client.post(
        RECONCILE_URL,
        json=reconcile_payload(
            [
                reconcile_event("e1", "2026-03-01T00:10:00Z", context={"tier": "gold"}),
                reconcile_event("e1", "2026-03-01T00:10:00Z", context={"tier": "free"}),
            ]
        ),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /sdk/rollout-audit：乱序、精确重投、冲突重投、同 sequence 不同事件
# ---------------------------------------------------------------------------


def _promote_events() -> list[dict]:
    return [
        wave_started("s1", 1),
        node_completed("n1", 2),
        metric_batch("b1", 3, 1),
        metric_batch("b2", 4, 2),
        metric_batch("b3", 5, 3),
    ]


def test_audit_out_of_order_exact_reposts_dedupe(client):
    events = list(reversed(_promote_events()))
    # 每条再追加一次乱序精确重投。
    events += list(reversed(events))
    resp = client.post(AUDIT_URL, json=audit_payload(events))
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["state"]["status"] == "completed"
    assert data["state"]["replayed_event_count"] == 5


def test_audit_conflicting_repost_is_422(client):
    # 同 event_id 但 sequence 不同 → 冲突重投。
    resp = client.post(
        AUDIT_URL,
        json=audit_payload([wave_started("s1", 1), wave_started("s1", 2)]),
    )
    assert resp.status_code == 422

    # 同 event_id 跨类型重投也冲突。
    resp = client.post(
        AUDIT_URL,
        json=audit_payload(
            [wave_started("s1", 1), node_completed("s1", 1)]
        ),
    )
    assert resp.status_code == 422


def test_audit_same_sequence_distinct_events_is_422_after_folding(client):
    # 折叠后 sequence 必须唯一：先放一对精确重投（折叠后只剩 1 条，合法），
    # 再放另一 event_id 同 sequence 的不同事件（折叠后仍重复 → 422）。
    duplicate_pair = [wave_started("s1", 1), wave_started("s1", 1)]
    resp = client.post(AUDIT_URL, json=audit_payload(duplicate_pair))
    assert resp.status_code == 200
    assert resp.json()["state"]["replayed_event_count"] == 1

    conflicting = duplicate_pair + [node_completed("n1", 1)]
    resp = client.post(AUDIT_URL, json=audit_payload(conflicting))
    assert resp.status_code == 422


def test_audit_first_violation_pre_state_unaffected_by_exact_reposts(client):
    # 乱序精确重投不应改变“首个违规 + 事前状态”：
    # node_completed 先于波启动违规；其前后各放一个同内容重投。
    bad = node_completed("n1", 1)
    events = [bad, bad, wave_started("s-late", 2), bad]
    resp = client.post(AUDIT_URL, json=audit_payload(events))
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["violation"]["kind"] == "node_before_wave_start"
    # 违规事件（含其重投）不落状态，事前 0 条已重放。
    assert data["state"]["replayed_event_count"] == 0
    assert data["state"]["current_wave"] is None
