from __future__ import annotations

import signal
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from .utils import compact_date, normalize_date, records_from_table


class ProviderTimeoutError(TimeoutError):
    pass


@contextmanager
def provider_timeout(seconds: float):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def handle_timeout(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise ProviderTimeoutError(f"timed out after {seconds:g}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    try:
        signal.signal(signal.SIGALRM, handle_timeout)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


@dataclass
class FetchLog:
    ok: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def record_ok(self, name: str, rows: list[dict[str, Any]]) -> None:
        self.ok[name] = len(rows)

    def record_warning(self, name: str, exc: Exception) -> None:
        self.warnings.append(f"{name}: {type(exc).__name__}: {exc}")

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "warnings": self.warnings}


class AkshareProvider:
    """Thin adapter around AkShare.

    AkShare wraps many public web endpoints. Function names and columns may
    change over time, so every call is isolated and non-fatal.
    """

    def __init__(self, include_news: bool = True, timeout_seconds: float = 20) -> None:
        try:
            import akshare as ak  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "AkShare is not installed. Run `python3 -m pip install -r requirements.txt`, "
                "or use `--sample` to test the report format."
            ) from exc
        self.ak = ak
        self.include_news = include_news
        self.timeout_seconds = timeout_seconds
        self.log = FetchLog()

    def fetch(self, trade_date: str | None = None) -> dict[str, Any]:
        normalized_date = normalize_date(trade_date)
        payload: dict[str, Any] = {
            "trade_date": normalized_date,
            "source": "akshare",
            "stocks": self._call("stocks", self.ak.stock_zh_a_spot_em),
            "industry_boards": self._call("industry_boards", self.ak.stock_board_industry_name_em),
            "concept_boards": self._call("concept_boards", self.ak.stock_board_concept_name_em),
            "limit_up_pool": self._call(
                "limit_up_pool",
                self.ak.stock_zt_pool_em,
                date=compact_date(normalized_date),
            ),
            "indices": self._fetch_indices(),
            "news": self._fetch_news() if self.include_news else [],
        }
        payload["status"] = self.log.as_dict()
        return payload

    def _call(self, name: str, fn: Callable[..., Any], **kwargs: Any) -> list[dict[str, Any]]:
        try:
            with provider_timeout(self.timeout_seconds):
                rows = records_from_table(fn(**kwargs))
            self.log.record_ok(name, rows)
            return rows
        except Exception as exc:  # noqa: BLE001 - provider errors must not kill the whole run
            self.log.record_warning(name, exc)
            return []

    def _fetch_indices(self) -> list[dict[str, Any]]:
        candidates: list[tuple[str, Callable[..., Any], dict[str, Any]]] = [
            ("indices_sina", self.ak.stock_zh_index_spot_sina, {}),
            ("indices_em", self.ak.stock_zh_index_spot_em, {"symbol": "沪深重要指数"}),
        ]
        for name, fn, kwargs in candidates:
            rows = self._call(name, fn, **kwargs)
            if rows:
                return rows
        return []

    def _fetch_news(self) -> list[dict[str, Any]]:
        candidates: list[tuple[str, Callable[..., Any], dict[str, Any]]] = []
        for func_name in ("stock_info_global_cls", "stock_info_global_sina", "stock_news_em"):
            fn = getattr(self.ak, func_name, None)
            if callable(fn):
                candidates.append((func_name, fn, {}))

        for name, fn, kwargs in candidates:
            rows = self._call(f"news_{name}", fn, **kwargs)
            if rows:
                return rows
        return []
