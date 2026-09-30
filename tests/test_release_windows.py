"""POST /release-windows/resolve 测试：跨午夜、春季跳时、秋季重复、黑窗切割。"""

from __future__ import annotations

import pytest


def weekly(rule_id: str, weekday: int, start_local: str, end_local: str) -> dict:
    return {
        "rule_id": rule_id,
        "weekday": weekday,
        "start_local": start_local,
        "end_local": end_local,
    }


def calendar(
    calendar_id: str,
    timezone: str,
    windows: list[dict] | None = None,
    blackouts: list[dict] | None = None,
) -> dict:
    return {
        "calendar_id": calendar_id,
        "timezone": timezone,
        "weekly_windows": windows or [],
        "blackouts": blackouts or [],
    }


def request_body(
    start: str,
    end: str,
    calendars: list[dict],
    min_duration_seconds: int = 1,
) -> dict:
    return {
        "start": start,
        "end": end,
        "min_duration_seconds": min_duration_seconds,
        "calendars": calendars,
    }


def resolve(client, body):
    response = client.post("/release-windows/resolve", json=body)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 跨午夜
# ---------------------------------------------------------------------------


def test_cross_midnight_window_expands_into_next_utc_day(client):
    """纽约周六 20:00→次日 02:00（跨午夜）= UTC 周日 01:00→07:00。"""
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-09T00:00:00Z",
        [calendar("us", "America/New_York", [weekly("us-sat", 5, "20:00", "02:00")])],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"], w["duration_seconds"]) for w in data["windows"]] == [
        ("2026-02-08T01:00:00Z", "2026-02-08T07:00:00Z", 6 * 3600)
    ]
    window = data["windows"][0]
    assert window["contributors"] == [
        {"calendar_id": "us", "rule_ids": ["us-sat"]}
    ]
    assert window["dst_adjustments"] == []


def test_cross_midnight_far_east_timezone(client):
    """东京周五 23:00→次日 01:00：起讫都在 UTC 周六，跨 UTC 午夜边界展开。"""
    body = request_body(
        "2026-02-06T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("jp", "Asia/Tokyo", [weekly("jp-fri", 4, "23:00", "01:00")])],
    )
    data = resolve(client, body)

    # 周五 23:00 JST = 周五 14:00 UTC；周六 01:00 JST = 周五 16:00 UTC。
    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-02-06T14:00:00Z", "2026-02-06T16:00:00Z")
    ]


def test_query_range_is_half_open(client):
    """恰在查询 end 处开始的窗口不纳入；落在 [start,end) 内的部分被裁剪。"""
    # 纽约周六 12:00→20:00 = UTC 17:00→次日 01:00；查询止于 UTC 23:00。
    body = request_body(
        "2026-02-07T18:00:00Z",
        "2026-02-07T23:00:00Z",
        [calendar("us", "America/New_York", [weekly("w", 5, "12:00", "20:00")])],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-02-07T18:00:00Z", "2026-02-07T23:00:00Z")
    ]


# ---------------------------------------------------------------------------
# 春季跳时（gap）
# ---------------------------------------------------------------------------


def test_spring_forward_nonexistent_start_shifts_forward(client):
    """2026-03-08 纽约 02:00→03:00 不存在：开始 02:00 顺延到 03:00（07:00Z）。"""
    body = request_body(
        "2026-03-08T00:00:00Z",
        "2026-03-08T12:00:00Z",
        [calendar("us", "America/New_York", [weekly("w", 6, "02:00", "04:00")])],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"], w["duration_seconds"]) for w in data["windows"]] == [
        ("2026-03-08T07:00:00Z", "2026-03-08T08:00:00Z", 3600)
    ]
    adjustment = data["windows"][0]["dst_adjustments"][0]
    assert adjustment == {
        "calendar_id": "us",
        "rule_id": "w",
        "boundary": "start",
        "kind": "gap_shifted",
        "local_time": "2026-03-08T02:00:00",
        "resolved_at": "2026-03-08T07:00:00Z",
    }


def test_spring_forward_nonexistent_end_shifts_forward(client):
    """结束落在 gap（02:30）也顺延；窗口实际长度缩短为 1 小时。"""
    body = request_body(
        "2026-03-08T00:00:00Z",
        "2026-03-08T12:00:00Z",
        [calendar("us", "America/New_York", [weekly("w", 6, "01:30", "02:30")])],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-03-08T06:30:00Z", "2026-03-08T07:00:00Z")
    ]
    adjustment = data["windows"][0]["dst_adjustments"][0]
    assert adjustment["boundary"] == "end"
    assert adjustment["kind"] == "gap_shifted"
    assert adjustment["local_time"] == "2026-03-08T02:30:00"
    assert adjustment["resolved_at"] == "2026-03-08T07:00:00Z"


def test_spring_forward_collapsed_occurrence_is_dropped(client):
    """开始顺延后不早于结束 → 区间坍塌为空，该次出现静默丢弃。"""
    body = request_body(
        "2026-03-08T00:00:00Z",
        "2026-03-08T12:00:00Z",
        [calendar("us", "America/New_York", [weekly("w", 6, "02:30", "03:00")])],
    )
    data = resolve(client, body)
    assert data["windows"] == []


def test_spring_forward_window_skips_gap_without_report_when_unaffected(client):
    """两端都在 gap 外（01:30→03:30）时不产生调整记录，时长被吃掉 1 小时。"""
    body = request_body(
        "2026-03-08T00:00:00Z",
        "2026-03-08T12:00:00Z",
        [calendar("us", "America/New_York", [weekly("w", 6, "01:30", "03:30")])],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"], w["duration_seconds"]) for w in data["windows"]] == [
        ("2026-03-08T06:30:00Z", "2026-03-08T07:30:00Z", 3600)
    ]
    assert data["windows"][0]["dst_adjustments"] == []


# ---------------------------------------------------------------------------
# 秋季重复（fold）
# ---------------------------------------------------------------------------


def test_fall_back_start_earlier_end_later(client):
    """2026-11-01 纽约 01:00/01:30 重复：开始取较早（05:00Z），结束取较晚（06:30Z）。"""
    body = request_body(
        "2026-11-01T00:00:00Z",
        "2026-11-01T12:00:00Z",
        [calendar("us", "America/New_York", [weekly("w", 6, "01:00", "01:30")])],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"], w["duration_seconds"]) for w in data["windows"]] == [
        ("2026-11-01T05:00:00Z", "2026-11-01T06:30:00Z", 5400)
    ]
    adjustments = data["windows"][0]["dst_adjustments"]
    assert [(a["boundary"], a["kind"], a["resolved_at"]) for a in adjustments] == [
        ("start", "ambiguous_earlier", "2026-11-01T05:00:00Z"),
        ("end", "ambiguous_later", "2026-11-01T06:30:00Z"),
    ]


def test_fall_back_duplicate_hour_is_double_counted(client):
    """跨过整个重复小时的窗口比平时长 1 小时：00:30→02:30 = 3 小时。"""
    body = request_body(
        "2026-11-01T00:00:00Z",
        "2026-11-01T12:00:00Z",
        [calendar("us", "America/New_York", [weekly("w", 6, "00:30", "02:30")])],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"], w["duration_seconds"]) for w in data["windows"]] == [
        ("2026-11-01T04:30:00Z", "2026-11-01T07:30:00Z", 10800)
    ]


# ---------------------------------------------------------------------------
# 重复边界：开始取较早、结束取较晚
# ---------------------------------------------------------------------------


def test_duplicate_boundaries_merge_earliest_start_latest_end(client):
    """同一 weekday 两条规则在春季跳时日共享 02:00 开始：合并取顺延时刻，
    结束取较晚；合并段上两条规则都作为贡献，且调整去重。"""
    body = request_body(
        "2026-03-08T00:00:00Z",
        "2026-03-08T12:00:00Z",
        [
            calendar(
                "us",
                "America/New_York",
                [
                    weekly("w-a", 6, "02:00", "04:00"),
                    weekly("w-b", 6, "02:00", "05:00"),
                ],
            )
        ],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-03-08T07:00:00Z", "2026-03-08T09:00:00Z")
    ]
    window = data["windows"][0]
    assert window["contributors"] == [
        {"calendar_id": "us", "rule_ids": ["w-a", "w-b"]}
    ]
    # 两条规则的开始都从同一不存在时刻顺延到同一 UTC 时刻；调整按
    # (resolved_at, calendar, rule, boundary) 区分，因此保留两条。
    assert [
        (a["rule_id"], a["boundary"], a["resolved_at"])
        for a in window["dst_adjustments"]
    ] == [
        ("w-a", "start", "2026-03-08T07:00:00Z"),
        ("w-b", "start", "2026-03-08T07:00:00Z"),
    ]


def test_adjacent_occurrences_merge_with_rules_union(client):
    """端点相接（相邻）的两个出现合并为一段，规则取并集。"""
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [
            calendar(
                "us",
                "America/New_York",
                [
                    weekly("w1", 5, "12:00", "13:00"),  # 17:00→18:00Z
                    weekly("w2", 5, "13:00", "14:00"),  # 18:00→19:00Z
                ],
            )
        ],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-02-07T17:00:00Z", "2026-02-07T19:00:00Z")
    ]
    assert data["windows"][0]["contributors"][0]["rule_ids"] == ["w1", "w2"]


# ---------------------------------------------------------------------------
# 黑窗切割
# ---------------------------------------------------------------------------


def test_blackout_in_middle_splits_window(client):
    """UTC 黑窗落在可用区间中部，切成两段，两段都带原周期规则出处。"""
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [
            calendar(
                "us",
                "America/New_York",
                [weekly("w", 5, "12:00", "18:00")],  # 17:00→23:00Z
                [
                    {
                        "rule_id": "freeze",
                        "start": "2026-02-07T19:00:00Z",
                        "end": "2026-02-07T21:00:00Z",
                    }
                ],
            )
        ],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-02-07T17:00:00Z", "2026-02-07T19:00:00Z"),
        ("2026-02-07T21:00:00Z", "2026-02-07T23:00:00Z"),
    ]
    for window in data["windows"]:
        # 黑窗规则不作为正向贡献；每段都保留周期规则。
        assert window["contributors"] == [
            {"calendar_id": "us", "rule_ids": ["w"]}
        ]


def test_blackout_touching_edge_trims_window(client):
    """黑窗与可用区间端点相接时裁掉相接的一侧；半开相接不产生空段。"""
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [
            calendar(
                "us",
                "America/New_York",
                [weekly("w", 5, "12:00", "18:00")],  # 17:00→23:00Z
                [
                    {
                        "rule_id": "freeze",
                        "start": "2026-02-07T22:00:00Z",
                        "end": "2026-02-07T23:00:00Z",
                    }
                ],
            )
        ],
    )
    data = resolve(client, body)
    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-02-07T17:00:00Z", "2026-02-07T22:00:00Z")
    ]


def test_overlapping_blackouts_are_merged(client):
    """相互重叠的黑窗先合并，中间不留可发布缝隙。"""
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [
            calendar(
                "us",
                "America/New_York",
                [weekly("w", 5, "12:00", "18:00")],
                [
                    {
                        "rule_id": "b1",
                        "start": "2026-02-07T19:00:00Z",
                        "end": "2026-02-07T20:00:00Z",
                    },
                    {
                        "rule_id": "b2",
                        "start": "2026-02-07T19:30:00Z",
                        "end": "2026-02-07T21:00:00Z",
                    },
                ],
            )
        ],
    )
    data = resolve(client, body)
    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-02-07T17:00:00Z", "2026-02-07T19:00:00Z"),
        ("2026-02-07T21:00:00Z", "2026-02-07T23:00:00Z"),
    ]


def test_blackout_covering_everything_yields_empty(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [
            calendar(
                "us",
                "America/New_York",
                [weekly("w", 5, "12:00", "18:00")],
                [
                    {
                        "rule_id": "freeze",
                        "start": "2026-02-07T00:00:00Z",
                        "end": "2026-02-08T00:00:00Z",
                    }
                ],
            )
        ],
    )
    assert resolve(client, body)["windows"] == []


# ---------------------------------------------------------------------------
# 多日历交集、裁剪、最短时长与排序
# ---------------------------------------------------------------------------


def test_intersection_of_two_calendars(client):
    """纽约 12:00→23:00（17:00→04:00Z 次日）与柏林周六 18:00→23:00
    （17:00→22:00Z）的共同部分是 17:00→22:00Z。"""
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-09T00:00:00Z",
        [
            calendar("us", "America/New_York", [weekly("us-w", 5, "12:00", "23:00")]),
            calendar("eu", "Europe/Berlin", [weekly("eu-w", 5, "18:00", "23:00")]),
        ],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-02-07T17:00:00Z", "2026-02-07T22:00:00Z")
    ]
    contributors = {c["calendar_id"]: c["rule_ids"] for c in data["windows"][0]["contributors"]}
    assert contributors == {"us": ["us-w"], "eu": ["eu-w"]}


def test_blackout_in_one_calendar_cuts_intersection(client):
    """任一日历的黑窗都会切割共同窗口，且各段贡献仍包含两份日历。"""
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-09T00:00:00Z",
        [
            calendar("us", "America/New_York", [weekly("us-w", 5, "12:00", "23:00")]),
            calendar(
                "eu",
                "Europe/Berlin",
                [weekly("eu-w", 5, "18:00", "23:00")],
                [
                    {
                        "rule_id": "eu-freeze",
                        "start": "2026-02-07T20:00:00Z",
                        "end": "2026-02-07T21:00:00Z",
                    }
                ],
            ),
        ],
    )
    data = resolve(client, body)

    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-02-07T17:00:00Z", "2026-02-07T20:00:00Z"),
        ("2026-02-07T21:00:00Z", "2026-02-07T22:00:00Z"),
    ]
    for window in data["windows"]:
        calendars = {c["calendar_id"] for c in window["contributors"]}
        assert calendars == {"us", "eu"}


def test_min_duration_filters_short_pieces(client):
    """黑窗切出的两段中，不足 2 小时的被过滤，长的保留。"""
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [
            calendar(
                "us",
                "America/New_York",
                [weekly("w", 5, "12:00", "18:00")],  # 17:00→23:00Z
                [
                    {
                        "rule_id": "freeze",
                        "start": "2026-02-07T19:00:00Z",
                        "end": "2026-02-07T21:30:00Z",
                    }
                ],
            )
        ],
        min_duration_seconds=2 * 3600,
    )
    data = resolve(client, body)
    # 第一段 2h 保留（闭下限），第二段 1.5h 被过滤。
    assert [(w["start"], w["end"]) for w in data["windows"]] == [
        ("2026-02-07T17:00:00Z", "2026-02-07T19:00:00Z")
    ]


def test_min_duration_boundary_is_inclusive(client):
    """恰好等于 min_duration_seconds 的窗口保留。"""
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("us", "America/New_York", [weekly("w", 5, "12:00", "15:00")])],
        min_duration_seconds=3 * 3600,
    )
    data = resolve(client, body)
    assert len(data["windows"]) == 1
    assert data["windows"][0]["duration_seconds"] == 3 * 3600


def test_windows_sorted_by_time(client):
    """多个出现按开始时间排序输出。"""
    body = request_body(
        "2026-02-05T00:00:00Z",
        "2026-02-09T00:00:00Z",
        [
            calendar(
                "utc",
                "UTC",
                [
                    weekly("fri", 4, "10:00", "11:00"),
                    weekly("sat", 5, "10:00", "11:00"),
                    weekly("sun", 6, "10:00", "11:00"),
                ],
            )
        ],
    )
    data = resolve(client, body)
    starts = [w["start"] for w in data["windows"]]
    assert starts == sorted(starts)
    assert starts == [
        "2026-02-06T10:00:00Z",
        "2026-02-07T10:00:00Z",
        "2026-02-08T10:00:00Z",
    ]


def test_max_query_span_31_days_allowed(client):
    """恰好 31 天的查询跨度合法。"""
    body = request_body(
        "2026-01-01T00:00:00Z",
        "2026-02-01T00:00:00Z",
        [calendar("utc", "UTC", [weekly("w", 0, "10:00", "11:00")])],
    )
    response = client.post("/release-windows/resolve", json=body)
    assert response.status_code == 200


def test_empty_weekly_windows_means_never_available(client):
    """日历没有周期窗口 → 永远不可发布（合法请求，空结果，不是 422）。"""
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("us", "America/New_York")],
    )
    assert resolve(client, body)["windows"] == []


def test_response_echoes_query_parameters(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("utc", "UTC", [weekly("w", 5, "10:00", "11:00")])],
        min_duration_seconds=900,
    )
    data = resolve(client, body)
    assert data["start"] == "2026-02-07T00:00:00Z"
    assert data["end"] == "2026-02-08T00:00:00Z"
    assert data["min_duration_seconds"] == 900


# ---------------------------------------------------------------------------
# 422 校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["start", "end"],
)
def test_naive_query_time_rejected(client, field):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("utc", "UTC")],
    )
    body[field] = "2026-02-07T00:00:00"  # naive
    response = client.post("/release-windows/resolve", json=body)
    assert response.status_code == 422


def test_non_utc_query_offset_rejected(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("utc", "UTC")],
    )
    body["start"] = "2026-02-07T08:00:00+08:00"
    response = client.post("/release-windows/resolve", json=body)
    assert response.status_code == 422


def test_non_utc_blackout_time_rejected(client):
    cal = calendar(
        "us",
        "America/New_York",
        [weekly("w", 5, "10:00", "11:00")],
        [{"rule_id": "b", "start": "2026-02-07T05:00:00-05:00",
          "end": "2026-02-07T06:00:00-05:00"}],
    )
    body = request_body("2026-02-07T00:00:00Z", "2026-02-08T00:00:00Z", [cal])
    response = client.post("/release-windows/resolve", json=body)
    assert response.status_code == 422


def test_invalid_timezone_rejected(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("us", "Mars/Olympus", [weekly("w", 5, "10:00", "11:00")])],
    )
    response = client.post("/release-windows/resolve", json=body)
    assert response.status_code == 422


def test_duplicate_rule_id_within_calendar_rejected(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [
            calendar(
                "utc",
                "UTC",
                [weekly("dup", 5, "10:00", "11:00"), weekly("dup", 6, "10:00", "11:00")],
            )
        ],
    )
    assert client.post("/release-windows/resolve", json=body).status_code == 422


def test_duplicate_rule_id_between_weekly_and_blackout_rejected(client):
    cal = calendar(
        "utc",
        "UTC",
        [weekly("same", 5, "10:00", "11:00")],
        [{"rule_id": "same", "start": "2026-02-07T12:00:00Z",
          "end": "2026-02-07T13:00:00Z"}],
    )
    body = request_body("2026-02-07T00:00:00Z", "2026-02-08T00:00:00Z", [cal])
    assert client.post("/release-windows/resolve", json=body).status_code == 422


def test_duplicate_rule_id_across_calendars_rejected(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [
            calendar("us", "UTC", [weekly("shared", 5, "10:00", "11:00")]),
            calendar("eu", "UTC", [weekly("shared", 5, "12:00", "13:00")]),
        ],
    )
    assert client.post("/release-windows/resolve", json=body).status_code == 422


def test_duplicate_calendar_id_rejected(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [
            calendar("x", "UTC", [weekly("a", 5, "10:00", "11:00")]),
            calendar("x", "UTC", [weekly("b", 5, "12:00", "13:00")]),
        ],
    )
    assert client.post("/release-windows/resolve", json=body).status_code == 422


def test_zero_length_weekly_window_rejected(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("utc", "UTC", [weekly("w", 5, "10:00", "10:00")])],
    )
    assert client.post("/release-windows/resolve", json=body).status_code == 422


def test_zero_length_blackout_rejected(client):
    cal = calendar(
        "utc",
        "UTC",
        [weekly("w", 5, "10:00", "11:00")],
        [{"rule_id": "b", "start": "2026-02-07T12:00:00Z",
          "end": "2026-02-07T12:00:00Z"}],
    )
    body = request_body("2026-02-07T00:00:00Z", "2026-02-08T00:00:00Z", [cal])
    assert client.post("/release-windows/resolve", json=body).status_code == 422


def test_empty_query_range_rejected(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-07T00:00:00Z",
        [calendar("utc", "UTC")],
    )
    assert client.post("/release-windows/resolve", json=body).status_code == 422


def test_query_span_over_31_days_rejected(client):
    body = request_body(
        "2026-01-01T00:00:00Z",
        "2026-02-01T00:00:01Z",
        [calendar("utc", "UTC")],
    )
    assert client.post("/release-windows/resolve", json=body).status_code == 422


def test_min_duration_must_be_positive(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("utc", "UTC")],
        min_duration_seconds=0,
    )
    assert client.post("/release-windows/resolve", json=body).status_code == 422


def test_no_calendars_rejected(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [],
    )
    assert client.post("/release-windows/resolve", json=body).status_code == 422


@pytest.mark.parametrize("bad_time", ["25:00", "9:00", "10:60", "1000", "abc", ""])
def test_malformed_local_time_rejected(client, bad_time):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("utc", "UTC", [weekly("w", 5, bad_time, "11:00")])],
    )
    assert client.post("/release-windows/resolve", json=body).status_code == 422


def test_unknown_field_rejected(client):
    body = request_body(
        "2026-02-07T00:00:00Z",
        "2026-02-08T00:00:00Z",
        [calendar("utc", "UTC", [weekly("w", 5, "10:00", "11:00")])],
    )
    body["calendars"][0]["unexpected"] = 1
    assert client.post("/release-windows/resolve", json=body).status_code == 422
