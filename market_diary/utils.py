from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .storage import atomic_write_json

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def now_text(timezone: str = "Asia/Shanghai") -> str:
    if ZoneInfo is None:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S")


def normalize_date(value: str | None = None, timezone: str = "Asia/Shanghai") -> str:
    if not value:
        if ZoneInfo is None:
            return datetime.now().strftime("%Y-%m-%d")
        return datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")

    value = str(value).strip()
    if re.fullmatch(r"\d{8}", value):
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    raise ValueError(f"Unsupported date format: {value}. Use YYYY-MM-DD or YYYYMMDD.")


def compact_date(value: str) -> str:
    return normalize_date(value).replace("-", "")


def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json_file(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_file(path: str | Path, data: Any) -> None:
    atomic_write_json(path, data)


def records_from_table(table: Any) -> list[dict[str, Any]]:
    if table is None:
        return []
    if isinstance(table, list):
        return [dict(item) for item in table if isinstance(item, dict)]
    if hasattr(table, "to_dict"):
        try:
            return table.to_dict(orient="records")
        except TypeError:
            return table.to_dict("records")
    return []


def pick(row: dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def pick_column_name(rows: list[dict[str, Any]], names: Iterable[str]) -> str | None:
    if not rows:
        return None
    keys = set()
    for row in rows[:20]:
        keys.update(row.keys())
    for name in names:
        if name in keys:
            return name
    return None


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    text = str(value).strip()
    if not text or text in {"-", "--", "nan", "None"}:
        return default
    multiplier = 1.0
    if text.endswith("%"):
        text = text[:-1]
    if text.endswith("万亿"):
        multiplier = 1_000_000_000_000
        text = text[:-2]
    elif text.endswith("亿"):
        multiplier = 100_000_000
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 10_000
        text = text[:-1]
    text = text.replace(",", "")
    try:
        return float(text) * multiplier
    except ValueError:
        return default


def format_pct(value: Any) -> str:
    return f"{to_float(value):.2f}%"


def format_money(value: Any) -> str:
    amount = to_float(value)
    if amount >= 1_000_000_000_000:
        return f"{amount / 1_000_000_000_000:.2f}万亿"
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:.1f}亿"
    if amount >= 10_000:
        return f"{amount / 10_000:.1f}万"
    return f"{amount:.0f}"


def safe_filename(text: str) -> str:
    text = re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("_")
    return text or "report"


def top_n(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    return rows[: max(0, n)]
