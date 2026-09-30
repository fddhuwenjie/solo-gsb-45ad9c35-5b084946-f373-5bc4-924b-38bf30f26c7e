"""实验归因测试：去重与内容冲突、最近曝光选择、归因窗口、变体切换洗脱、汇总与固定输出顺序。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.attribution import attribute_experiment
from app.models import (
    AttributionStatus,
    ConversionEvent,
    ExperimentAttributeRequest,
    ExposureEvent,
)

URL = "/experiments/attribute"
SWITCH = "new_checkout"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ts(seconds: int) -> datetime:
    return BASE + timedelta(seconds=seconds)


def exposure(
    event_id: str,
    user: str = "u1",
    at: int = 0,
    variant: str = "A",
) -> ExposureEvent:
    return ExposureEvent(
        event_id=event_id, user_id=user, timestamp=ts(at), variant_id=variant
    )


def conversion(
    event_id: str,
    user: str = "u1",
    at: int = 0,
    value: float = 1.0,
) -> ConversionEvent:
    return ConversionEvent(
        event_id=event_id, user_id=user, timestamp=ts(at), value=value
    )


def make_request(
    exposures: list[ExposureEvent] | None = None,
    conversions: list[ConversionEvent] | None = None,
    window: int = 1000,
    washout: int = 0,
    switch_id: str = SWITCH,
) -> ExperimentAttributeRequest:
    return ExperimentAttributeRequest(
        switch_id=switch_id,
        window_seconds=window,
        washout_seconds=washout,
        exposures=exposures or [],
        conversions=conversions or [],
    )


def payload(request: ExperimentAttributeRequest) -> dict:
    return request.model_dump(mode="json")


def by_id(resp) -> dict[str, object]:
    return {item.event_id: item for item in resp.attributions}


def variant_stats(resp) -> dict[str, tuple[int, int, float]]:
    return {
        item.variant_id: (item.exposed_users, item.conversions, item.value)
        for item in resp.variants
    }


# ---------------------------------------------------------------------------
# 基本归因与汇总
# ---------------------------------------------------------------------------


def test_basic_attribution_and_variant_stats():
    req = make_request(
        exposures=[
            exposure("e1", user="u1", at=0, variant="A"),
            exposure("e2", user="u2", at=0, variant="B"),
        ],
        conversions=[
            conversion("c1", user="u1", at=10, value=2.5),
            conversion("c2", user="u1", at=20, value=1.5),
            conversion("c3", user="u2", at=30, value=4.0),
        ],
    )
    resp = attribute_experiment(req)
    assert resp.switch_id == SWITCH
    assert variant_stats(resp) == {
        "A": (1, 2, 4.0),
        "B": (1, 1, 4.0),
    }
    assert [
        (a.event_id, a.status, a.exposure_event_id, a.variant_id)
        for a in resp.attributions
    ] == [
        ("c1", AttributionStatus.ATTRIBUTED, "e1", "A"),
        ("c2", AttributionStatus.ATTRIBUTED, "e1", "A"),
        ("c3", AttributionStatus.ATTRIBUTED, "e2", "B"),
    ]


def test_value_sum_only_counts_attributed_conversions():
    req = make_request(
        exposures=[exposure("e1", at=0, variant="A")],
        conversions=[
            conversion("c1", at=1, value=1.5),
            conversion("c2", at=2, value=2.25),
            conversion("c3", at=2000, value=8.0),  # 超窗：不计入
        ],
        window=100,
    )
    resp = attribute_experiment(req)
    assert variant_stats(resp) == {"A": (1, 2, 3.75)}


def test_zero_value_conversion_is_valid_and_counted():
    req = make_request(
        exposures=[exposure("e1", at=0)],
        conversions=[conversion("c1", at=1, value=0.0)],
    )
    resp = attribute_experiment(req)
    assert variant_stats(resp) == {"A": (1, 1, 0.0)}


def test_exposed_users_deduped_per_variant():
    req = make_request(
        exposures=[
            exposure("e1", user="u1", at=0, variant="A"),
            exposure("e2", user="u1", at=10, variant="A"),  # u1 重复曝光 A
            exposure("e3", user="u2", at=0, variant="A"),
            exposure("e4", user="u1", at=20, variant="B"),  # u1 也曝光过 B
        ],
    )
    resp = attribute_experiment(req)
    assert variant_stats(resp) == {"A": (2, 0, 0.0), "B": (1, 0, 0.0)}


def test_variant_with_exposures_but_no_conversions_still_listed():
    req = make_request(exposures=[exposure("e1", variant="A"), exposure("e2", variant="B")])
    resp = attribute_experiment(req)
    assert [v.variant_id for v in resp.variants] == ["A", "B"]
    assert all(v.conversions == 0 and v.value == 0.0 for v in resp.variants)


# ---------------------------------------------------------------------------
# 去重与内容冲突
# ---------------------------------------------------------------------------


def test_identical_duplicate_events_are_deduped():
    req = make_request(
        exposures=[exposure("e1", at=0, variant="A"), exposure("e1", at=0, variant="A")],
        conversions=[conversion("c1", at=10, value=2.0), conversion("c1", at=10, value=2.0)],
    )
    resp = attribute_experiment(req)
    assert len(resp.attributions) == 1
    assert variant_stats(resp) == {"A": (1, 1, 2.0)}


def test_same_instant_different_offset_dedupes_not_conflicts():
    # 两种时区写法表示同一 UTC 时刻，归一化后视为相同内容。
    e1 = ExposureEvent(
        event_id="e1",
        user_id="u1",
        timestamp=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        variant_id="A",
    )
    e2 = ExposureEvent(
        event_id="e1",
        user_id="u1",
        timestamp=datetime(2026, 1, 1, 1, 0, tzinfo=timezone(timedelta(hours=1))),
        variant_id="A",
    )
    resp = attribute_experiment(
        make_request(exposures=[e1, e2], conversions=[conversion("c1", at=10)])
    )
    assert variant_stats(resp) == {"A": (1, 1, 1.0)}


def test_conflicting_exposure_variant_rejected():
    with pytest.raises(ValueError, match="内容冲突"):
        make_request(exposures=[exposure("e1", variant="A"), exposure("e1", variant="B")])


def test_conflicting_exposure_user_rejected():
    with pytest.raises(ValueError, match="内容冲突"):
        make_request(exposures=[exposure("e1", user="u1"), exposure("e1", user="u2")])


def test_conflicting_conversion_value_or_time_rejected():
    with pytest.raises(ValueError, match="内容冲突"):
        make_request(
            conversions=[conversion("c1", value=1.0), conversion("c1", value=2.0)]
        )
    with pytest.raises(ValueError, match="内容冲突"):
        make_request(conversions=[conversion("c1", at=0), conversion("c1", at=5)])


def test_same_id_across_exposure_and_conversion_rejected():
    with pytest.raises(ValueError, match="内容冲突"):
        make_request(exposures=[exposure("x")], conversions=[conversion("x")])


# ---------------------------------------------------------------------------
# 最近曝光选择
# ---------------------------------------------------------------------------


def test_most_recent_exposure_wins():
    req = make_request(
        exposures=[exposure("e1", at=0, variant="A"), exposure("e2", at=50, variant="A")],
        conversions=[conversion("c1", at=60)],
    )
    resp = attribute_experiment(req)
    assert by_id(resp)["c1"].exposure_event_id == "e2"


def test_same_timestamp_exposures_largest_event_id_wins():
    req = make_request(
        exposures=[
            exposure("e-a", at=0, variant="A"),
            exposure("e-b", at=0, variant="B"),
        ],
        conversions=[conversion("c1", at=10)],
        washout=0,  # 关闭洗脱，隔离同一时刻 ID tiebreak
    )
    resp = attribute_experiment(req)
    item = by_id(resp)["c1"]
    assert item.status is AttributionStatus.ATTRIBUTED
    assert (item.exposure_event_id, item.variant_id) == ("e-b", "B")


def test_exposure_after_conversion_is_not_used():
    req = make_request(
        exposures=[exposure("e1", at=100, variant="A")],
        conversions=[conversion("c1", at=50)],
    )
    resp = attribute_experiment(req)
    item = by_id(resp)["c1"]
    assert item.status is AttributionStatus.NO_EXPOSURE
    assert item.exposure_event_id is None
    assert item.variant_id is None


def test_exposure_at_same_moment_as_conversion_is_not_later():
    req = make_request(
        exposures=[exposure("e1", at=50)],
        conversions=[conversion("c1", at=50)],
    )
    resp = attribute_experiment(req)
    assert by_id(resp)["c1"].status is AttributionStatus.ATTRIBUTED


def test_no_exposure_for_unknown_user_and_before_first_exposure():
    req = make_request(
        exposures=[exposure("e1", user="u1", at=100)],
        conversions=[
            conversion("c1", user="u2", at=10),  # 该用户无任何曝光
            conversion("c2", user="u1", at=50),  # 早于该用户首次曝光
        ],
    )
    resp = attribute_experiment(req)
    assert by_id(resp)["c1"].status is AttributionStatus.NO_EXPOSURE
    assert by_id(resp)["c2"].status is AttributionStatus.NO_EXPOSURE
    assert resp.variants == [] or all(v.conversions == 0 for v in resp.variants)


def test_conversions_without_any_exposures():
    req = make_request(conversions=[conversion("c1", at=0)])
    resp = attribute_experiment(req)
    assert resp.variants == []
    assert by_id(resp)["c1"].status is AttributionStatus.NO_EXPOSURE


# ---------------------------------------------------------------------------
# 归因窗口
# ---------------------------------------------------------------------------


def test_window_is_closed_interval():
    req = make_request(
        exposures=[exposure("e1", at=0)],
        conversions=[
            conversion("c_edge", at=100),  # 恰好窗口边界：仍可归因
            conversion("c_out", at=101),   # 超窗：expired
        ],
        window=100,
    )
    resp = attribute_experiment(req)
    assert by_id(resp)["c_edge"].status is AttributionStatus.ATTRIBUTED
    expired = by_id(resp)["c_out"]
    assert expired.status is AttributionStatus.EXPIRED
    assert expired.exposure_event_id is None
    assert expired.variant_id is None
    assert variant_stats(resp) == {"A": (1, 1, 1.0)}


def test_zero_window_requires_same_moment():
    req = make_request(
        exposures=[exposure("e1", at=10)],
        conversions=[conversion("c_same", at=10), conversion("c_late", at=11)],
        window=0,
    )
    resp = attribute_experiment(req)
    assert by_id(resp)["c_same"].status is AttributionStatus.ATTRIBUTED
    assert by_id(resp)["c_late"].status is AttributionStatus.EXPIRED


def test_expired_falls_back_to_no_other_exposure():
    # 更早的曝光同样超窗：最近曝光超窗即 expired，不会改归更早的曝光。
    req = make_request(
        exposures=[exposure("e1", at=0), exposure("e2", at=50)],
        conversions=[conversion("c1", at=200)],
        window=100,
    )
    resp = attribute_experiment(req)
    assert by_id(resp)["c1"].status is AttributionStatus.EXPIRED


# ---------------------------------------------------------------------------
# 变体切换与洗脱
# ---------------------------------------------------------------------------


def test_washout_interval_is_half_open():
    req = make_request(
        exposures=[
            exposure("e1", at=0, variant="A"),
            exposure("e2", at=100, variant="B"),  # A→B 切换，洗脱 [100, 150)
        ],
        conversions=[
            conversion("c_start", at=100, value=1.0),  # 区间起点：含
            conversion("c_mid", at=149, value=2.0),
            conversion("c_end", at=150, value=4.0),    # 区间终点：不含
        ],
        washout=50,
    )
    resp = attribute_experiment(req)
    items = by_id(resp)
    assert items["c_start"].status is AttributionStatus.CONTAMINATED
    assert items["c_mid"].status is AttributionStatus.CONTAMINATED
    assert items["c_end"].status is AttributionStatus.ATTRIBUTED
    # contaminated 仍给出匹配到的曝光，但不计入变体。
    assert (items["c_mid"].exposure_event_id, items["c_mid"].variant_id) == ("e2", "B")
    assert (items["c_end"].exposure_event_id, items["c_end"].variant_id) == ("e2", "B")
    assert variant_stats(resp) == {"A": (1, 0, 0.0), "B": (1, 1, 4.0)}


def test_zero_washout_never_contaminates():
    req = make_request(
        exposures=[
            exposure("e1", at=0, variant="A"),
            exposure("e2", at=10, variant="B"),
        ],
        conversions=[conversion("c1", at=10)],
        washout=0,
    )
    resp = attribute_experiment(req)
    item = by_id(resp)["c1"]
    assert item.status is AttributionStatus.ATTRIBUTED
    assert item.variant_id == "B"


def test_repeat_exposure_same_variant_is_not_a_switch():
    req = make_request(
        exposures=[exposure("e1", at=0, variant="A"), exposure("e2", at=10, variant="A")],
        conversions=[conversion("c1", at=15)],
        washout=100,
    )
    resp = attribute_experiment(req)
    assert by_id(resp)["c1"].status is AttributionStatus.ATTRIBUTED


def test_multiple_switches_create_separate_washout_intervals():
    req = make_request(
        exposures=[
            exposure("e1", at=0, variant="A"),
            exposure("e2", at=100, variant="B"),
            exposure("e3", at=200, variant="A"),
        ],
        conversions=[
            conversion("c1", at=110),  # B 洗脱 [100,130)
            conversion("c2", at=140),  # 归 B
            conversion("c3", at=210),  # A 洗脱 [200,230)
            conversion("c4", at=240),  # 归 A
        ],
        washout=30,
    )
    resp = attribute_experiment(req)
    assert {
        event_id: (item.status, item.variant_id)
        for event_id, item in by_id(resp).items()
    } == {
        "c1": (AttributionStatus.CONTAMINATED, "B"),
        "c2": (AttributionStatus.ATTRIBUTED, "B"),
        "c3": (AttributionStatus.CONTAMINATED, "A"),
        "c4": (AttributionStatus.ATTRIBUTED, "A"),
    }
    assert variant_stats(resp) == {"A": (1, 1, 1.0), "B": (1, 1, 1.0)}


def test_first_exposure_is_not_a_switch():
    req = make_request(
        exposures=[exposure("e1", at=0, variant="A")],
        conversions=[conversion("c1", at=5)],
        washout=100,
    )
    resp = attribute_experiment(req)
    assert by_id(resp)["c1"].status is AttributionStatus.ATTRIBUTED


def test_expired_takes_precedence_over_contaminated():
    # 洗脱区间比窗口更长：区间内但最近曝光已超窗 → expired（先判定能否归因）。
    req = make_request(
        exposures=[
            exposure("e1", at=0, variant="A"),
            exposure("e2", at=100, variant="B"),
        ],
        conversions=[conversion("c1", at=120)],
        window=10,
        washout=50,
    )
    resp = attribute_experiment(req)
    item = by_id(resp)["c1"]
    assert item.status is AttributionStatus.EXPIRED
    assert item.exposure_event_id is None
    assert all(v.conversions == 0 for v in resp.variants)


# ---------------------------------------------------------------------------
# 排序与确定性
# ---------------------------------------------------------------------------


def test_output_order_fixed_by_user_time_and_id():
    req = make_request(
        exposures=[
            exposure("e2", user="u2", at=0, variant="b"),
            exposure("e1", user="u1", at=0, variant="a"),
        ],
        conversions=[
            conversion("c9", user="u2", at=5),
            conversion("c2", user="u1", at=20),
            conversion("c1", user="u1", at=10),
        ],
    )
    resp = attribute_experiment(req)
    assert [item.event_id for item in resp.attributions] == ["c1", "c2", "c9"]
    assert [item.variant_id for item in resp.variants] == ["a", "b"]


def test_same_user_and_timestamp_ordered_by_event_id():
    req = make_request(
        exposures=[exposure("e1", at=0)],
        conversions=[conversion("c-b", at=10), conversion("c-a", at=10)],
    )
    resp = attribute_experiment(req)
    assert [item.event_id for item in resp.attributions] == ["c-a", "c-b"]


def test_out_of_order_input_produces_identical_output():
    exposures = [
        exposure("e3", user="u2", at=20, variant="B"),
        exposure("e1", user="u1", at=0, variant="A"),
        exposure("e2", user="u1", at=100, variant="B"),
    ]
    conversions = [
        conversion("c3", user="u2", at=30, value=3.0),
        conversion("c1", user="u1", at=10, value=1.0),
        conversion("c2", user="u1", at=110, value=2.0),
    ]
    first = attribute_experiment(
        make_request(exposures=exposures, conversions=conversions, washout=30)
    )
    second = attribute_experiment(
        make_request(
            exposures=list(reversed(exposures)),
            conversions=list(reversed(conversions)),
            washout=30,
        )
    )
    assert first.model_dump() == second.model_dump()


def test_empty_request_returns_empty_lists():
    resp = attribute_experiment(make_request())
    assert resp.switch_id == SWITCH
    assert resp.variants == []
    assert resp.attributions == []


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def test_api_happy_path(client):
    req = make_request(
        exposures=[exposure("e1", at=0, variant="on")],
        conversions=[conversion("c1", at=10, value=9.9)],
    )
    resp = client.post(URL, json=payload(req))
    assert resp.status_code == 200
    assert resp.json() == {
        "switch_id": SWITCH,
        "variants": [
            {"variant_id": "on", "exposed_users": 1, "conversions": 1, "value": 9.9}
        ],
        "attributions": [
            {
                "event_id": "c1",
                "user_id": "u1",
                "timestamp": "2026-01-01T00:00:10Z",
                "value": 9.9,
                "status": "attributed",
                "exposure_event_id": "e1",
                "variant_id": "on",
            }
        ],
    }


def test_api_contaminated_case(client):
    req = make_request(
        exposures=[
            exposure("e1", at=0, variant="off"),
            exposure("e2", at=100, variant="on"),
        ],
        conversions=[conversion("c1", at=120, value=5.0)],
        washout=50,
    )
    resp = client.post(URL, json=payload(req))
    assert resp.status_code == 200
    body = resp.json()
    assert body["attributions"][0]["status"] == "contaminated"
    assert body["attributions"][0]["exposure_event_id"] == "e2"
    assert {v["variant_id"]: v["conversions"] for v in body["variants"]} == {
        "off": 0,
        "on": 0,
    }


def test_api_dedupes_identical_events(client):
    req = make_request(
        exposures=[exposure("e1", at=0, variant="A")],
        conversions=[conversion("c1", at=10, value=2.0)],
    )
    body = payload(req)
    body["exposures"].append(body["exposures"][0])
    body["conversions"].append(body["conversions"][0])
    resp = client.post(URL, json=body)
    assert resp.status_code == 200
    assert len(resp.json()["attributions"]) == 1
    assert resp.json()["variants"][0]["exposed_users"] == 1


def test_api_rejects_conflicting_event_id(client):
    body = payload(make_request(exposures=[exposure("e1", variant="A")]))
    body["exposures"].append(
        {
            "event_id": "e1",
            "user_id": "u1",
            "timestamp": "2026-01-01T00:00:00Z",
            "variant_id": "B",
        }
    )
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request())
    body["exposures"] = [
        {
            "event_id": "e1",
            "user_id": "u1",
            "timestamp": "2026-01-01T00:00:00Z",
            "variant_id": "A",
        }
    ]
    body["conversions"] = [
        {
            "event_id": "e1",
            "user_id": "u1",
            "timestamp": "2026-01-01T00:00:10Z",
            "value": 1.0,
        }
    ]
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_negative_value(client):
    body = payload(make_request(conversions=[conversion("c1", value=1.0)]))
    body["conversions"][0]["value"] = -0.01
    assert client.post(URL, json=body).status_code == 422


@pytest.mark.parametrize("field", ["window_seconds", "washout_seconds"])
def test_api_rejects_negative_durations(client, field):
    body = payload(make_request())
    body[field] = -1
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_empty_ids(client):
    body = payload(make_request())
    body["switch_id"] = ""
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(exposures=[exposure("e1")]))
    body["exposures"][0]["event_id"] = ""
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(exposures=[exposure("e1")]))
    body["exposures"][0]["variant_id"] = ""
    assert client.post(URL, json=body).status_code == 422


def test_api_rejects_extra_fields(client):
    body = payload(make_request())
    body["surprise"] = True
    assert client.post(URL, json=body).status_code == 422

    body = payload(make_request(exposures=[exposure("e1")]))
    body["exposures"][0]["channel"] = "push"
    assert client.post(URL, json=body).status_code == 422


def test_api_naive_timestamp_treated_as_utc(client):
    body = payload(
        make_request(
            exposures=[exposure("e1", at=0)],
            conversions=[conversion("c1", at=10)],
        )
    )
    body["exposures"][0]["timestamp"] = "2026-01-01T00:00:00"
    body["conversions"][0]["timestamp"] = "2026-01-01T00:00:10"
    resp = client.post(URL, json=body)
    assert resp.status_code == 200
    assert resp.json()["attributions"][0]["status"] == "attributed"


def test_api_output_order_independent_of_submission_order(client):
    req = make_request(
        exposures=[
            exposure("e2", user="u2", at=0, variant="b"),
            exposure("e1", user="u1", at=0, variant="a"),
        ],
        conversions=[
            conversion("c9", user="u2", at=5),
            conversion("c1", user="u1", at=10),
        ],
    )
    body = client.post(URL, json=payload(req)).json()
    assert [item["event_id"] for item in body["attributions"]] == ["c1", "c9"]
    assert [item["variant_id"] for item in body["variants"]] == ["a", "b"]
