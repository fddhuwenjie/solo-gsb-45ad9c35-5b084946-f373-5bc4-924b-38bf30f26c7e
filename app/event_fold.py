"""共享事件身份判定与折叠：实验归因、决策日志核对、铺开执行审计的统一入口。

三个入口（POST /experiments/attribute、POST /sdk/reconcile、
POST /sdk/rollout-audit）的事件都可乱序、可重投提交，折叠语义完全一致：

1. 事件以 ``event_id`` 标识身份。内容身份按**规范化内容**比较——
   UTC 等价时刻（naive 视为 UTC、aware 换算到 UTC、不同偏移写法）视为
   同一时刻；JSON 对象只按键值对判等，键序不同视为同一内容
   （嵌套结构递归规范化，数组保留顺序）；
2. 同一 ``event_id`` 的规范化内容相同（精确重投）时**只保留首次**
   出现的事件，折叠结果与提交顺序无关；
3. 同一 ``event_id`` 跨类型或任一字段冲突时抛出
   :class:`EventContentConflict`（HTTP 层表现为 ``422``）；
4. 折叠只负责“身份去重”，之后再交给各业务排序或按 ``sequence`` 重放：
   归因按 (user_id, timestamp, event_id) 排序，核对按
   (node_id, timestamp, event_id) 排序，审计按 ``sequence`` 升序重放
   （并在折叠后检查 ``sequence`` 唯一性）。
"""

from __future__ import annotations

import enum
import json
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

# 事件内容身份：业务侧给出 (字段...) 元组，键函数由各事件模型模块提供。
# 之所以用元组而非 JSON：datetime 等类型在规范化时统一换算为 UTC，
# 枚举按其值比较，dict 按规范化 JSON 判等，其余原值比较。
EventContent = tuple[Any, ...]


class EventContentConflict(ValueError):
    """同一 event_id 的规范化内容不一致（跨类型或字段冲突）。

    继承 ValueError：由 Pydantic 模型校验器抛出时按请求校验失败处理
    （HTTP 422），消息形如“事件 ID 内容冲突: '...'”。
    """


def canonical_value(value: Any) -> Any:
    """把单个值规范化为可做身份比较的形式。

    - datetime：naive 视为 UTC、aware 换算到 UTC，返回 UTC datetime；
    - enum：按其声明值（.value）比较；
    - dict：规范化 JSON（键按字典序、无空白），键序不同视为同值；
    - list / tuple：逐元素递归规范化，数组顺序保留；
    - 其余（str / int / float / bool / None 等）原样返回。
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return json.dumps(
            {key: canonical_value(item) for key, item in value.items()},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    if isinstance(value, (list, tuple)):
        return tuple(canonical_value(item) for item in value)
    return value


def fold_events(
    events: Iterable[Any],
    content_of: Callable[[Any], EventContent],
) -> list[Any]:
    """按 event_id 折叠事件流：精确重投保留首次，内容冲突抛错。

    ``content_of`` 给出事件的规范化内容字段元组；时间字段必须传
    datetime（由 canonical_value 统一换算 UTC），JSON 字段（如 context）
    传 dict 即可（键序不敏感）。
    """
    unique: dict[str, Any] = {}
    contents: dict[str, EventContent] = {}
    for event in events:
        event_id = event.event_id
        content = tuple(canonical_value(part) for part in content_of(event))
        previous = contents.get(event_id)
        if previous is not None:
            if previous != content:
                raise EventContentConflict(f"事件 ID 内容冲突: '{event_id}'")
            continue
        unique[event_id] = event
        contents[event_id] = content
    return list(unique.values())


def fold_streams(*streams: Iterable[Any]) -> Iterator[Any]:
    """顺序串联多股事件流（用于归因的曝光 + 转化跨类型折叠）。

    流的先后顺序即“首次出现”的优先级顺序；调用方通常在曝光流之后
    再给转化流（跨类型同 ID 必冲突，顺序仅影响被保留的那一条）。
    """
    for stream in streams:
        yield from stream
