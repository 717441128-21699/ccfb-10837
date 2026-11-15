"""股东变更事件回放 —— 从变更记录推出任意一天的股权结构。

事件语义（半开区间）：
- ``event_date <= as_of < valid_to`` 时事件有效；``valid_to`` 为空表示至今有效。
- 同一公司同一股东可能有多段事件（退出后再进入等），取所有有效段并按比例求和兜底。
"""
from __future__ import annotations

from typing import Any, Iterable


def _get(row: Any, key: str, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def replay_events(events: Iterable[Any], as_of: str | None = None) -> list[dict]:
    """把事件流回放成 ``as_of`` 当天的股东表。

    返回字段：holder_kind, holder_id, shares_pct, voting_pct,
    is_nominee, beneficial_holder_id。
    """
    agg: dict[tuple[str, str], dict] = {}
    for ev in events:
        event_date = _get(ev, "event_date")
        valid_to = _get(ev, "valid_to")
        if as_of is not None:
            if event_date and event_date > as_of:
                continue
            if valid_to and valid_to <= as_of:
                continue
        elif valid_to is not None:
            # 不传 as_of：只看至今有效的事件
            continue
        kind = _get(ev, "holder_kind")
        holder = _get(ev, "holder_id")
        key = (kind, holder)
        slot = agg.setdefault(key, {
            "holder_kind": kind,
            "holder_id": holder,
            "shares_pct": 0.0,
            "voting_pct": 0.0,
            "is_nominee": int(_get(ev, "is_nominee", 0) or 0),
            "beneficial_holder_id": _get(ev, "beneficial_holder_id"),
        })
        slot["shares_pct"] += float(_get(ev, "shares_pct", 0.0))
        slot["voting_pct"] += float(_get(ev, "voting_pct", 0.0))
        # 元信息以最新一段为准
        slot["is_nominee"] = int(_get(ev, "is_nominee", 0) or 0)
        slot["beneficial_holder_id"] = _get(ev, "beneficial_holder_id")
    return list(agg.values())


def assert_cap_table_closed(holdings: list[dict], *, tol: float = 1e-6) -> None:
    """断言某一时点持股比例/表决权合计 100%，生成与测试共用。"""
    shares = sum(h["shares_pct"] for h in holdings)
    voting = sum(h["voting_pct"] for h in holdings)
    if abs(shares - 100.0) > tol or abs(voting - 100.0) > tol:
        raise AssertionError(
            f"cap table not closed: shares={shares:.6f} voting={voting:.6f}"
        )


def event_timeline(events: Iterable[Any]) -> list[str]:
    """公司全部变更日期（去重升序），供前端时间轴使用。"""
    dates = {_get(ev, "event_date") for ev in events if _get(ev, "event_date")}
    return sorted(dates)
