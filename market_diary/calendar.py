from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from market_diary.models import MarketGateResult


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _validated_calendar(calendar: Mapping[str, Any]) -> tuple[int, set[date]]:
    year = calendar.get("year")
    if isinstance(year, bool) or not isinstance(year, int):
        raise ValueError("calendar year must be an integer")
    raw_closed = calendar.get("closed_dates")
    if not isinstance(raw_closed, list) or any(
        not isinstance(value, str) for value in raw_closed
    ):
        raise ValueError("calendar closed_dates must be a list of ISO dates")
    closed: set[date] = set()
    for value in raw_closed:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("calendar closed_dates must contain ISO dates") from error
        if parsed.isoformat() != value:
            raise ValueError("calendar closed_dates must contain ISO dates")
        if parsed.year != year:
            raise ValueError("closed date must match calendar year")
        closed.add(parsed)
    return year, closed


def load_calendar(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("calendar must be a JSON object")
    _validated_calendar(payload)
    return payload


def is_trade_day(day: date, calendar: dict[str, Any]) -> bool:
    year, closed = _validated_calendar(calendar)
    if day.year != year:
        raise ValueError(f"date is outside calendar year {year}")
    if day.weekday() >= 5:
        return False
    return day not in closed


def next_trade_day(day: date, calendar: dict[str, Any]) -> date:
    year, _ = _validated_calendar(calendar)
    if day.year != year:
        raise ValueError(f"date is outside calendar year {year}")
    candidate = day + timedelta(days=1)
    while True:
        if candidate.year != year:
            raise ValueError("next calendar year is not configured")
        if is_trade_day(candidate, calendar):
            return candidate
        candidate += timedelta(days=1)


def _quote_time_is_at_or_after(value: Any, threshold: time) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        return False
    return parsed >= threshold


def evaluate_market_gate(
    *,
    now: datetime,
    target_date: date,
    calendar: dict[str, Any],
    close_samples: Sequence[Mapping[str, Any]] = (),
) -> MarketGateResult:
    if not is_trade_day(target_date, calendar):
        return MarketGateResult(
            "closed",
            target_date.isoformat(),
            next_trade_day(target_date, calendar).isoformat(),
            "exchange calendar marks date closed",
        )

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local_now = now.astimezone(SHANGHAI)
    if local_now.date() == target_date and local_now.time() < time(15, 15):
        return MarketGateResult(
            "market_not_stable",
            target_date.isoformat(),
            None,
            "before 15:15 Asia/Shanghai",
        )

    target_date_text = target_date.isoformat()
    dated = [row for row in close_samples if row.get("trade_date") == target_date_text]
    timestamp_ready = any(
        _quote_time_is_at_or_after(row.get("quote_time"), time(15, 10))
        for row in dated
    )
    repeated_close = (
        len(dated) >= 2
        and bool(dated[-1].get("code"))
        and dated[-1].get("code") == dated[-2].get("code")
        and float(dated[-1]["close"]) == float(dated[-2]["close"])
    )
    status = "ready" if timestamp_ready or repeated_close else "market_not_stable"
    reason = (
        "official close timestamp/stable repeated close"
        if status == "ready"
        else "close data has not passed stability check"
    )
    return MarketGateResult(status, target_date_text, None, reason)
