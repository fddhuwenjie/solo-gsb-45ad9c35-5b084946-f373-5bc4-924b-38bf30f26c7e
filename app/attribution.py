"""实验归因：按灰度曝光事件为转化做确定性归因。

语义概述：

- 事件先经共享折叠层（app.event_fold）按 event_id 判定身份：UTC 等价
  时刻与同值 context 视为同一内容，精确重投只保留首次，跨类型或字段
  冲突在模型校验阶段即 422；折叠后再按 (user_id, timestamp, event_id)
  排序处理；
- 转化归给窗口内不晚于它的最近曝光：曝光时刻 ≤ 转化时刻且
  转化时刻 − 曝光时刻 ≤ window_seconds（闭区间）；同一时刻多个曝光时
  取 event_id 最大者；
- 无先前曝光 → no_exposure；最近曝光超出窗口 → expired；
- 同一用户相邻两次曝光 variant_id 不同即发生一次变体切换，切换后的半开
  洗脱区间 [切换时刻, 切换时刻 + washout_seconds) 内的转化记为
  contaminated，不计入任何变体（washout_seconds=0 时不产生洗脱区间）；
- 每个变体汇总去重曝光用户数、计入转化数与 value 合计。variants 按
  variant_id 升序、attributions 按 (user_id, timestamp, event_id) 升序，
  输出顺序与事件提交顺序无关。
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime, timedelta

from app.event_fold import fold_events
from app.models import (
    AttributionStatus,
    ConversionAttribution,
    ConversionEvent,
    ExperimentAttributeRequest,
    ExperimentAttributeResponse,
    ExposureEvent,
    VariantAttribution,
    attribution_event_content,
)


def attribute_experiment(
    request: ExperimentAttributeRequest,
) -> ExperimentAttributeResponse:
    """对整批乱序曝光/转化事件做确定性归因并汇总。"""
    # 共享折叠：同 event_id 精确重投保留首次；跨类型同 ID 的冲突已由
    # 模型校验（曝光在前、转化在后的同一次折叠）拒绝，这里各自折叠即可。
    exposures: list[ExposureEvent] = fold_events(
        request.exposures, attribution_event_content
    )
    conversions: list[ConversionEvent] = fold_events(
        request.conversions, attribution_event_content
    )

    # 曝光按用户分组，组内按 (时刻, event_id) 升序，供“最近曝光”二分查找。
    exposures_by_user: dict[str, list[ExposureEvent]] = {}
    for event in exposures:
        exposures_by_user.setdefault(event.user_id, []).append(event)

    exposure_times: dict[str, list[datetime]] = {}
    switch_times: dict[str, list[datetime]] = {}
    for user_id, user_exposures in exposures_by_user.items():
        user_exposures.sort(key=lambda event: (event.timestamp, event.event_id))
        exposure_times[user_id] = [event.timestamp for event in user_exposures]
        # 变体切换时刻：相邻两次曝光 variant_id 不同时，后一次曝光的时刻。
        switch_times[user_id] = [
            current.timestamp
            for previous, current in zip(user_exposures, user_exposures[1:])
            if current.variant_id != previous.variant_id
        ]

    window = timedelta(seconds=request.window_seconds)
    washout = timedelta(seconds=request.washout_seconds)

    attributions: list[ConversionAttribution] = []
    for conversion in conversions:
        user_exposures = exposures_by_user.get(conversion.user_id, [])
        times = exposure_times.get(conversion.user_id, [])

        # 不晚于转化的最近曝光：时刻 ≤ 转化时刻的最后一个；
        # 组内按 (时刻, event_id) 排序，同一时刻自然取 event_id 最大者。
        index = bisect_right(times, conversion.timestamp) - 1
        exposure = user_exposures[index] if index >= 0 else None

        status: AttributionStatus
        exposure_event_id: str | None = None
        variant_id: str | None = None
        if exposure is None:
            status = AttributionStatus.NO_EXPOSURE
        elif conversion.timestamp - exposure.timestamp > window:
            status = AttributionStatus.EXPIRED
        else:
            # 半开洗脱区间 [切换时刻, 切换时刻 + 时长)：时长全局一致时，
            # 不晚于转化时刻的最后一次切换终点最远，只需检查它一次。
            switches = switch_times.get(conversion.user_id, [])
            switch_index = bisect_right(switches, conversion.timestamp) - 1
            if switch_index >= 0 and conversion.timestamp - switches[switch_index] < washout:
                status = AttributionStatus.CONTAMINATED
            else:
                status = AttributionStatus.ATTRIBUTED
            # attributed 与 contaminated 都在窗口内匹配到了最近曝光，
            # 区别仅在于后者不计入变体统计。
            exposure_event_id = exposure.event_id
            variant_id = exposure.variant_id

        attributions.append(
            ConversionAttribution(
                event_id=conversion.event_id,
                user_id=conversion.user_id,
                timestamp=conversion.timestamp,
                value=conversion.value,
                status=status,
                exposure_event_id=exposure_event_id,
                variant_id=variant_id,
            )
        )

    # 固定输出顺序；同时让 value 按确定顺序累加（浮点和与提交顺序无关）。
    attributions.sort(key=lambda item: (item.user_id, item.timestamp, item.event_id))

    # 各变体的去重曝光用户集合。
    exposed_users: dict[str, set[str]] = {}
    for event in exposures:
        exposed_users.setdefault(event.variant_id, set()).add(event.user_id)

    conversion_counts: dict[str, int] = {variant_id: 0 for variant_id in exposed_users}
    value_sums: dict[str, float] = {variant_id: 0.0 for variant_id in exposed_users}
    for attribution in attributions:
        if (
            attribution.status is AttributionStatus.ATTRIBUTED
            and attribution.variant_id is not None
        ):
            conversion_counts[attribution.variant_id] += 1
            value_sums[attribution.variant_id] += attribution.value

    variants = [
        VariantAttribution(
            variant_id=variant_id,
            exposed_users=len(users),
            conversions=conversion_counts[variant_id],
            value=value_sums[variant_id],
        )
        for variant_id, users in sorted(exposed_users.items())
    ]

    return ExperimentAttributeResponse(
        switch_id=request.switch_id,
        variants=variants,
        attributions=attributions,
    )
