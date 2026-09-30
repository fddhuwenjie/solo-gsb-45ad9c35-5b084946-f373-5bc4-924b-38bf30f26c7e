"""SDK 决策日志核对测试：±偏差区间边界、多版切换、决策重算比对、去重与 422 校验。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.artifacts import config_digest
from app.models import (
    Allocation,
    Condition,
    ConfigSnapshot,
    FlagConfig,
    NodeDecisionEvent,
    Operator,
    ReconcileClassification,
    ReconcileRequest,
    RecomputedDecision,
    Rule,
    Switch,
    Variant,
)
from app.reconcile import ReconcileRequestError, reconcile

URL = "/sdk/reconcile"
EVALUATE_URL = "/evaluate"
SWITCH = "new_checkout"

T0 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
GRACE = 300  # 宽限 5 分钟
SKEW = 60  # 时钟偏差 1 分钟


def make_config(version: int) -> FlagConfig:
    """单开关：tier=gold 命中后 100% on，否则默认 off；version 区分各版摘要。"""
    return FlagConfig(
        version=version,
        switches=[
            Switch(
                id=SWITCH,
                default_variant="off",
                variants=[Variant(id="on"), Variant(id="off")],
                rules=[
                    Rule(
                        id="gold_users",
                        conditions=[
                            Condition(
                                attribute="tier",
                                operator=Operator.EQ,
                                value="gold",
                            )
                        ],
                        allocations=[Allocation(variant_id="on", weight=10_000)],
                    )
                ],
            )
        ],
    )


# 三版配置：决策逻辑相同（gold -> on），仅 version 不同从而摘要不同。
V1 = make_config(version=1)
V2 = make_config(version=2)
V3 = make_config(version=3)
D1 = config_digest(V1.model_dump(mode="json"))
D2 = config_digest(V2.model_dump(mode="json"))
D3 = config_digest(V3.model_dump(mode="json"))
UNKNOWN = "f" * 64

# 激活时刻：V1 @ T0，V2 @ T0+3600，V3 @ T0+7200。
# 允许区间：D1 = [T0, T0+3900)，D2 = [T0+3600, T0+7500)，D3 = [T0+7200, +∞)。
T1 = T0 + timedelta(seconds=3600)
T2 = T0 + timedelta(seconds=7200)


def ts(offset: int) -> datetime:
    return T0 + timedelta(seconds=offset)


def snap(config: FlagConfig, at: datetime) -> ConfigSnapshot:
    return ConfigSnapshot(config=config, activated_at=at)


def three_snapshots() -> list[ConfigSnapshot]:
    return [snap(V1, T0), snap(V2, T1), snap(V3, T2)]


def event(
    event_id: str,
    *,
    node: str = "node-a",
    at: datetime,
    digest: str,
    user: str = "user-1",
    tier: str = "gold",
    variant: str = "on",
    served_default: bool = False,
    reason: str | None = None,
    switch: str = SWITCH,
) -> NodeDecisionEvent:
    return NodeDecisionEvent(
        event_id=event_id,
        node_id=node,
        timestamp=at,
        digest=digest,
        switch_id=switch,
        user_id=user,
        context={"tier": tier},
        variant_id=variant,
        served_default=served_default,
        reason=reason,
    )


def make_request(
    snapshots: list[ConfigSnapshot],
    events: list[NodeDecisionEvent],
    *,
    grace: int = GRACE,
    skew: int = SKEW,
) -> ReconcileRequest:
    return ReconcileRequest(
        snapshots=snapshots,
        grace_seconds=grace,
        clock_skew_seconds=skew,
        events=events,
    )


def payload(request: ReconcileRequest) -> dict:
    return request.model_dump(mode="json")


def by_id(resp) -> dict[str, object]:
    return {item.event_id: item for item in resp.results}


# ---------------------------------------------------------------------------
# 时间分类：±偏差区间与允许区间的边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "offset,digest,expected",
    [
        # D1 = [0, 3900)：±60s 区间完全落入。
        (1800, D1, ReconcileClassification.CONSISTENT),
        # 区间右端越过宽限终点 3900（[3820, 3940]）→ 相交。
        (3880, D1, ReconcileClassification.AMBIGUOUS_TIME),
        # 区间左端恰好落在宽限终点（[3900, 4020]）→ 整体在后。
        (3960, D1, ReconcileClassification.STALE_VERSION),
        # D2 = [3600, 7500)：区间右端 3599 < 3600 → 整体在前。
        (3539, D2, ReconcileClassification.EARLY_VERSION),
        # 区间右端恰好触及激活时刻 3600（[3480, 3600]）→ 相交。
        (3540, D2, ReconcileClassification.AMBIGUOUS_TIME),
        # 区间左端恰好落在激活时刻（[3600, 3720]）→ 完全落入。
        (3660, D2, ReconcileClassification.CONSISTENT),
        # 区间右端越过 D2 宽限终点 7500 → 相交。
        (7470, D2, ReconcileClassification.AMBIGUOUS_TIME),
        # 区间左端 7500 起 → 整体在后。
        (7560, D2, ReconcileClassification.STALE_VERSION),
        # D3 = [7200, +∞)：±60s 跨过激活时刻 → 相交。
        (7200, D3, ReconcileClassification.AMBIGUOUS_TIME),
        (7260, D3, ReconcileClassification.CONSISTENT),
        # 最后一版无上界：一年后仍一致。
        (365 * 86400, D3, ReconcileClassification.CONSISTENT),
        # 早于首版激活：区间右端 T0-1 < T0 → 在前；触及 T0 → 相交。
        (-61, D1, ReconcileClassification.EARLY_VERSION),
        (-30, D1, ReconcileClassification.AMBIGUOUS_TIME),
        # 在 V1 生效期上报尚未激活的 V3 → 在前。
        (100, D3, ReconcileClassification.EARLY_VERSION),
    ],
)
def test_time_classification(offset, digest, expected):
    req = make_request(three_snapshots(), [event("e1", at=ts(offset), digest=digest)])
    resp = reconcile(req)
    assert resp.event_count == 1
    assert resp.results[0].classification is expected


def test_grace_end_is_exclusive_with_zero_skew():
    # 零偏差下恰好“下一版激活 + 宽限”：允许区间右端不含 → stale_version。
    at_end = event("e_end", at=ts(3900), digest=D1)
    just_in = event("e_in", at=ts(3899), digest=D1)
    resp = reconcile(make_request(three_snapshots(), [at_end, just_in], skew=0))
    items = by_id(resp)
    assert items["e_end"].classification is ReconcileClassification.STALE_VERSION
    assert items["e_in"].classification is ReconcileClassification.CONSISTENT


def test_activation_start_is_inclusive_with_zero_skew():
    at_start = event("e_on", at=ts(3600), digest=D2)
    just_before = event("e_off", at=ts(3599), digest=D2)
    resp = reconcile(make_request(three_snapshots(), [at_start, just_before], skew=0))
    items = by_id(resp)
    assert items["e_on"].classification is ReconcileClassification.CONSISTENT
    assert items["e_off"].classification is ReconcileClassification.EARLY_VERSION


def test_zero_skew_never_ambiguous():
    # 零偏差时事件为单个时刻：只可能落入/在前/在后，不会相交。
    offsets = [-1, 0, 1800, 3599, 3600, 3899, 3900, 7500, 10_000]
    events = [event(f"e{i}", at=ts(o), digest=D1) for i, o in enumerate(offsets)]
    resp = reconcile(make_request(three_snapshots(), events, skew=0))
    for item in resp.results:
        assert item.classification is not ReconcileClassification.AMBIGUOUS_TIME


def test_grace_overlap_allows_both_versions():
    # 宽限重叠区 [3600, 3900) 内，新旧两版均为合法上报。
    old = event("e_old", at=ts(3700), digest=D1)
    new = event("e_new", at=ts(3700), digest=D2)
    resp = reconcile(make_request(three_snapshots(), [old, new]))
    items = by_id(resp)
    assert items["e_old"].classification is ReconcileClassification.CONSISTENT
    assert items["e_new"].classification is ReconcileClassification.CONSISTENT


def test_zero_grace_old_version_expires_at_next_activation():
    # 零宽限：旧版在下一版激活时刻即失效。
    old = event("e_old", at=ts(3600), digest=D1)
    resp = reconcile(make_request(three_snapshots(), [old], grace=0, skew=0))
    assert resp.results[0].classification is ReconcileClassification.STALE_VERSION


def test_single_snapshot_window_extends_forever():
    resp = reconcile(
        make_request([snap(V1, T0)], [event("e1", at=ts(365 * 86400), digest=D1)])
    )
    assert resp.results[0].classification is ReconcileClassification.CONSISTENT


def test_snapshots_are_sorted_by_activation():
    # 快照提交顺序不影响允许区间。
    shuffled = [snap(V3, T2), snap(V1, T0), snap(V2, T1)]
    events = [
        event("e1", at=ts(1800), digest=D1),
        event("e2", at=ts(3700), digest=D1),
        event("e3", at=ts(7260), digest=D3),
    ]
    ordered = reconcile(make_request(three_snapshots(), events))
    shuffled_resp = reconcile(make_request(shuffled, events))
    assert ordered.model_dump() == shuffled_resp.model_dump()


# ---------------------------------------------------------------------------
# 多版切换：事件落在哪一版窗口
# ---------------------------------------------------------------------------


def test_multi_version_switch_windows():
    events = [
        event("v1_era", at=ts(1800), digest=D1),  # V1 生效期
        event("v2_era", at=ts(5400), digest=D2),  # V2 生效期
        event("v3_era", at=ts(9000), digest=D3),  # V3 生效期
        # V2 生效期仍上报 V1，已超宽限 → stale。
        event("v1_late", at=ts(5400), digest=D1),
        # V1 生效期上报 V2（尚未激活）→ early。
        event("v2_early", at=ts(1800), digest=D2),
    ]
    resp = reconcile(make_request(three_snapshots(), events))
    items = by_id(resp)
    assert items["v1_era"].classification is ReconcileClassification.CONSISTENT
    assert items["v2_era"].classification is ReconcileClassification.CONSISTENT
    assert items["v3_era"].classification is ReconcileClassification.CONSISTENT
    assert items["v1_late"].classification is ReconcileClassification.STALE_VERSION
    assert items["v2_early"].classification is ReconcileClassification.EARLY_VERSION


def test_unknown_digest_skips_recompute():
    resp = reconcile(
        make_request(three_snapshots(), [event("e1", at=ts(100), digest=UNKNOWN)])
    )
    item = resp.results[0]
    assert item.classification is ReconcileClassification.UNKNOWN_DIGEST
    assert item.recomputed is None
    assert item.decision_mismatch is None


def test_unknown_digest_event_may_reference_any_switch():
    # 摘要不可识别时没有可校验的配置，开关引用不做检查。
    resp = reconcile(
        make_request(
            three_snapshots(),
            [event("e1", at=ts(100), digest=UNKNOWN, switch="ghost")],
        )
    )
    assert resp.results[0].classification is ReconcileClassification.UNKNOWN_DIGEST


# ---------------------------------------------------------------------------
# 决策重算比对
# ---------------------------------------------------------------------------


def test_matching_decision_is_not_mismatch():
    resp = reconcile(
        make_request(three_snapshots(), [event("e1", at=ts(100), digest=D1)])
    )
    item = resp.results[0]
    assert item.decision_mismatch is False
    assert item.recomputed == RecomputedDecision(
        variant_id="on", served_default=False, reason=None
    )


def test_variant_mismatch_flagged():
    # gold 用户重算为 on，上报 off → 不符。
    resp = reconcile(
        make_request(
            three_snapshots(), [event("e1", at=ts(100), digest=D1, variant="off")]
        )
    )
    item = resp.results[0]
    assert item.decision_mismatch is True
    assert item.recomputed.variant_id == "on"


def test_served_default_and_reason_mismatch_flagged():
    # free 用户重算为默认兜底 off / no_rule_matched。
    ok = event(
        "ok",
        at=ts(100),
        digest=D1,
        tier="free",
        variant="off",
        served_default=True,
        reason="no_rule_matched",
    )
    bad_reason = event(
        "bad_reason",
        at=ts(100),
        digest=D1,
        tier="free",
        variant="off",
        served_default=True,
        reason=None,
    )
    bad_default = event(
        "bad_default",
        at=ts(100),
        digest=D1,
        tier="free",
        variant="off",
        served_default=False,
        reason="no_rule_matched",
    )
    resp = reconcile(make_request(three_snapshots(), [ok, bad_reason, bad_default]))
    items = by_id(resp)
    assert items["ok"].decision_mismatch is False
    assert items["ok"].recomputed == RecomputedDecision(
        variant_id="off", served_default=True, reason="no_rule_matched"
    )
    assert items["bad_reason"].decision_mismatch is True
    assert items["bad_default"].decision_mismatch is True


def test_mismatch_checked_against_reported_snapshot_version():
    # 同一入参在不同版本下重算：上报值与所报版本的配置比对。
    # V2 把 gold 用户改为 100% off（仍走规则，非默认兜底）。
    v2_alt = FlagConfig(
        version=22,
        switches=[
            Switch(
                id=SWITCH,
                default_variant="off",
                variants=[Variant(id="on"), Variant(id="off")],
                rules=[
                    Rule(
                        id="gold_users",
                        conditions=[
                            Condition(
                                attribute="tier",
                                operator=Operator.EQ,
                                value="gold",
                            )
                        ],
                        allocations=[Allocation(variant_id="off", weight=10_000)],
                    )
                ],
            )
        ],
    )
    d_alt = config_digest(v2_alt.model_dump(mode="json"))
    snapshots = [snap(V1, T0), snap(v2_alt, T1)]
    # 在 V2 生效期上报 on：相对 V1 一致，相对 V2（所报版本）不符。
    resp = reconcile(
        make_request(
            snapshots,
            [event("e1", at=ts(3700), digest=d_alt, variant="on")],
        )
    )
    item = resp.results[0]
    assert item.classification is ReconcileClassification.CONSISTENT
    assert item.decision_mismatch is True
    assert item.recomputed.variant_id == "off"


def test_recomputed_matches_evaluate_endpoint(client):
    # 同一配置与入参：重算三元组与 /evaluate 一致。
    resp = reconcile(
        make_request(
            three_snapshots(),
            [event("e1", at=ts(100), digest=D1, user="user-777", tier="gold")],
        )
    )
    evaluated = client.post(
        EVALUATE_URL,
        json={
            "config": V1.model_dump(mode="json"),
            "switch_id": SWITCH,
            "user_id": "user-777",
            "context": {"tier": "gold"},
        },
    ).json()
    recomputed = resp.results[0].recomputed
    assert (recomputed.variant_id, recomputed.served_default, recomputed.reason) == (
        evaluated["variant_id"],
        evaluated["served_default"],
        evaluated["reason"],
    )


# ---------------------------------------------------------------------------
# 去重与内容冲突
# ---------------------------------------------------------------------------


def test_identical_duplicate_events_are_deduped():
    first = event("e1", at=ts(100), digest=D1)
    second = event("e1", at=ts(100), digest=D1)
    resp = reconcile(make_request(three_snapshots(), [first, second]))
    assert resp.event_count == 1
    assert len(resp.results) == 1
    assert resp.nodes[0].events == 1


def test_same_instant_different_offset_dedupes():
    # 两种时区写法表示同一 UTC 时刻，归一化后视为相同内容。
    utc = event("e1", at=ts(100), digest=D1)
    offset = NodeDecisionEvent(
        event_id="e1",
        node_id="node-a",
        timestamp=datetime(2026, 3, 1, 8, 1, 40, tzinfo=timezone(timedelta(hours=8))),
        digest=D1,
        switch_id=SWITCH,
        user_id="user-1",
        context={"tier": "gold"},
        variant_id="on",
        served_default=False,
        reason=None,
    )
    resp = reconcile(make_request(three_snapshots(), [utc, offset]))
    assert resp.event_count == 1


def test_conflicting_event_content_rejected():
    with pytest.raises(ValueError, match="内容冲突"):
        make_request(
            three_snapshots(),
            [
                event("e1", at=ts(100), digest=D1, variant="on"),
                event("e1", at=ts(100), digest=D1, variant="off"),
            ],
        )


def test_conflicting_event_node_or_time_rejected():
    with pytest.raises(ValueError, match="内容冲突"):
        make_request(
            three_snapshots(),
            [
                event("e1", node="node-a", at=ts(100), digest=D1),
                event("e1", node="node-b", at=ts(100), digest=D1),
            ],
        )
    with pytest.raises(ValueError, match="内容冲突"):
        make_request(
            three_snapshots(),
            [event("e1", at=ts(100), digest=D1), event("e1", at=ts(101), digest=D1)],
        )


def test_conflicting_event_digest_rejected():
    with pytest.raises(ValueError, match="内容冲突"):
        make_request(
            three_snapshots(),
            [event("e1", at=ts(100), digest=D1), event("e1", at=ts(100), digest=D2)],
        )


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------


def test_duplicate_activated_at_rejected():
    with pytest.raises(ValueError, match="activated_at"):
        make_request([snap(V1, T0), snap(V2, T0)], [])


def test_duplicate_snapshot_digest_rejected():
    # 同一配置再次激活（回滚）会让所报摘要的允许区间不唯一。
    with pytest.raises(ReconcileRequestError, match="摘要重复"):
        reconcile(make_request([snap(V1, T0), snap(V1, T1)], []))


def test_event_referencing_unknown_switch_in_snapshot_rejected():
    with pytest.raises(ReconcileRequestError, match="不存在"):
        reconcile(
            make_request(
                three_snapshots(),
                [event("e1", at=ts(100), digest=D1, switch="ghost")],
            )
        )


@pytest.mark.parametrize("field", ["grace_seconds", "clock_skew_seconds"])
def test_negative_durations_rejected(field):
    kwargs = {"grace_seconds": GRACE, "clock_skew_seconds": SKEW, field: -1}
    with pytest.raises(ValueError):
        ReconcileRequest(snapshots=three_snapshots(), **kwargs)


def test_empty_snapshots_rejected():
    with pytest.raises(ValueError):
        ReconcileRequest(
            snapshots=[], grace_seconds=GRACE, clock_skew_seconds=SKEW
        )


# ---------------------------------------------------------------------------
# 排序与节点计数
# ---------------------------------------------------------------------------


def test_results_sorted_and_node_counts():
    events = [
        event("e3", node="node-b", at=ts(100), digest=D1),
        event("e1", node="node-a", at=ts(200), digest=D1, variant="off"),  # 决策不符
        event("e2", node="node-a", at=ts(100), digest=D1),
        event("e4", node="node-b", at=ts(3960), digest=D1),  # stale
        event("e5", node="node-b", at=ts(100), digest=UNKNOWN),
    ]
    resp = reconcile(make_request(three_snapshots(), events))
    assert resp.event_count == 5
    assert [(item.node_id, item.event_id) for item in resp.results] == [
        ("node-a", "e2"),
        ("node-a", "e1"),
        ("node-b", "e3"),
        ("node-b", "e5"),
        ("node-b", "e4"),
    ]
    assert [summary.node_id for summary in resp.nodes] == ["node-a", "node-b"]
    node_a, node_b = resp.nodes
    assert node_a.events == 2
    assert node_a.consistent == 2
    assert node_a.decision_mismatches == 1
    assert node_b.events == 3
    assert node_b.consistent == 1
    assert node_b.stale_version == 1
    assert node_b.unknown_digest == 1
    assert node_b.decision_mismatches == 0
    # 各分类计数合计等于事件数。
    for summary in resp.nodes:
        assert summary.events == (
            summary.consistent
            + summary.ambiguous_time
            + summary.early_version
            + summary.stale_version
            + summary.unknown_digest
        )


def test_out_of_order_input_produces_identical_output():
    events = [
        event("e1", node="node-b", at=ts(100), digest=D1),
        event("e2", node="node-a", at=ts(200), digest=D2),
        event("e3", node="node-a", at=ts(100), digest=D1),
    ]
    first = reconcile(make_request(three_snapshots(), events))
    second = reconcile(make_request(three_snapshots(), list(reversed(events))))
    assert first.model_dump() == second.model_dump()


def test_empty_events_returns_empty_lists():
    resp = reconcile(make_request(three_snapshots(), []))
    assert resp.event_count == 0
    assert resp.results == []
    assert resp.nodes == []


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def test_api_happy_path(client):
    req = make_request(three_snapshots(), [event("e1", at=ts(100), digest=D1)])
    resp = client.post(URL, json=payload(req))
    assert resp.status_code == 200
    assert resp.json() == {
        "event_count": 1,
        "results": [
            {
                "event_id": "e1",
                "node_id": "node-a",
                "timestamp": "2026-03-01T00:01:40Z",
                "digest": D1,
                "classification": "consistent",
                "decision_mismatch": False,
                "recomputed": {
                    "variant_id": "on",
                    "served_default": False,
                    "reason": None,
                },
            }
        ],
        "nodes": [
            {
                "node_id": "node-a",
                "events": 1,
                "consistent": 1,
                "ambiguous_time": 0,
                "early_version": 0,
                "stale_version": 0,
                "unknown_digest": 0,
                "decision_mismatches": 0,
            }
        ],
    }


def test_api_naive_timestamp_treated_as_utc(client):
    body = payload(make_request(three_snapshots(), [event("e1", at=ts(100), digest=D1)]))
    body["events"][0]["timestamp"] = "2026-03-01T00:01:40"
    resp = client.post(URL, json=body)
    assert resp.status_code == 200
    assert resp.json()["results"][0]["classification"] == "consistent"


def test_api_rejects_invalid_snapshot_config(client):
    bad = make_config(version=9)
    bad.switches[0].rules[0].allocations[0].weight = 1
    resp = client.post(URL, json=payload(make_request([snap(bad, T0)], [])))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "校验未通过" in detail["message"]
    assert any(e["code"] == "invalid_weight_sum" for e in detail["errors"])
    assert all(e["path"].startswith("snapshots[0]") for e in detail["errors"])


def test_api_rejects_duplicate_snapshot_digest(client):
    body = payload(make_request([snap(V1, T0), snap(V1, T1)], []))
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_event_with_unknown_switch(client):
    body = payload(
        make_request(
            three_snapshots(), [event("e1", at=ts(100), digest=D1, switch="ghost")]
        )
    )
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_conflicting_event_id(client):
    body = payload(make_request(three_snapshots(), [event("e1", at=ts(100), digest=D1)]))
    duplicate = dict(body["events"][0])
    duplicate["variant_id"] = "off"
    body["events"].append(duplicate)
    assert client.post(URL, json=body).status_code == 422


def test_api_dedupes_identical_events(client):
    body = payload(make_request(three_snapshots(), [event("e1", at=ts(100), digest=D1)]))
    body["events"].append(dict(body["events"][0]))
    resp = client.post(URL, json=body)
    assert resp.status_code == 200
    assert resp.json()["event_count"] == 1


@pytest.mark.parametrize("field", ["grace_seconds", "clock_skew_seconds"])
def test_api_rejects_negative_durations(client, field):
    body = payload(make_request(three_snapshots(), []))
    body[field] = -1
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_empty_snapshots(client):
    body = payload(make_request(three_snapshots(), []))
    body["snapshots"] = []
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_empty_ids(client):
    body = payload(make_request(three_snapshots(), [event("e1", at=ts(100), digest=D1)]))
    body["events"][0]["node_id"] = ""
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(three_snapshots(), [event("e1", at=ts(100), digest=D1)]))
    body["events"][0]["digest"] = ""
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(three_snapshots(), [event("e1", at=ts(100), digest=D1)]))
    body["events"][0]["event_id"] = ""
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_extra_fields(client):
    body = payload(make_request(three_snapshots(), []))
    body["surprise"] = True
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(three_snapshots(), []))
    body["snapshots"][0]["key_id"] = "key-a"
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(three_snapshots(), [event("e1", at=ts(100), digest=D1)]))
    body["events"][0]["channel"] = "sdk"
    assert client.post(URL, json=body).status_code == 422
