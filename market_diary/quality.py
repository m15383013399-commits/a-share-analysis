from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, time
from numbers import Integral, Real
from typing import Any

from .models import QualityResult


REQUIRED_INDEX_CODES: frozenset[str] = frozenset(
    {"000001", "399001", "399006", "000688", "000905", "000300"}
)


def validate_snapshot(
    snapshot: Mapping[str, Any],
    *,
    watch_codes: Sequence[str],
    min_stock_count: int = 5000,
    index_pct_tolerance: float = 0.05,
    index_level_tolerance_pct: float = 0.05,
    required_index_codes: Sequence[str] = tuple(REQUIRED_INDEX_CODES),
) -> QualityResult:
    index_pct_tolerance = _quality_tolerance(
        index_pct_tolerance, "index_pct_tolerance"
    )
    index_level_tolerance_pct = _quality_tolerance(
        index_level_tolerance_pct, "index_level_tolerance_pct"
    )
    required_index_set = _required_index_set(required_index_codes)
    errors: list[str] = []
    warnings: list[str] = []
    trade_date = str(snapshot.get("trade_date") or "")
    if not _valid_trade_date(trade_date):
        errors.append("target trade date is missing or invalid")

    raw_indices = snapshot.get("indices")
    indices = _mapping_rows(raw_indices)
    if not isinstance(raw_indices, (list, tuple)) or len(indices) != len(raw_indices):
        errors.append("index rows must be objects")
    index_codes = [str(row.get("code") or "") for row in indices]
    index_by_code = {str(row.get("code") or ""): row for row in indices}
    duplicate_index_codes = sorted(
        code or "<empty>"
        for code, count in Counter(index_codes).items()
        if count > 1
    )
    if duplicate_index_codes:
        errors.append(f"duplicate index code: {','.join(duplicate_index_codes)}")
    for code in sorted(required_index_set - set(index_codes)):
        errors.append(f"missing required index: {code}")
    for code in sorted(set(index_codes) - required_index_set):
        errors.append(f"unexpected index: {code or '<empty>'}")
    for code, row in sorted(index_by_code.items()):
        if str(row.get("trade_date") or "") != trade_date:
            errors.append(f"index {code} trade date mismatch")
        if code not in required_index_set:
            continue
        if _positive_float(row.get("close")) is None:
            errors.append(f"index {code} official close is missing or invalid")

    raw_stocks = snapshot.get("stocks")
    stocks = _mapping_rows(raw_stocks)
    if not isinstance(raw_stocks, (list, tuple)) or len(stocks) != len(raw_stocks):
        errors.append("stock rows must be objects")
    stock_codes = [str(row.get("code") or "").strip() for row in stocks]
    valid_quote_flags = [
        _valid_stock_code(code) and _valid_stock_quote(row, trade_date)
        for code, row in zip(stock_codes, stocks)
    ]
    valid_stock_count = sum(valid_quote_flags)
    if valid_stock_count < min_stock_count:
        errors.append(
            f"stock sample count below {min_stock_count}: {valid_stock_count} valid rows"
        )
    invalid_codes = sorted(
        {code or "<empty>" for code in stock_codes if not _valid_stock_code(code)}
    )
    if invalid_codes:
        errors.append(f"invalid stock code: {','.join(invalid_codes[:10])}")
    duplicates = sorted(
        code or "<empty>"
        for code, count in Counter(stock_codes).items()
        if count > 1
    )
    if duplicates:
        errors.append(f"duplicate stock code: {','.join(duplicates[:10])}")
    invalid_quotes = [
        code or "<empty>"
        for code, row in zip(stock_codes, stocks)
        if not _valid_stock_quote(row, trade_date)
    ]
    if invalid_quotes:
        errors.append(f"invalid stock quote: {','.join(invalid_quotes[:10])}")
    mismatched_stock_dates = sum(
        str(row.get("trade_date") or "") != trade_date for row in stocks
    )
    if mismatched_stock_dates:
        errors.append(
            f"stock trade date mismatch: {mismatched_stock_dates} rows do not match {trade_date}"
        )

    breadth = snapshot.get("breadth")
    breadth = breadth if isinstance(breadth, Mapping) else {}
    for amount_key in ("sh_amount", "sz_amount"):
        if _positive_float(breadth.get(amount_key)) is None:
            errors.append(f"{amount_key} must be positive")
    counts = breadth.get("counts")
    counts = counts if isinstance(counts, Mapping) else breadth
    parsed_counts = {
        key: _nonnegative_int(counts.get(key)) for key in ("up", "down", "flat")
    }
    if any(value is None for value in parsed_counts.values()):
        errors.append("breadth counts must be non-negative integers")
    breadth_total = sum(value for value in parsed_counts.values() if value is not None)
    declared_total = counts.get("total")
    declared_total_mismatch = (
        declared_total is not None and _nonnegative_int(declared_total) != valid_stock_count
    )
    if breadth_total != valid_stock_count or declared_total_mismatch:
        errors.append("breadth total does not equal valid stock count")
    derived_counts = Counter(
        str(row.get("direction"))
        for row, valid in zip(stocks, valid_quote_flags)
        if valid
    )
    if all(value is not None for value in parsed_counts.values()) and any(
        parsed_counts[key] != derived_counts[key] for key in ("up", "down", "flat")
    ):
        errors.append("breadth counts do not match valid stock quote directions")

    normalized_watch_codes = [str(code).strip() for code in watch_codes]
    stock_by_code = {str(row.get("code") or "").strip(): row for row in stocks}
    invalid_watch_codes = [
        code
        for code in normalized_watch_codes
        if not _is_official_close(stock_by_code.get(code), trade_date)
    ]
    if len(normalized_watch_codes) != 5 or len(set(normalized_watch_codes)) != 5:
        errors.append("watch pool must contain exactly five distinct official closes")
    elif invalid_watch_codes:
        errors.append(
            "watch pool must contain exactly five official closes for target trade date: "
            + ",".join(invalid_watch_codes)
        )

    sectors = snapshot.get("sectors")
    sectors = sectors if isinstance(sectors, Mapping) else {}
    industries = sectors.get("industries", sectors.get("industry"))
    concepts = sectors.get("concepts", sectors.get("concept"))
    industry_count = len(industries) if isinstance(industries, (list, tuple)) else 0
    concept_count = len(concepts) if isinstance(concepts, (list, tuple)) else 0
    if industry_count < 30:
        warnings.append("industry sector count below 30; mark proxy/missing")
    if concept_count < 50:
        warnings.append("concept sector count below 50; mark proxy/missing")

    sources = snapshot.get("sources")
    sources = sources if isinstance(sources, Mapping) else {}
    comparison_present = "index_comparisons" in sources or "source_comparisons" in snapshot
    raw_comparisons = (
        sources.get("index_comparisons")
        if "index_comparisons" in sources
        else snapshot.get("source_comparisons")
    )
    comparisons: list[Mapping[str, Any]] = []
    if not comparison_present:
        warnings.append("index cross-source comparison unavailable")
    elif not isinstance(raw_comparisons, (list, tuple)):
        errors.append("index cross-source comparison field invalid")
    else:
        comparisons = _mapping_rows(raw_comparisons)
        if len(comparisons) != len(raw_comparisons):
            errors.append("index cross-source comparison field invalid")
        comparison_codes = [str(row.get("code") or "") for row in comparisons]
        if (
            len(comparison_codes) != len(required_index_set)
            or len(set(comparison_codes)) != len(comparison_codes)
            or set(comparison_codes) != required_index_set
        ):
            errors.append("index cross-source comparison set must match fixed six indices")
    index_source_meta = sources.get("indices")
    selected_index_source = (
        index_source_meta.get("source")
        if isinstance(index_source_meta, Mapping)
        else None
    )
    expected_source_pair: tuple[Any, Any] | None = None
    for comparison in comparisons:
        code = str(comparison.get("code") or "unknown")
        primary = comparison.get("primary")
        secondary = comparison.get("secondary")
        if not isinstance(primary, Mapping) or not isinstance(secondary, Mapping):
            errors.append(f"index {code} cross-source comparison invalid")
            continue
        primary_source = primary.get("source")
        secondary_source = secondary.get("source")
        source_pair = (primary_source, secondary_source)
        if expected_source_pair is None:
            expected_source_pair = source_pair
        canonical = index_by_code.get(code)
        canonical_pct = _float(canonical.get("pct")) if canonical is not None else None
        canonical_close = (
            _positive_float(canonical.get("close")) if canonical is not None else None
        )
        primary_pct = _float(primary.get("pct"))
        secondary_pct = _float(secondary.get("pct"))
        primary_close = _positive_float(primary.get("close"))
        secondary_close = _positive_float(secondary.get("close"))
        if (
            primary_source not in ("tencent", "akshare")
            or secondary_source not in ("tencent", "akshare")
            or primary_source == secondary_source
            or source_pair != expected_source_pair
            or primary_source != selected_index_source
            or canonical_pct is None
            or canonical_close is None
            or primary_pct is None
            or primary_close is None
            or not math.isclose(primary_pct, canonical_pct, rel_tol=1e-12, abs_tol=1e-9)
            or not math.isclose(primary_close, canonical_close, rel_tol=1e-12, abs_tol=1e-9)
        ):
            errors.append(f"index {code} cross-source comparison authenticity invalid")
        if None in (primary_pct, secondary_pct, primary_close, secondary_close):
            errors.append(f"index {code} cross-source comparison invalid")
            continue
        pct_gap = abs(primary_pct - secondary_pct)
        level_gap_pct = abs(primary_close - secondary_close) / primary_close * 100
        if pct_gap > index_pct_tolerance or level_gap_pct > index_level_tolerance_pct:
            errors.append(f"index {code} cross-source difference exceeds tolerance")

    return QualityResult(
        "failed_quality_gate" if errors else "passed",
        tuple(errors),
        tuple(warnings),
    )


def _mapping_rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _required_index_set(value: Sequence[str]) -> frozenset[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("required_index_codes must be a sequence of codes")
    codes = tuple(str(code).strip() for code in value)
    if len(codes) != len(set(codes)) or set(codes) != REQUIRED_INDEX_CODES:
        raise ValueError(
            "required_index_codes must contain each fixed six index code exactly once"
        )
    return frozenset(codes)


def _valid_trade_date(value: str) -> bool:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _quality_tolerance(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise ValueError(f"{field} must be a finite nonnegative real number")
    return float(value)


def _valid_stock_code(value: str) -> bool:
    return re.fullmatch(
        r"(?:(?:600|601|603|605|688|689|000|001|002|003|300|301)[0-9]{3})",
        value,
    ) is not None


def _valid_stock_quote(row: Mapping[str, Any], trade_date: str) -> bool:
    if str(row.get("trade_date") or "") != trade_date:
        return False
    close = _positive_float(row.get("close"))
    previous_close = _positive_float(row.get("previous_close"))
    pct = _float(row.get("pct"))
    amount = _float(row.get("amount"))
    direction = row.get("direction")
    if (
        close is None
        or previous_close is None
        or pct is None
        or amount is None
        or amount < 0
        or direction not in {"up", "down", "flat"}
    ):
        return False
    derived_pct = round((close / previous_close - 1) * 100, 6)
    derived_direction = "up" if derived_pct > 0 else "down" if derived_pct < 0 else "flat"
    return abs(pct - derived_pct) <= 0.0000005 and direction == derived_direction


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_float(value: Any) -> float | None:
    number = _float(value)
    return number if number is not None and number > 0 else None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0:
        return None
    return int(value)


def _is_official_close(row: Mapping[str, Any] | None, trade_date: str) -> bool:
    if row is None:
        return False
    quote_time = _strict_time(row.get("quote_time"))
    return (
        str(row.get("trade_date") or "") == trade_date
        and _positive_float(row.get("close")) is not None
        and quote_time is not None
        and quote_time >= time(15, 0)
    )


def _strict_time(value: Any) -> time | None:
    if not isinstance(value, str) or re.fullmatch(r"\d{2}:\d{2}:\d{2}", value) is None:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None
