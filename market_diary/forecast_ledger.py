from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .storage import atomic_write_json


CORE_INDEX_CODES: tuple[str, ...] = ("399006", "000688", "000905", "000300")
SIX_FACTOR_KEYS: tuple[str, ...] = (
    "fundamental_catalyst",
    "industry_cycle",
    "liquidity",
    "technical_structure",
    "valuation",
    "risk",
)

ALLOWED_FACTOR_STATES = {"positive", "neutral", "negative"}
ALLOWED_EXCEPTIONS = {"data_error", "delisted", "long_suspension"}
PROBABILITY_KEYS = {"up", "flat", "down"}
DISCLAIMER = "不构成投资建议"


class ForecastValidationError(ValueError):
    """Raised when a forecast document violates the canonical ledger schema."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ForecastValidationError(f"{label}: must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ForecastValidationError(f"{label}: must be a list")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ForecastValidationError(f"{label}: must be nonempty text")
    return value


def _iso_date(value: Any, label: str) -> tuple[str, date]:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise ForecastValidationError(f"{label}: must use YYYY-MM-DD format") from error
    if str(parsed) != text:
        raise ForecastValidationError(f"{label}: must use YYYY-MM-DD format")
    return text, parsed


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ForecastValidationError(f"{label}: must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ForecastValidationError(f"{label}: must be a finite number") from error
    if not math.isfinite(number):
        raise ForecastValidationError(f"{label}: must be a finite number")
    return number


def _validate_probabilities(value: Any, label: str) -> dict[str, Any]:
    probabilities = _mapping(value, f"{label}.probabilities")
    if set(probabilities) != PROBABILITY_KEYS:
        raise ForecastValidationError(
            f"{label}: probabilities must contain exactly up, flat, and down"
        )
    numbers = {
        key: _finite_number(probabilities[key], f"{label}.probabilities.{key}")
        for key in ("up", "flat", "down")
    }
    if any(number < 0 or number > 100 for number in numbers.values()):
        raise ForecastValidationError(
            f"{label}: probability values must be between 0 and 100"
        )
    if not math.isclose(sum(numbers.values()), 100.0, rel_tol=0.0, abs_tol=1e-6):
        raise ForecastValidationError(f"{label}: probabilities must sum to 100")
    return dict(probabilities)


def _normalize_range(
    item: Mapping[str, Any], label: str, *, require_probabilities: bool
) -> dict[str, Any]:
    lower = _finite_number(item.get("lower"), f"{label}.lower")
    upper = _finite_number(item.get("upper"), f"{label}.upper")
    if lower > upper:
        raise ForecastValidationError(f"{label}: lower exceeds upper")
    midpoint = round(lower / 2 + upper / 2, 6)
    if not math.isfinite(midpoint):
        raise ForecastValidationError(f"{label}.midpoint: must be a finite number")
    if "midpoint" in item:
        supplied = _finite_number(item["midpoint"], f"{label}.midpoint")
        if not math.isclose(supplied, midpoint, rel_tol=0.0, abs_tol=1e-6):
            raise ForecastValidationError(f"{label}: midpoint does not match range")
    normalized = {**dict(item), "lower": lower, "upper": upper, "midpoint": midpoint}
    if require_probabilities:
        normalized["probabilities"] = _validate_probabilities(
            item.get("probabilities"), label
        )
    return normalized


def _code_list(value: Any, label: str) -> list[str]:
    rows = _list(value, label)
    codes = [_text(row, f"{label}[{index}]") for index, row in enumerate(rows)]
    if len(set(codes)) != len(codes):
        raise ForecastValidationError(f"{label}: codes must be unique")
    return codes


def _pool_codes(document: Mapping[str, Any], label: str) -> set[str]:
    rows = _list(document.get("watch_pool"), f"{label}.watch_pool")
    if len(rows) != 5:
        raise ForecastValidationError(f"{label}.watch_pool: must contain exactly 5 stocks")
    codes = {
        _text(
            _mapping(row, f"{label}.watch_pool[{index}]").get("code"),
            f"{label}.watch_pool[{index}].code",
        )
        for index, row in enumerate(rows)
    }
    if len(codes) != 5:
        raise ForecastValidationError(f"{label}.watch_pool: stock codes must be unique")
    return codes


def _validate_pool_change(
    current: set[str], previous: set[str], changes: Mapping[str, Any]
) -> dict[str, Any]:
    declared_exited = _code_list(changes.get("exited"), "changes.exited")
    declared_entered = _code_list(changes.get("entered"), "changes.entered")
    if len(declared_exited) != len(declared_entered):
        raise ForecastValidationError("exits and entries must have equal size")

    exited = previous - current
    entered = current - previous
    if set(declared_exited) != exited or set(declared_entered) != entered:
        raise ForecastValidationError(
            "declared changes must match the actual watch-pool difference"
        )

    exception_reason = changes.get("exception_reason")
    if exception_reason is not None and exception_reason not in ALLOWED_EXCEPTIONS:
        allowed = ", ".join(sorted(ALLOWED_EXCEPTIONS))
        raise ForecastValidationError(f"exception_reason must be one of: {allowed}")
    if len(entered) > 2 and exception_reason not in ALLOWED_EXCEPTIONS:
        raise ForecastValidationError("normal watch-pool replacement is at most 2")
    return {**dict(changes), "exited": declared_exited, "entered": declared_entered}


def _normalize_indices(value: Any) -> list[dict[str, Any]]:
    rows = _list(value, "indices")
    mappings = [_mapping(row, f"indices[{index}]") for index, row in enumerate(rows)]
    codes = [row.get("code") for row in mappings]
    if (
        len(rows) != 4
        or any(not isinstance(code, str) for code in codes)
        or set(codes) != set(CORE_INDEX_CODES)
        or len(set(codes)) != 4
    ):
        raise ForecastValidationError("indices must contain exactly the four core indices")

    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(mappings):
        label = f"indices[{index}]"
        item = _normalize_range(row, label, require_probabilities=True)
        _text(row.get("name"), f"{label}.name")
        _text(row.get("week_range"), f"{label}.week_range")
        _text(row.get("invalidation"), f"{label}.invalidation")
        normalized.append(item)
    return normalized


def _normalize_sectors(value: Any) -> list[dict[str, Any]]:
    rows = _list(value, "sectors")
    if not 3 <= len(rows) <= 5:
        raise ForecastValidationError("sectors must contain 3 to 5 sectors")
    mappings = [_mapping(row, f"sectors[{index}]") for index, row in enumerate(rows)]
    codes = [
        _text(row.get("code"), f"sectors[{index}].code")
        for index, row in enumerate(mappings)
    ]
    if len(set(codes)) != len(codes):
        raise ForecastValidationError("sectors must use unique sector codes")

    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(mappings):
        label = f"sectors[{index}]"
        for field in ("name", "catalyst", "crowding", "invalidation"):
            _text(row.get(field), f"{label}.{field}")
        item = dict(row)
        item["probabilities"] = _validate_probabilities(
            row.get("probabilities"), label
        )
        if "lower" in row or "upper" in row or "midpoint" in row:
            item = _normalize_range(item, label, require_probabilities=True)
        normalized.append(item)
    return normalized


def _normalize_factors(value: Any, label: str) -> dict[str, dict[str, Any]]:
    factors = _mapping(value, f"{label}.factors")
    missing = [key for key in SIX_FACTOR_KEYS if key not in factors]
    if missing:
        raise ForecastValidationError(f"{label}.factors: missing {', '.join(missing)}")
    extras = set(factors) - set(SIX_FACTOR_KEYS)
    if extras:
        raise ForecastValidationError(
            f"{label}.factors: unexpected factors {', '.join(sorted(extras))}"
        )

    normalized: dict[str, dict[str, Any]] = {}
    for key in SIX_FACTOR_KEYS:
        assessment = _mapping(factors[key], f"{label}.factors.{key}")
        state = assessment.get("state")
        if state not in ALLOWED_FACTOR_STATES:
            raise ForecastValidationError(
                f"{label}.factors.{key}.state must be positive, neutral, or negative"
            )
        _text(assessment.get("conclusion"), f"{label}.factors.{key}.conclusion")
        normalized[key] = dict(assessment)
    return normalized


def _normalize_watch_pool(value: Any) -> list[dict[str, Any]]:
    rows = _list(value, "watch_pool")
    if len(rows) != 5:
        raise ForecastValidationError("watch_pool must contain exactly 5 stocks")
    mappings = [_mapping(row, f"watch_pool[{index}]") for index, row in enumerate(rows)]
    codes = [
        _text(row.get("code"), f"watch_pool[{index}].code")
        for index, row in enumerate(mappings)
    ]
    if len(set(codes)) != 5:
        raise ForecastValidationError("watch_pool must use unique stock codes")

    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(mappings):
        label = f"watch_pool[{index}]"
        for field in ("name", "week_view", "watch_condition", "status"):
            _text(row.get(field), f"{label}.{field}")
        item = _normalize_range(row, label, require_probabilities=False)
        item["factors"] = _normalize_factors(row.get("factors"), label)
        normalized.append(item)
    return normalized


def _validate_sources(value: Any) -> list[str]:
    sources = _list(value, "sources")
    if not sources:
        raise ForecastValidationError("sources must contain at least one source URL")
    normalized: list[str] = []
    for index, value in enumerate(sources):
        source = _text(value, f"sources[{index}]")
        parsed = urlparse(source)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ForecastValidationError("sources must contain only HTTP or HTTPS URLs")
        normalized.append(source)
    return normalized


def validate_forecast(
    document: Mapping[str, Any], *, previous: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Validate and normalize one forecast without mutating the caller's document."""

    document = _mapping(document, "forecast")
    trade_date, parsed_trade_date = _iso_date(document.get("trade_date"), "trade_date")
    next_trade_date, parsed_next_trade_date = _iso_date(
        document.get("next_trade_date"), "next_trade_date"
    )
    if parsed_next_trade_date <= parsed_trade_date:
        raise ForecastValidationError("next_trade_date must be after trade_date")
    if document.get("disclaimer") != DISCLAIMER:
        raise ForecastValidationError(f"disclaimer must be exactly {DISCLAIMER}")

    normalized_indices = _normalize_indices(document.get("indices"))
    normalized_sectors = _normalize_sectors(document.get("sectors"))
    normalized_pool = _normalize_watch_pool(document.get("watch_pool"))
    current_codes = {row["code"] for row in normalized_pool}
    changes = _mapping(document.get("changes"), "changes")

    if previous is None:
        exited = _code_list(changes.get("exited"), "changes.exited")
        entered = _code_list(changes.get("entered"), "changes.entered")
        if exited or entered:
            raise ForecastValidationError(
                "initial watch pool is not a replacement; changes must be empty"
            )
        if changes.get("exception_reason") is not None:
            raise ForecastValidationError("initial watch pool cannot use exception_reason")
        normalized_changes = {**dict(changes), "exited": exited, "entered": entered}
    else:
        previous = _mapping(previous, "previous")
        _, parsed_previous_date = _iso_date(
            previous.get("trade_date"), "previous.trade_date"
        )
        if parsed_trade_date <= parsed_previous_date:
            raise ForecastValidationError("trade_date must be after previous trade_date")
        previous_codes = _pool_codes(previous, "previous")
        normalized_changes = _validate_pool_change(
            current_codes, previous_codes, changes
        )

    normalized = dict(document)
    normalized.update(
        {
            "trade_date": trade_date,
            "next_trade_date": next_trade_date,
            "indices": normalized_indices,
            "sectors": normalized_sectors,
            "watch_pool": normalized_pool,
            "changes": normalized_changes,
            "sources": _validate_sources(document.get("sources")),
            "disclaimer": DISCLAIMER,
        }
    )
    return normalized


def save_forecast(
    document: Mapping[str, Any],
    directory: str | Path,
    *,
    previous: Mapping[str, Any] | None = None,
) -> Path:
    """Atomically persist a validated forecast under its trade-date filename."""

    normalized = validate_forecast(document, previous=previous)
    path = Path(directory) / f"{normalized['trade_date']}.json"
    atomic_write_json(path, normalized)
    return path
