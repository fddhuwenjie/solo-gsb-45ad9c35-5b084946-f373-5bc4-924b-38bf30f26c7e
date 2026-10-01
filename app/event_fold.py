"""共享的事件身份判定与折叠组件（实验归因 / SDK 决策日志核对 / 铺开执行审计）。

三个事件入口对 ``event_id`` 采用完全一致的统一语义：

1. **内容规范化后再按 ``event_id`` 判身份**：同一 ``event_id`` 的多条事件，
   其内容（类型与全部业务字段）在规范化后相等时视为同一事件的**精确重投**，
   折叠时只保留**首次**出现的一条；
2. **规范化等价规则**：

   - 时间字段归一到 UTC：naive 时刻视为 UTC，带偏移时刻换算到 UTC，
     因而 ``Z`` 与 ``+08:00`` 等不同写法只要表示同一 UTC 时刻即等价；
   - JSON 字段（如决策日志的 ``context``）按规范化 JSON 判等，对象键序
     不同但值相同视为同一内容（``canonical_json_key``）；
3. **跨类型或字段冲突**：同一 ``event_id`` 却对应不同事件类型（如曝光与
   转化共用一个 ID）或任一字段不同，抛出 ``EventIdentityConflict``，
   由各 HTTP 入口转为 ``422``。

折叠只负责身份判定与去重；折叠之后，各业务再自行排序（归因按
``(user_id, timestamp, event_id)``、核对按 ``(node_id, timestamp,
event_id)``）或按 ``sequence`` 重放（铺开审计）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable, TypeVar

from app.artifacts import canonical_json

T = TypeVar("T")


class EventIdentityConflict(Exception):
    """同一 event_id 的事件规范化内容不一致（跨类型或字段冲突，入口返回 422）。"""


def utc_instant(value: datetime) -> datetime:
    """把时刻归一到 UTC（naive 视为 UTC），供跨时区写法的内容等价比较。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonical_json_key(value: object) -> bytes:
    """JSON 值的规范化键：对象键按字典序、无空白；键序不同但值相同即等价。"""
    return canonical_json(value)


def fold_events(
    events: Iterable[T],
    event_id: Callable[[T], str],
    content: Callable[[T], object],
) -> list[T]:
    """按 event_id 折叠事件流：同 ID 规范化内容相等只保留首次，冲突即抛错。

    ``content`` 返回的内容键通常是元组（首元素可为类型鉴别符），其中的
    datetime 应使用 ``utc_instant``、嵌套 JSON 应使用 ``canonical_json_key``
    归一化，以保证“UTC 等价时刻 / JSON 键序不同但值相同”视为同一内容。
    返回的去重列表保持各 event_id 首次出现的相对顺序。
    """
    seen: dict[str, object] = {}
    unique: list[T] = []
    for event in events:
        key = event_id(event)
        signature = content(event)
        previous = seen.get(key)
        if previous is None:
            seen[key] = signature
            unique.append(event)
        elif previous != signature:
            raise EventIdentityConflict(f"事件 ID 内容冲突: '{key}'")
    return unique
