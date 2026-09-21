from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class MarketGateResult:
    status: Literal["closed", "market_not_stable", "ready"]
    trade_date: str
    next_trade_date: str | None
    reason: str


@dataclass(frozen=True)
class QualityResult:
    status: Literal["passed", "failed_quality_gate"]
    required_errors: tuple[str, ...]
    optional_warnings: tuple[str, ...]
