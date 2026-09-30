"""发布窗口求解：多份地区维护日历在查询范围内的共同可发布 UTC 半开区间。

语义概述：

- 每份日历含 IANA 时区、按 weekday（0=周一 … 6=周日）定义的**本地**
  `weekly_windows`（end <= start 即跨午夜到下一本地自然日）与 **UTC**
  `blackouts`（半开绝对区间）；
- 逐日把周期窗口展开成 UTC 区间。本地边界在春季跳时（gap）中不存在时
  **顺延到首个有效时刻**；在秋季重复（fold）中，**开始取较早一次、
  结束取较晚一次**；同一 (weekday, 墙上时刻) 的多个规则产生重复边界时，
  开始取较早时刻、结束取较晚时刻；顺延导致区间坍塌为空时该次出现被丢弃；
- 每份日历先把展开结果按**重叠或相邻**（含端点相接）合并，再扣除黑窗
  （扣除时保留各段的规则与 DST 调整出处）；
- 求全部日历可用区间的交集，裁剪到查询的半开范围 `[start, end)`，
  丢弃短于 `min_duration_seconds` 的结果；
- 响应按时间排序，逐窗口列出各日历贡献的规则 ID，以及覆盖该窗口的日历
  所发生过的 DST 调整（resolved_at 落在窗口内，包含端点）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.models import (
    DstAdjustment,
    DstAdjustmentKind,
    DstBoundary,
    ReleaseCalendar,
    ReleaseResolveRequest,
    ReleaseResolveResponse,
    ResolvedWindow,
    WindowContributor,
)

UTC = ZoneInfo("UTC")
# 理论上 gap 不超过 24 小时；分钟步进有此上限即可覆盖任何已知 DST 规则。
_GAP_LIMIT = timedelta(hours=48)
# 展开周期窗口时，UTC 日期向查询两侧各扩 2 天：任意 UTC 偏移（含历史偏移）
# 下，可能与查询范围相交的出现都始于这几个本地日期。
_PADDING_DAYS = 2


@dataclass(frozen=True)
class Adjustment:
    """内部表示：一次 DST 调整及其出处。"""

    calendar_id: str
    rule_id: str
    boundary: DstBoundary
    kind: DstAdjustmentKind
    local_time: datetime  # naive，规则 weekday 所落本地日期的墙上时刻
    resolved_at: datetime  # aware UTC，边界最终解析到的时刻


@dataclass
class Segment:
    """一段半开 UTC 可用区间及其规则/DST 出处。"""

    start: datetime
    end: datetime
    rule_ids: frozenset[str] = frozenset()
    adjustments: tuple[Adjustment, ...] = ()


def _is_nonexistent(local: datetime, tz: ZoneInfo) -> bool:
    """本地墙上时刻是否落在春季跳时的空档（不存在）。"""
    aware = local.replace(tzinfo=tz, fold=0)
    return aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != local


def _is_ambiguous(local: datetime, tz: ZoneInfo) -> bool:
    """本地墙上时刻是否在秋季重复（fold=0 与 fold=1 对应不同 UTC 时刻）。"""
    early = local.replace(tzinfo=tz, fold=0).astimezone(UTC)
    late = local.replace(tzinfo=tz, fold=1).astimezone(UTC)
    return early != late


def resolve_boundary(
    local: datetime,
    tz: ZoneInfo,
    boundary: DstBoundary,
    calendar_id: str,
    rule_id: str,
) -> tuple[datetime, Adjustment | None]:
    """把本地墙上边界解析为 UTC 时刻，返回 (UTC 时刻, DST 调整或 None)。

    - 不存在（gap）：逐分钟顺延到首个有效本地时刻；
    - 重复（fold）：开始取较早（fold=0）、结束取较晚（fold=1）。
    """
    if _is_nonexistent(local, tz):
        shifted = local
        step = timedelta(minutes=1)
        while shifted - local <= _GAP_LIMIT:
            shifted = shifted + step
            if not _is_nonexistent(shifted, tz):
                break
        # 顺延后的墙上时刻是唯一有效的，fold=0/1 等价，取 0 即可。
        resolved = shifted.replace(tzinfo=tz, fold=0).astimezone(UTC)
        return resolved, Adjustment(
            calendar_id=calendar_id,
            rule_id=rule_id,
            boundary=boundary,
            kind=DstAdjustmentKind.GAP_SHIFTED,
            local_time=local,
            resolved_at=resolved,
        )

    if _is_ambiguous(local, tz):
        if boundary is DstBoundary.START:
            resolved = local.replace(tzinfo=tz, fold=0).astimezone(UTC)
            kind = DstAdjustmentKind.AMBIGUOUS_EARLIER
        else:
            resolved = local.replace(tzinfo=tz, fold=1).astimezone(UTC)
            kind = DstAdjustmentKind.AMBIGUOUS_LATER
        return resolved, Adjustment(
            calendar_id=calendar_id,
            rule_id=rule_id,
            boundary=boundary,
            kind=kind,
            local_time=local,
            resolved_at=resolved,
        )

    return local.replace(tzinfo=tz).astimezone(UTC), None


def _parse_hhmm(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour=hour, minute=minute)


def expand_calendar_for(
    calendar: ReleaseCalendar,
    query_start: datetime,
    query_end: datetime,
) -> list[Segment]:
    """在覆盖查询范围的本地日期上展开周期窗口，并合并重叠/相邻出现。

    UTC 日期两侧各扩 _PADDING_DAYS 天；跨午夜窗口由其 weekday 当日生成、
    结束落在下一本地自然日。顺延/重复解析后坍塌为空（end <= start）的
    出现直接丢弃。重复边界的“开始取较早、结束取较晚”由重叠合并统一完成。
    """
    tz = ZoneInfo(calendar.timezone)
    segments: list[Segment] = []

    day = (query_start - timedelta(days=_PADDING_DAYS)).date()
    last_day = (query_end + timedelta(days=_PADDING_DAYS)).date()
    while day <= last_day:
        for window in calendar.weekly_windows:
            if window.weekday != day.weekday():
                continue
            start_clock = _parse_hhmm(window.start_local)
            end_clock = _parse_hhmm(window.end_local)
            start_local = datetime.combine(day, start_clock)
            # end <= start 即跨午夜：结束在下一本地自然日（相等已被模型拒绝）。
            end_date = day if end_clock > start_clock else day + timedelta(days=1)
            end_local = datetime.combine(end_date, end_clock)

            start_utc, start_adj = resolve_boundary(
                start_local, tz, DstBoundary.START,
                calendar.calendar_id, window.rule_id,
            )
            end_utc, end_adj = resolve_boundary(
                end_local, tz, DstBoundary.END,
                calendar.calendar_id, window.rule_id,
            )
            if end_utc <= start_utc:
                # 顺延/重复解析后坍塌为空：该次出现不贡献任何时间。
                continue
            segments.append(
                Segment(
                    start=start_utc,
                    end=end_utc,
                    rule_ids=frozenset({window.rule_id}),
                    adjustments=tuple(
                        adj for adj in (start_adj, end_adj) if adj is not None
                    ),
                )
            )
        day += timedelta(days=1)

    return merge_segments(segments)


def merge_segments(segments: list[Segment]) -> list[Segment]:
    """合并重叠或相邻（端点相接）的区间；规则 ID 取并集，调整记录全部保留。"""
    if not segments:
        return []
    ordered = sorted(segments, key=lambda segment: (segment.start, segment.end))
    merged: list[Segment] = []
    for segment in ordered:
        if merged and segment.start <= merged[-1].end:
            previous = merged[-1]
            merged[-1] = Segment(
                start=previous.start,
                end=max(previous.end, segment.end),
                rule_ids=previous.rule_ids | segment.rule_ids,
                adjustments=previous.adjustments + segment.adjustments,
            )
        else:
            merged.append(segment)
    return merged


def _filter_adjustments(
    adjustments: tuple[Adjustment, ...],
    *,
    start: datetime,
    end: datetime,
) -> tuple[Adjustment, ...]:
    """保留解析时刻落在 [start, end]（**含端点**）的调整。

    黑窗切割、交集、裁剪都按出处传播调整；最终只保留解析时刻确实落在
    本段内的那些。含端点是为了让“窗口结束恰为被顺延的边界”这类调整仍
    归属于该窗口。
    """
    return tuple(
        adjustment
        for adjustment in adjustments
        if start <= adjustment.resolved_at <= end
    )


def subtract_blackouts(calendar: ReleaseCalendar, available: list[Segment]) -> list[Segment]:
    """从可用区间中扣除该日历的全部 UTC 黑窗（先合并黑窗再逐段切割）。

    切出的每段仍携带原周期窗口的规则与 DST 调整出处；黑窗规则本身不作为
    正向贡献。
    """
    blackouts = merge_segments(
        [
            Segment(start=blackout.start, end=blackout.end)
            for blackout in calendar.blackouts
        ]
    )
    if not blackouts:
        return available

    result: list[Segment] = []
    for segment in available:
        cursor = segment.start
        for blackout in blackouts:
            if blackout.end <= segment.start:
                continue
            if blackout.start >= segment.end:
                break
            cut_start = max(cursor, blackout.start)
            if cursor < cut_start:
                result.append(
                    Segment(
                        start=cursor,
                        end=cut_start,
                        rule_ids=segment.rule_ids,
                        adjustments=_filter_adjustments(
                            segment.adjustments, start=cursor, end=cut_start
                        ),
                    )
                )
            cursor = max(cursor, blackout.end)
            if cursor >= segment.end:
                break
        if cursor < segment.end:
            result.append(
                Segment(
                    start=cursor,
                    end=segment.end,
                    rule_ids=segment.rule_ids,
                    adjustments=_filter_adjustments(
                        segment.adjustments, start=cursor, end=segment.end
                    ),
                )
            )
    return merge_segments(result)


def intersect_segments(left: list[Segment], right: list[Segment]) -> list[Segment]:
    """两个有序半开区间列表的交集；规则 ID 与调整出处取并集。"""
    result: list[Segment] = []
    i = j = 0
    while i < len(left) and j < len(right):
        left_seg, right_seg = left[i], right[j]
        start = max(left_seg.start, right_seg.start)
        end = min(left_seg.end, right_seg.end)
        if start < end:
            result.append(
                Segment(
                    start=start,
                    end=end,
                    rule_ids=left_seg.rule_ids | right_seg.rule_ids,
                    adjustments=_filter_adjustments(
                        left_seg.adjustments + right_seg.adjustments,
                        start=start,
                        end=end,
                    ),
                )
            )
        if left_seg.end < right_seg.end:
            i += 1
        elif right_seg.end < left_seg.end:
            j += 1
        else:
            i += 1
            j += 1
    return result


def clip_to_query(segments: list[Segment], start: datetime, end: datetime) -> list[Segment]:
    """裁剪到半开查询范围 [start, end)。"""
    result: list[Segment] = []
    for segment in segments:
        clipped_start = max(segment.start, start)
        clipped_end = min(segment.end, end)
        if clipped_start < clipped_end:
            result.append(
                Segment(
                    start=clipped_start,
                    end=clipped_end,
                    rule_ids=segment.rule_ids,
                    adjustments=_filter_adjustments(
                        segment.adjustments,
                        start=clipped_start,
                        end=clipped_end,
                    ),
                )
            )
    return result


def resolve_release_windows(request: ReleaseResolveRequest) -> ReleaseResolveResponse:
    """计算全部日历在查询范围内共同可发布且足够长的 UTC 半开区间。"""
    # 规则 -> 日历归属（请求模型已保证规则 ID 全局唯一）。
    rule_to_calendar: dict[str, str] = {}
    per_calendar: list[tuple[ReleaseCalendar, list[Segment]]] = []
    common: list[Segment] | None = None
    for calendar in request.calendars:
        for window in calendar.weekly_windows:
            rule_to_calendar[window.rule_id] = calendar.calendar_id
        availability = subtract_blackouts(
            calendar,
            expand_calendar_for(calendar, request.start, request.end),
        )
        per_calendar.append((calendar, availability))
        common = availability if common is None else intersect_segments(common, availability)

    common = clip_to_query(common or [], request.start, request.end)
    min_duration = timedelta(seconds=request.min_duration_seconds)

    windows: list[ResolvedWindow] = []
    for segment in common:
        if segment.end - segment.start < min_duration:
            continue

        # 各日历贡献：周期窗口规则按归属日历分组，日历与规则均升序。
        contributor_map: dict[str, set[str]] = {}
        for rule_id in segment.rule_ids:
            calendar_id = rule_to_calendar.get(rule_id)
            if calendar_id is not None:
                contributor_map.setdefault(calendar_id, set()).add(rule_id)

        # DST 调整：仅保留 resolved_at 落在窗口内（含端点）的调整。
        adjustments = sorted(
            {
                adj
                for adj in segment.adjustments
                if segment.start <= adj.resolved_at <= segment.end
            },
            key=lambda adj: (
                adj.resolved_at,
                adj.calendar_id,
                adj.rule_id,
                adj.boundary.value,
            ),
        )

        windows.append(
            ResolvedWindow(
                start=segment.start,
                end=segment.end,
                duration_seconds=int((segment.end - segment.start).total_seconds()),
                contributors=[
                    WindowContributor(calendar_id=calendar_id, rule_ids=sorted(rule_ids))
                    for calendar_id, rule_ids in sorted(contributor_map.items())
                ],
                dst_adjustments=[
                    DstAdjustment(
                        calendar_id=adj.calendar_id,
                        rule_id=adj.rule_id,
                        boundary=adj.boundary,
                        kind=adj.kind,
                        local_time=adj.local_time,
                        resolved_at=adj.resolved_at,
                    )
                    for adj in adjustments
                ],
            )
        )

    windows.sort(key=lambda window: (window.start, window.end))
    return ReleaseResolveResponse(
        start=request.start,
        end=request.end,
        min_duration_seconds=request.min_duration_seconds,
        windows=windows,
    )
