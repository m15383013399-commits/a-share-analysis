from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from .forecast_ledger import validate_forecast
from .storage import atomic_write_text


EVALUATION_KEY: tuple[str, ...] = (
    "forecast_date",
    "actual_date",
    "asset_type",
    "asset_code",
)
SNAPSHOT_KEYS = {
    "schema_version",
    "trade_date",
    "generated_at",
    "market_status",
    "quality_status",
    "sources",
    "indices",
    "breadth",
    "sectors",
    "stocks",
    "warnings",
}
ASSET_GROUPS = {"index": "indices", "sector": "sectors", "stock": "stocks"}
SUSPENDED_STATUSES = {"suspended", "halted", "停牌"}


class EvaluationPersistenceError(RuntimeError):
    """Raised when persistence fails and prior state cannot be fully restored."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label}: must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{label}: must be a sequence")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: must be nonempty text")
    return value


def _iso_date(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label}: must use YYYY-MM-DD format") from error
    if str(parsed) != text:
        raise ValueError(f"{label}: must use YYYY-MM-DD format")
    return text


def _finite_number(value: Any, label: str) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{label}: must be a finite number")
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"{label}: must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label}: must be a finite number") from error
    if not math.isfinite(number):
        raise ValueError(f"{label}: must be a finite number")
    return number


def evaluate_range(
    *,
    actual_pct: float,
    lower: float,
    upper: float,
    flat_threshold: float = 0.30,
) -> dict[str, Any]:
    """Score one percentage forecast range using percentage-point arithmetic."""

    actual = _finite_number(actual_pct, "actual_pct")
    low = _finite_number(lower, "lower")
    high = _finite_number(upper, "upper")
    threshold = _finite_number(flat_threshold, "flat_threshold")
    if low > high:
        raise ValueError("lower: must not exceed upper")
    if threshold < 0:
        raise ValueError("flat_threshold: must be nonnegative")

    midpoint = round(low / 2 + high / 2, 6)
    if not math.isfinite(midpoint):
        raise ValueError("midpoint: derived value must be finite")
    error = round(actual - midpoint, 6)
    if not math.isfinite(error):
        raise ValueError("error: derived value must be finite")

    if midpoint > 0:
        direction_hit = actual > 0
    elif midpoint < 0:
        direction_hit = actual < 0
    else:
        direction_hit = abs(actual) <= threshold

    return {
        "actual_pct": actual,
        "lower": low,
        "upper": high,
        "midpoint": midpoint,
        "error": error,
        "absolute_error": abs(error),
        "direction_hit": direction_hit,
        "range_hit": low <= actual <= high,
    }


def _row_map(value: Any, label: str, actual_date: str) -> dict[str, Mapping[str, Any]]:
    rows = _sequence(value, label)
    by_code: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(rows):
        row = _mapping(value, f"{label}[{index}]")
        code = _text(row.get("code"), f"{label}[{index}].code")
        if code in by_code:
            raise ValueError(f"{label}: duplicate actual code {code}")
        row_date = _iso_date(row.get("trade_date"), f"{label}[{index}].trade_date")
        if row_date != actual_date:
            raise ValueError(
                f"{label}[{index}].trade_date {row_date} does not match {actual_date}"
            )
        by_code[code] = row
    return by_code


def _base_record(
    forecast_row: Mapping[str, Any],
    *,
    forecast_date: str,
    actual_date: str,
    asset_type: str,
) -> dict[str, Any]:
    return {
        "forecast_date": forecast_date,
        "actual_date": actual_date,
        "asset_type": asset_type,
        "asset_code": forecast_row["code"],
        "asset_name": forecast_row["name"],
    }


def _validated_forecast_for_evaluation(
    forecast: Any,
    previous_forecast: Mapping[str, Any] | None,
    allow_initial_without_previous: bool,
) -> dict[str, Any]:
    """Validate only with real history or explicit first-ledger authorization."""

    if not isinstance(allow_initial_without_previous, bool):
        raise ValueError("allow_initial_without_previous must be a boolean")
    document = _mapping(forecast, "forecast")
    if previous_forecast is not None and allow_initial_without_previous:
        raise ValueError(
            "previous_forecast and allow_initial_without_previous are mutually exclusive"
        )
    if previous_forecast is None and not allow_initial_without_previous:
        raise ValueError(
            "previous_forecast is required unless "
            "allow_initial_without_previous=True is explicitly authorized"
        )

    raw_sectors = _sequence(document.get("sectors"), "forecast.sectors")
    for index, value in enumerate(raw_sectors):
        sector = _mapping(value, f"forecast.sectors[{index}]")
        range_fields = {field for field in ("lower", "upper") if field in sector}
        if range_fields and range_fields != {"lower", "upper"}:
            raise ValueError(
                f"forecast sector {index}: lower and upper must be supplied together"
            )

    if previous_forecast is None:
        return validate_forecast(document)
    return validate_forecast(
        document,
        previous=_mapping(previous_forecast, "previous_forecast"),
    )


def _not_evaluable(base: Mapping[str, Any], reason: str, **values: Any) -> dict[str, Any]:
    return {**dict(base), **values, "status": "not_evaluable", "reason": reason}


def _evaluated(
    base: Mapping[str, Any],
    forecast_row: Mapping[str, Any],
    actual_pct: float,
    flat_threshold: float,
) -> dict[str, Any]:
    score = evaluate_range(
        actual_pct=actual_pct,
        lower=forecast_row["lower"],
        upper=forecast_row["upper"],
        flat_threshold=flat_threshold,
    )
    return {**dict(base), "status": "evaluated", **score}


def evaluate_forecast(
    forecast: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    flat_threshold: float = 0.30,
    previous_forecast: Mapping[str, Any] | None = None,
    allow_initial_without_previous: bool = False,
) -> list[dict[str, Any]]:
    """Evaluate one forecast against its explicitly dated next-session snapshot."""

    normalized = _validated_forecast_for_evaluation(
        forecast,
        previous_forecast,
        allow_initial_without_previous,
    )
    snapshot = _mapping(snapshot, "snapshot")
    if set(snapshot) != SNAPSHOT_KEYS:
        missing = sorted(SNAPSHOT_KEYS - set(snapshot))
        extras = sorted(set(snapshot) - SNAPSHOT_KEYS)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extras:
            details.append(f"unexpected {', '.join(extras)}")
        raise ValueError(f"snapshot: must use exact 11-field contract ({'; '.join(details)})")

    forecast_date = normalized["trade_date"]
    actual_date = _iso_date(snapshot.get("trade_date"), "snapshot.trade_date")
    if normalized["next_trade_date"] != actual_date:
        raise ValueError(
            "forecast.next_trade_date "
            f"{normalized['next_trade_date']} does not match snapshot.trade_date {actual_date}"
        )
    threshold = _finite_number(flat_threshold, "flat_threshold")
    if threshold < 0:
        raise ValueError("flat_threshold: must be nonnegative")

    indices = _row_map(snapshot.get("indices"), "snapshot.indices", actual_date)
    stocks = _row_map(snapshot.get("stocks"), "snapshot.stocks", actual_date)
    sectors_container = _mapping(snapshot.get("sectors"), "snapshot.sectors")
    industries = _sequence(
        sectors_container.get("industries"), "snapshot.sectors.industries"
    )
    concepts = _sequence(
        sectors_container.get("concepts"), "snapshot.sectors.concepts"
    )
    sectors = _row_map(
        [*industries, *concepts], "snapshot.sectors.proxy_rows", actual_date
    )

    records: list[dict[str, Any]] = []
    for forecast_row in normalized["indices"]:
        base = _base_record(
            forecast_row,
            forecast_date=forecast_date,
            actual_date=actual_date,
            asset_type="index",
        )
        actual = indices.get(forecast_row["code"])
        if actual is None:
            records.append(_not_evaluable(base, "actual_missing"))
            continue
        actual_pct = _finite_number(
            actual.get("pct"), f"snapshot.indices.{forecast_row['code']}.pct"
        )
        records.append(_evaluated(base, forecast_row, actual_pct, threshold))

    for forecast_row in normalized["sectors"]:
        base = _base_record(
            forecast_row,
            forecast_date=forecast_date,
            actual_date=actual_date,
            asset_type="sector",
        )
        actual = sectors.get(forecast_row["code"])
        if actual is None:
            records.append(_not_evaluable(base, "actual_missing"))
            continue
        actual_pct = _finite_number(
            actual.get("pct"), f"snapshot.sectors.{forecast_row['code']}.pct"
        )
        if "lower" not in forecast_row and "upper" not in forecast_row:
            records.append(
                _not_evaluable(
                    base, "qualitative_forecast", actual_pct=actual_pct
                )
            )
            continue
        records.append(_evaluated(base, forecast_row, actual_pct, threshold))

    for forecast_row in normalized["watch_pool"]:
        base = _base_record(
            forecast_row,
            forecast_date=forecast_date,
            actual_date=actual_date,
            asset_type="stock",
        )
        actual = stocks.get(forecast_row["code"])
        if actual is None:
            records.append(_not_evaluable(base, "actual_missing"))
            continue
        quote_status = str(actual.get("status") or "").strip().lower()
        if quote_status in SUSPENDED_STATUSES:
            records.append(_not_evaluable(base, "suspended"))
            continue
        if "close" not in actual or actual.get("close") is None:
            records.append(_not_evaluable(base, "close_missing"))
            continue
        close = _finite_number(
            actual.get("close"), f"snapshot.stocks.{forecast_row['code']}.close"
        )
        previous_close = _finite_number(
            actual.get("previous_close"),
            f"snapshot.stocks.{forecast_row['code']}.previous_close",
        )
        if previous_close <= 0:
            raise ValueError(
                f"snapshot.stocks.{forecast_row['code']}.previous_close: must be positive"
            )
        actual_pct = round((close / previous_close - 1) * 100, 6)
        if not math.isfinite(actual_pct):
            raise ValueError(
                f"snapshot.stocks.{forecast_row['code']}.actual_pct: must be finite"
            )
        records.append(_evaluated(base, forecast_row, actual_pct, threshold))

    return records


def _validated_record(record: Any, index: int) -> dict[str, Any]:
    row = dict(_mapping(record, f"records[{index}]"))
    forecast_date = _iso_date(row.get("forecast_date"), f"records[{index}].forecast_date")
    actual_date = _iso_date(row.get("actual_date"), f"records[{index}].actual_date")
    if date.fromisoformat(actual_date) <= date.fromisoformat(forecast_date):
        raise ValueError(f"records[{index}]: actual_date must be after forecast_date")
    asset_type = row.get("asset_type")
    if asset_type not in ASSET_GROUPS:
        raise ValueError(f"records[{index}].asset_type: must be index, sector, or stock")
    _text(row.get("asset_code"), f"records[{index}].asset_code")
    status = row.get("status")
    if status not in {"evaluated", "not_evaluable"}:
        raise ValueError(
            f"records[{index}].status: must be evaluated or not_evaluable"
        )
    if status == "evaluated":
        for field in ("actual_pct", "lower", "upper", "midpoint", "error", "absolute_error"):
            row[field] = _finite_number(row.get(field), f"records[{index}].{field}")
        if not isinstance(row.get("direction_hit"), bool):
            raise ValueError(f"records[{index}].direction_hit: must be boolean")
        if not isinstance(row.get("range_hit"), bool):
            raise ValueError(f"records[{index}].range_hit: must be boolean")
    elif "absolute_error" in row:
        raise ValueError(
            f"records[{index}]: not_evaluable row must not contain absolute_error"
        )
    return row


def _validated_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        _validated_record(record, index)
        for index, record in enumerate(_sequence(records, "records"))
    ]


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    evaluable = [row for row in rows if row["status"] == "evaluated"]
    count = len(evaluable)
    return {
        "sample_count": count,
        "direction_hit_rate": (
            sum(row["direction_hit"] for row in evaluable) / count if count else None
        ),
        "range_hit_rate": (
            sum(row["range_hit"] for row in evaluable) / count if count else None
        ),
        "mean_absolute_error": (
            math.fsum(row["absolute_error"] / count for row in evaluable)
            if count
            else None
        ),
    }


def _require_finite_summary_floats(value: Any, label: str = "summary") -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label}: summary float must be finite")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _require_finite_summary_floats(child, f"{label}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, child in enumerate(value):
            _require_finite_summary_floats(child, f"{label}[{index}]")


def summarize_evaluations(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate cumulative direction/range hit rates and MAE by asset class."""

    rows = _validated_records(records)
    summary = {
        group: _aggregate([row for row in rows if row["asset_type"] == asset_type])
        for asset_type, group in ASSET_GROUPS.items()
    }
    summary["overall"] = _aggregate(rows)
    _require_finite_summary_floats(summary)
    return summary


def _restore_file(path: Path, existed: bool, content: bytes | None) -> None:
    if existed:
        if content is None:
            raise RuntimeError(f"cannot restore {path}: original content missing")
        atomic_write_text(path, content.decode("utf-8"))
    else:
        path.unlink(missing_ok=True)


def _rollback_files(
    files: Sequence[tuple[str, Path, bool, bytes | None]],
) -> list[tuple[str, BaseException]]:
    failures: list[tuple[str, BaseException]] = []
    for label, path, existed, content in files:
        try:
            _restore_file(path, existed, content)
        except BaseException as error:
            failures.append((label, error))
    return failures


def _atomic_write_summary(path: Path, summary: Mapping[str, Any]) -> None:
    _require_finite_summary_floats(summary)
    text = json.dumps(
        summary,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    atomic_write_text(path, text + "\n")


def persist_evaluation(
    records: Sequence[Mapping[str, Any]],
    *,
    detail_path: str | Path,
    summary_path: str | Path,
) -> dict[str, Any]:
    """Idempotently upsert detail rows and atomically refresh cumulative metrics."""

    incoming = _validated_records(records)
    if not incoming:
        raise ValueError("records: must contain at least one evaluation")
    detail = Path(detail_path)
    summary_target = Path(summary_path)
    if detail.absolute() == summary_target.absolute():
        raise ValueError("detail_path and summary_path must be different files")

    detail_existed = detail.exists()
    summary_existed = summary_target.exists()
    old_detail = detail.read_bytes() if detail_existed else None
    old_summary = summary_target.read_bytes() if summary_existed else None

    existing: list[dict[str, Any]] = []
    if old_detail is not None:
        try:
            decoded = old_detail.decode("utf-8")
            existing = [
                _validated_record(json.loads(line), index)
                for index, line in enumerate(decoded.splitlines())
                if line.strip()
            ]
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"detail_path: invalid UTF-8 JSONL: {error}") from error

    merged = {
        tuple(row[field] for field in EVALUATION_KEY): row
        for row in existing
    }
    for row in incoming:
        merged[tuple(row[field] for field in EVALUATION_KEY)] = row
    cumulative = [merged[key] for key in sorted(merged)]
    cumulative_summary = summarize_evaluations(cumulative)
    detail_text = "".join(
        json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in cumulative
    )

    try:
        atomic_write_text(detail, detail_text)
        _atomic_write_summary(summary_target, cumulative_summary)
    except BaseException as persistence_error:
        rollback_failures = _rollback_files(
            (
                ("detail_path", detail, detail_existed, old_detail),
                ("summary_path", summary_target, summary_existed, old_summary),
            )
        )
        if rollback_failures:
            details = "; ".join(
                f"{label}: {error}" for label, error in rollback_failures
            )
            raise EvaluationPersistenceError(
                f"persistence failed: {persistence_error}; rollback failed: {details}"
            ) from persistence_error
        raise
    return cumulative_summary
