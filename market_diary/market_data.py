from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence


INDEX_CODES = {
    "sh000001": ("000001", "上证指数"),
    "sz399001": ("399001", "深证成指"),
    "sz399006": ("399006", "创业板指"),
    "sh000688": ("000688", "科创50"),
    "sh000905": ("000905", "中证500"),
    "sh000300": ("000300", "沪深300"),
}
REQUIRED_INDEX_CODES = tuple(code for code, _name in INDEX_CODES.values())
INDEX_SOURCE_NAMES = frozenset({"tencent", "akshare"})
BREADTH_SOURCE_NAMES = frozenset({"sina", "akshare"})
SECTOR_SOURCE_NAMES = frozenset({"eastmoney", "sina"})

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
SINA_STOCK_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
EASTMONEY_BOARD_URL = "https://push2.eastmoney.com/api/qt/clist/get"
SINA_INDUSTRY_URL = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
SINA_CONCEPT_URL = "https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php"
SINA_MIN_PCT_TOLERANCE = 0.0001
TWO_DECIMAL_PCT_TOLERANCE = 0.0050005


class MarketDataError(RuntimeError):
    pass


@dataclass
class SourceResult:
    dataset: str
    source: str
    fetched_at: str
    rows: list[dict[str, Any]]
    warnings: list[str]
    comparisons: list[dict[str, Any]] = field(default_factory=list)


def require_float(value: Any, field: str) -> float:
    if value is None or isinstance(value, bool):
        raise MarketDataError(f"{field}: numeric value is required")
    if isinstance(value, str) and not value.strip():
        raise MarketDataError(f"{field}: numeric value is required")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MarketDataError(f"{field}: invalid numeric value {value!r}") from exc
    if not math.isfinite(number):
        raise MarketDataError(f"{field}: numeric value must be finite")
    return number


def _sina_pct_tolerance(raw_pct: Any) -> float:
    """Allow a reported Sina percentage to be rounded at its stated precision.

    The price-derived value remains canonical.  Sina responses observed in live
    use may carry three decimal places, so a fixed 0.0001pp tolerance would
    incorrectly reject a normal rounding difference of up to 0.0005pp.
    """

    text = str(raw_pct).strip()
    fractional = text.partition(".")[2]
    decimals = len(fractional) if fractional.isdigit() else 0
    rounding_tolerance = 0.5 * 10 ** (-decimals)
    return max(SINA_MIN_PCT_TOLERANCE, rounding_tolerance + 0.0000005)


def normalize_quote(
    *,
    code: str,
    name: str,
    trade_date: str,
    close: Any,
    previous_close: Any,
    amount: Any,
    source: str,
    quote_time: str,
) -> dict[str, Any]:
    close_value = require_float(close, f"{source}:{code}:close")
    previous_value = require_float(previous_close, f"{source}:{code}:previous_close")
    amount_value = require_float(amount, f"{source}:{code}:amount")
    if close_value <= 0:
        raise MarketDataError(f"{source}:{code}: close must be positive")
    if previous_value <= 0:
        raise MarketDataError(f"{source}:{code}: previous close must be positive")
    if amount_value < 0:
        raise MarketDataError(f"{source}:{code}: amount must be nonnegative")
    pct = round((close_value / previous_value - 1) * 100, 6)
    return {
        "code": code,
        "name": name,
        "trade_date": trade_date,
        "close": close_value,
        "previous_close": previous_value,
        "pct": pct,
        "amount": amount_value,
        "source": source,
        "quote_time": quote_time,
        "direction": "up" if pct > 0 else "down" if pct < 0 else "flat",
    }


def _bare_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith(("sh", "sz", "bj")):
        text = text[2:]
    if not re.fullmatch(r"\d{6}", text):
        raise MarketDataError(f"invalid security code {value!r}")
    return text


def _provider_code(code: str) -> str:
    bare = _bare_code(code)
    if bare.startswith(("4", "8")):
        return f"bj{bare}"
    if bare.startswith(("5", "6", "9")):
        return f"sh{bare}"
    return f"sz{bare}"


def _parse_tencent_timestamp(value: Any, expected_trade_date: str, code: str) -> tuple[str, str]:
    timestamp = str(value or "").strip()
    if not re.fullmatch(r"\d{14}", timestamp):
        raise MarketDataError(f"tencent:{code}: invalid quote timestamp {timestamp!r}")
    try:
        parsed = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise MarketDataError(
            f"tencent:{code}: invalid quote timestamp {timestamp!r}"
        ) from exc
    response_date = parsed.date().isoformat()
    if response_date != expected_trade_date:
        raise MarketDataError(
            f"tencent:{code}: response date {response_date} does not match {expected_trade_date}"
        )
    quote_time = parsed.time().isoformat()
    return response_date, quote_time


def _tencent_amount_yuan(fields: Sequence[str], code: str) -> float:
    price_volume_amount = fields[35].split("/")
    if len(price_volume_amount) >= 3 and price_volume_amount[2].strip():
        return require_float(price_volume_amount[2], f"tencent:{code}:amount")
    amount_ten_thousand_yuan = require_float(fields[37], f"tencent:{code}:amount_wan")
    return amount_ten_thousand_yuan * 10_000


def parse_tencent_quotes(
    payload: str,
    trade_date: str,
    *,
    code_names: Mapping[str, tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    mappings = dict(code_names or INDEX_CODES)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    pattern = re.compile(r'v_([a-z]{2}\d{6})="([^"]*)"')
    for match in pattern.finditer(payload):
        provider_code = match.group(1).lower()
        fields = match.group(2).split("~")
        if provider_code not in mappings:
            continue
        if len(fields) <= 37:
            raise MarketDataError(f"tencent:{provider_code}: incomplete quote")
        canonical_code, canonical_name = mappings[provider_code]
        response_date, quote_time = _parse_tencent_timestamp(
            fields[30],
            trade_date,
            canonical_code,
        )
        row = normalize_quote(
            code=canonical_code,
            name=canonical_name or fields[1].strip(),
            trade_date=response_date,
            close=fields[3],
            previous_close=fields[4],
            amount=_tencent_amount_yuan(fields, canonical_code),
            source="tencent",
            quote_time=quote_time,
        )
        if canonical_code not in seen:
            rows.append(row)
            seen.add(canonical_code)
    return rows


def _iter_sina_rows(payload: Any):
    if isinstance(payload, list):
        if all(isinstance(item, Mapping) for item in payload):
            yield from payload
        else:
            for item in payload:
                yield from _iter_sina_rows(item)
        return
    if isinstance(payload, Mapping):
        for key in ("data", "result", "items", "list"):
            if key in payload:
                yield from _iter_sina_rows(payload[key])
                return
        if any(key in payload for key in ("code", "symbol")):
            yield payload


def _pick(row: Mapping[str, Any], names: Sequence[str], field: str) -> Any:
    for name in names:
        if name in row and row[name] is not None and not (
            isinstance(row[name], str) and not row[name].strip()
        ):
            return row[name]
    raise MarketDataError(f"{field}: value is required")


def parse_sina_stock_pages(
    payload: Any,
    trade_date: str,
    *,
    source: str = "sina",
    invalid_warnings: list[str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _iter_sina_rows(payload):
        raw_code = (
            str(raw.get("code") or raw.get("symbol") or "<unknown>")
            if isinstance(raw, Mapping)
            else "<unknown>"
        )
        try:
            code = _bare_code(_pick(raw, ("code", "symbol"), f"{source}:code"))
            if _a_share_exchange(code) is None:
                continue
            if code in seen:
                continue
            name = str(_pick(raw, ("name",), f"{source}:{code}:name")).strip()
            close = _pick(raw, ("trade", "close", "price", "current"), f"{source}:{code}:close")
            previous_close = _pick(
                raw,
                ("settlement", "previous_close", "prev_close", "preclose"),
                f"{source}:{code}:previous_close",
            )
            amount = _pick(raw, ("amount", "turnover"), f"{source}:{code}:amount")
            pct = require_float(
                _pick(raw, ("changepercent", "pct", "chg_pct"), f"{source}:{code}:pct"),
                f"{source}:{code}:pct",
            )
            row = normalize_quote(
                code=code,
                name=name,
                trade_date=trade_date,
                close=close,
                previous_close=previous_close,
                amount=amount,
                source=source,
                quote_time=str(raw.get("ticktime") or raw.get("time") or ""),
            )
            pct_tolerance = (
                TWO_DECIMAL_PCT_TOLERANCE
                if source in {"akshare", "eastmoney"}
                else _sina_pct_tolerance(
                    raw.get("changepercent", raw.get("pct", raw.get("chg_pct")))
                )
            )
            if abs(pct - row["pct"]) > pct_tolerance:
                raise MarketDataError(
                    f"{source}:{code}: supplier pct {pct} does not match "
                    f"price-derived pct {row['pct']}"
                )
        except MarketDataError as exc:
            if invalid_warnings is None:
                raise
            invalid_warnings.append(
                f"{source}:{raw_code}: skipped invalid quote: {exc}"
            )
            continue
        rows.append(row)
        seen.add(code)
    return rows


def parse_eastmoney_boards(payload: Any, trade_date: str) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise MarketDataError("eastmoney: board response must be an object")
    data = payload.get("data")
    diff = data.get("diff") if isinstance(data, Mapping) else None
    if diff is None:
        return []
    if not isinstance(diff, list):
        raise MarketDataError("eastmoney: data.diff must be a list")
    rows: list[dict[str, Any]] = []
    for raw in diff:
        if not isinstance(raw, Mapping):
            raise MarketDataError("eastmoney: board row must be an object")
        code = str(_pick(raw, ("f12", "code"), "eastmoney:board:code")).strip()
        name = str(_pick(raw, ("f14", "name"), f"eastmoney:{code}:name")).strip()
        pct = require_float(
            _pick(raw, ("f3", "changepercent", "pct"), f"eastmoney:{code}:pct"),
            f"eastmoney:{code}:pct",
        )
        amount = require_float(
            _pick(raw, ("f6", "amount"), f"eastmoney:{code}:amount"),
            f"eastmoney:{code}:amount",
        )
        rows.append(
            {
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "pct": pct,
                "amount": amount,
                "source": "eastmoney",
                "direction": "up" if pct > 0 else "down" if pct < 0 else "flat",
            }
        )
    return rows


def parse_sina_boards(payload: str, trade_date: str) -> list[dict[str, Any]]:
    match = re.fullmatch(
        r"\s*var\s+[A-Za-z_$][\w$]*\s*=\s*(\{.*\})\s*;?\s*",
        payload,
        flags=re.DOTALL,
    )
    if match is None:
        raise MarketDataError("sina: board response has invalid JavaScript wrapper")
    try:
        encoded_rows = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise MarketDataError("sina: board response contains invalid JSON") from exc
    if not isinstance(encoded_rows, Mapping):
        raise MarketDataError("sina: board payload must be an object")
    rows: list[dict[str, Any]] = []
    for object_code, encoded_row in encoded_rows.items():
        if not isinstance(encoded_row, str):
            raise MarketDataError(f"sina:{object_code}: board row must be a string")
        fields = encoded_row.split(",")
        if len(fields) < 8:
            raise MarketDataError(f"sina:{object_code}: incomplete board row")
        code = fields[0].strip()
        name = fields[1].strip()
        if not code or not name:
            raise MarketDataError(f"sina:{object_code}: code and name are required")
        pct = require_float(fields[5], f"sina:{code}:pct")
        amount = require_float(fields[7], f"sina:{code}:amount")
        rows.append(
            {
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "pct": pct,
                "amount": amount,
                "source": "sina",
                "direction": "up" if pct > 0 else "down" if pct < 0 else "flat",
            }
        )
    return rows


def _a_share_exchange(code: str) -> str | None:
    if re.fullmatch(r"(?:600|601|603|605|688|689)[0-9]{3}", code):
        return "sh"
    if re.fullmatch(r"(?:000|001|002|003|300|301)[0-9]{3}", code):
        return "sz"
    return None


def _validated_source_order(
    value: Sequence[str],
    *,
    field_name: str,
    allowed: frozenset[str],
    require_all: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MarketDataError(f"{field_name} must be a list of source names")
    order = tuple(str(source).strip().lower() for source in value)
    if not order or len(set(order)) != len(order):
        raise MarketDataError(f"{field_name} must contain distinct source names")
    unknown = set(order) - allowed
    if unknown:
        raise MarketDataError(
            f"{field_name} contains unsupported sources: {', '.join(sorted(unknown))}"
        )
    if require_all and set(order) != allowed:
        raise MarketDataError(
            f"{field_name} must contain exactly: {', '.join(sorted(allowed))}"
        )
    return order


def _validated_required_index_codes(value: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MarketDataError("required_index_codes must be a list of codes")
    codes = tuple(str(code).strip() for code in value)
    if len(codes) != len(set(codes)) or set(codes) != set(REQUIRED_INDEX_CODES):
        raise MarketDataError(
            "required_index_codes must contain each fixed six index code exactly once"
        )
    return codes


def _merge_watch_quotes(
    universe: Sequence[Mapping[str, Any]],
    watch_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    watch_by_code = {str(row.get("code") or ""): row for row in watch_rows}
    overlay_fields = (
        "trade_date",
        "close",
        "previous_close",
        "pct",
        "quote_time",
        "amount",
        "source",
        "fetched_at",
        "direction",
    )
    merged_rows: list[dict[str, Any]] = []
    for universe_row in universe:
        merged = dict(universe_row)
        watch_row = watch_by_code.get(str(merged.get("code") or ""))
        if watch_row is not None:
            for field in overlay_fields:
                if field in watch_row:
                    merged[field] = watch_row[field]
        merged_rows.append(merged)
    return merged_rows


def _summarize_breadth(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    counts = {
        "up": sum(row.get("direction") == "up" for row in rows),
        "down": sum(row.get("direction") == "down" for row in rows),
        "flat": sum(row.get("direction") == "flat" for row in rows),
        "total": len(rows),
    }
    amounts = {"sh": 0.0, "sz": 0.0}
    unclassified_codes: list[str] = []
    for row in rows:
        code = str(row.get("code") or "").strip()
        exchange = _a_share_exchange(code)
        if exchange is None:
            unclassified_codes.append(code or "<empty>")
            continue
        amounts[exchange] += require_float(row.get("amount"), f"breadth:{code}:amount")
    warnings = [
        "breadth: non-SH/SZ codes excluded from exchange amount totals: "
        + ", ".join(sorted(set(unclassified_codes)))
    ] if unclassified_codes else []
    return (
        {
            "counts": counts,
            "sh_amount": amounts["sh"],
            "sz_amount": amounts["sz"],
            "amount": amounts["sh"] + amounts["sz"],
        },
        warnings,
    )


class MarketDataClient:
    def __init__(
        self,
        *,
        get: Callable[..., Any],
        now: Callable[[], datetime] | None = None,
        timeout_seconds: float = 8,
        akshare_fetch: Callable[[str], Mapping[str, Any]] | None = None,
        index_sources: Sequence[str] = ("tencent", "akshare"),
        breadth_sources: Sequence[str] = ("sina", "akshare"),
        sector_sources: Sequence[str] = ("eastmoney", "sina"),
        required_index_codes: Sequence[str] = REQUIRED_INDEX_CODES,
        min_breadth_count: int = 0,
    ) -> None:
        self.min_breadth_count = min_breadth_count
        self.get = get
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.timeout_seconds = timeout_seconds
        self._akshare_fetch = akshare_fetch
        self._akshare_cache: dict[str, Mapping[str, Any]] = {}
        self.index_sources = _validated_source_order(
            index_sources,
            field_name="index_sources",
            allowed=INDEX_SOURCE_NAMES,
            require_all=True,
        )
        self.breadth_sources = _validated_source_order(
            breadth_sources,
            field_name="breadth_sources",
            allowed=BREADTH_SOURCE_NAMES,
        )
        self.sector_sources = _validated_source_order(
            sector_sources,
            field_name="sector_sources",
            allowed=SECTOR_SOURCE_NAMES,
        )
        self.required_index_codes = _validated_required_index_codes(required_index_codes)

    def _first_success(
        self,
        dataset: str,
        attempts: Sequence[tuple[str, Callable[[], list[dict[str, Any]]]]],
    ) -> SourceResult:
        warnings: list[str] = []
        for source, fetch in attempts:
            try:
                rows = fetch()
                if rows:
                    fetched_at = self.now().isoformat()
                    timestamped_rows = [{**row, "fetched_at": fetched_at} for row in rows]
                    return SourceResult(dataset, source, fetched_at, timestamped_rows, warnings)
                warnings.append(f"{source}: empty response")
            except Exception as exc:
                warnings.append(f"{source}: {type(exc).__name__}: {exc}")
        return SourceResult(dataset, "none", self.now().isoformat(), [], warnings)

    def _request(self, url: str, params: Mapping[str, Any]) -> Any:
        response = self.get(url, params=dict(params), timeout=self.timeout_seconds)
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        return response

    def _response_json(self, response: Any) -> Any:
        if isinstance(response, (Mapping, list)):
            return response
        parse = getattr(response, "json", None)
        if not callable(parse):
            raise MarketDataError("HTTP response does not provide JSON")
        return parse()

    def _response_text(self, response: Any) -> str:
        if isinstance(response, str):
            return response
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            raise MarketDataError("HTTP response does not provide text")
        return text

    def _akshare_payload(self, trade_date: str) -> Mapping[str, Any]:
        if trade_date not in self._akshare_cache:
            if self._akshare_fetch is not None:
                payload = self._akshare_fetch(trade_date)
            else:
                from .providers import AkshareProvider

                payload = AkshareProvider(
                    include_news=False,
                    timeout_seconds=self.timeout_seconds,
                ).fetch(trade_date)
            self._akshare_cache[trade_date] = payload
        return self._akshare_cache[trade_date]

    def _fetch_tencent(
        self,
        codes: Sequence[str],
        trade_date: str,
        *,
        index_symbols: bool = False,
    ) -> list[dict[str, Any]]:
        mappings: dict[str, tuple[str, str]] = {}
        index_providers = {code: provider for provider, (code, _) in INDEX_CODES.items()}
        for code in codes:
            bare = _bare_code(code)
            provider_code = index_providers.get(bare) if index_symbols else None
            provider_code = provider_code or _provider_code(bare)
            canonical_name = INDEX_CODES.get(provider_code, (bare, ""))[1]
            mappings[provider_code] = (bare, canonical_name)
        response = self._request(TENCENT_QUOTE_URL + ",".join(mappings), {})
        rows = parse_tencent_quotes(self._response_text(response), trade_date, code_names=mappings)
        missing = sorted(set(code for code, _ in mappings.values()) - {row["code"] for row in rows})
        if missing:
            raise MarketDataError(f"tencent: missing requested codes: {', '.join(missing)}")
        return rows

    def _parse_akshare_quotes(
        self,
        raw_rows: Any,
        codes: Sequence[str],
        trade_date: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_rows, list):
            raise MarketDataError("akshare: quote rows must be a list")
        wanted = {_bare_code(code) for code in codes}
        rows: list[dict[str, Any]] = []
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                raise MarketDataError("akshare: quote row must be an object")
            code = _bare_code(_pick(raw, ("代码", "code", "symbol"), "akshare:code"))
            if code not in wanted:
                continue
            provider_code = _provider_code(code)
            default_name = INDEX_CODES.get(provider_code, (code, ""))[1]
            rows.append(
                normalize_quote(
                    code=code,
                    name=str(raw.get("名称") or raw.get("name") or default_name).strip(),
                    trade_date=trade_date,
                    close=_pick(raw, ("最新价", "close", "trade"), f"akshare:{code}:close"),
                    previous_close=_pick(
                        raw,
                        ("昨收", "previous_close", "prev_close", "settlement"),
                        f"akshare:{code}:previous_close",
                    ),
                    amount=_pick(raw, ("成交额", "amount"), f"akshare:{code}:amount"),
                    source="akshare",
                    quote_time=str(raw.get("时间") or raw.get("quote_time") or ""),
                )
            )
        missing = sorted(wanted - {row["code"] for row in rows})
        if missing:
            raise MarketDataError(f"akshare: missing requested codes: {', '.join(missing)}")
        return rows

    def fetch_index_quotes(self, codes: Sequence[str], trade_date: str) -> SourceResult:
        fetches: dict[str, Callable[[], list[dict[str, Any]]]] = {
            "tencent": lambda: self._fetch_tencent(
                codes, trade_date, index_symbols=True
            ),
            "akshare": lambda: self._parse_akshare_quotes(
                self._akshare_payload(trade_date).get("indices", []),
                codes,
                trade_date,
            ),
        }
        warnings: list[str] = []
        successes: list[SourceResult] = []
        for source in self.index_sources:
            try:
                rows = fetches[source]()
                if not rows:
                    warnings.append(f"{source}: empty response")
                    continue
                fetched_at = self.now().isoformat()
                successes.append(
                    SourceResult(
                        "indices",
                        source,
                        fetched_at,
                        [{**row, "fetched_at": fetched_at} for row in rows],
                        [],
                    )
                )
            except Exception as exc:
                warnings.append(f"{source}: {type(exc).__name__}: {exc}")
        if not successes:
            return SourceResult("indices", "none", self.now().isoformat(), [], warnings)

        primary = successes[0]
        comparisons: list[dict[str, Any]] = []
        if len(successes) > 1:
            secondary = successes[1]
            secondary_by_code = {row["code"]: row for row in secondary.rows}
            for row in primary.rows:
                other = secondary_by_code.get(row["code"])
                if other is None:
                    continue
                comparisons.append(
                    {
                        "code": row["code"],
                        "primary": {
                            "source": primary.source,
                            "pct": row["pct"],
                            "close": row["close"],
                        },
                        "secondary": {
                            "source": secondary.source,
                            "pct": other["pct"],
                            "close": other["close"],
                        },
                    }
                )
        return SourceResult(
            "indices",
            primary.source,
            primary.fetched_at,
            primary.rows,
            warnings,
            comparisons,
        )

    def fetch_close_stability_quotes(
        self, codes: Sequence[str], trade_date: str
    ) -> SourceResult:
        return self._first_success(
            "close_stability",
            (
                (
                    "tencent",
                    lambda: self._fetch_tencent(
                        codes, trade_date, index_symbols=True
                    ),
                ),
            ),
        )

    def fetch_watch_quotes(self, codes: Sequence[str], trade_date: str) -> SourceResult:
        if not codes:
            return SourceResult("watch", "none", self.now().isoformat(), [], ["watch: no codes"])
        return self._first_success(
            "watch",
            (
                ("tencent", lambda: self._fetch_tencent(codes, trade_date)),
                (
                    "akshare",
                    lambda: self._parse_akshare_quotes(
                        self._akshare_payload(trade_date).get("stocks", []),
                        codes,
                        trade_date,
                    ),
                ),
            ),
        )

    def _fetch_sina_breadth(
        self, trade_date: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        pages: list[Any] = []
        page_size = 100
        for page in range(1, 201):
            response = self._request(
                SINA_STOCK_URL,
                {"page": page, "num": page_size, "sort": "symbol", "asc": 1, "node": "hs_a"},
            )
            payload = self._response_json(response)
            pages.append(payload)
            page_rows = list(_iter_sina_rows(payload))
            if len(page_rows) < page_size:
                break
        warnings: list[str] = []
        rows = parse_sina_stock_pages(
            pages, trade_date, invalid_warnings=warnings
        )
        return rows, warnings

    def _parse_akshare_breadth(
        self, trade_date: str
    ) -> tuple[list[dict[str, Any]], list[str]]:
        raw_rows = self._akshare_payload(trade_date).get("stocks", [])
        if not isinstance(raw_rows, list):
            raise MarketDataError("akshare: stock rows must be a list")
        translated: list[dict[str, Any]] = []
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                raise MarketDataError("akshare: stock row must be an object")
            translated.append(
                {
                    "code": _pick(raw, ("代码", "code", "symbol"), "akshare:code"),
                    "name": _pick(raw, ("名称", "name"), "akshare:name"),
                    "trade": _pick(raw, ("最新价", "close", "trade"), "akshare:close"),
                    "settlement": _pick(
                        raw,
                        ("昨收", "previous_close", "prev_close", "settlement"),
                        "akshare:previous_close",
                    ),
                    "changepercent": _pick(
                        raw,
                        ("涨跌幅", "pct", "changepercent"),
                        "akshare:pct",
                    ),
                    "amount": _pick(raw, ("成交额", "amount"), "akshare:amount"),
                    "ticktime": raw.get("时间") or raw.get("quote_time") or "",
                }
            )
        warnings: list[str] = []
        rows = parse_sina_stock_pages(
            translated,
            trade_date,
            source="akshare",
            invalid_warnings=warnings,
        )
        return rows, warnings

    def fetch_a_share_breadth(self, trade_date: str) -> SourceResult:
        attempts: dict[
            str, Callable[[], tuple[list[dict[str, Any]], list[str]]]
        ] = {
            "sina": lambda: self._fetch_sina_breadth(trade_date),
            "akshare": lambda: self._parse_akshare_breadth(trade_date),
        }
        warnings: list[str] = []
        for source in self.breadth_sources:
            try:
                rows, source_warnings = attempts[source]()
                warnings.extend(source_warnings)
                if len(rows) < self.min_breadth_count:
                    warnings.append(f"{source}: breadth incomplete ({len(rows)}/{self.min_breadth_count})")
                    continue
                if rows:
                    fetched_at = self.now().isoformat()
                    return SourceResult(
                        "breadth",
                        source,
                        fetched_at,
                        [{**row, "fetched_at": fetched_at} for row in rows],
                        warnings,
                    )
                warnings.append(f"{source}: empty response")
            except Exception as exc:
                warnings.append(f"{source}: {type(exc).__name__}: {exc}")
        return SourceResult("breadth", "none", self.now().isoformat(), [], warnings)

    def _fetch_eastmoney_boards(self, kind: str, trade_date: str) -> list[dict[str, Any]]:
        fs = "m:90+t:3" if kind == "concept" else "m:90+t:2"
        response = self._request(
            EASTMONEY_BOARD_URL,
            {"pn": 1, "pz": 500, "fs": fs, "fields": "f12,f14,f3,f6"},
        )
        return parse_eastmoney_boards(self._response_json(response), trade_date)

    def _fetch_sina_boards(self, kind: str, trade_date: str) -> list[dict[str, Any]]:
        if kind == "concept":
            url = SINA_CONCEPT_URL
            params = {"param": "class"}
        else:
            url = SINA_INDUSTRY_URL
            params = {}
        response = self._request(url, params)
        return parse_sina_boards(self._response_text(response), trade_date)

    def fetch_sectors(self, trade_date: str) -> tuple[SourceResult, SourceResult]:
        industry_attempts = {
            "eastmoney": lambda: self._fetch_eastmoney_boards("industry", trade_date),
            "sina": lambda: self._fetch_sina_boards("industry", trade_date),
        }
        concept_attempts = {
            "eastmoney": lambda: self._fetch_eastmoney_boards("concept", trade_date),
            "sina": lambda: self._fetch_sina_boards("concept", trade_date),
        }
        industries = self._first_success(
            "industry_sectors",
            tuple((source, industry_attempts[source]) for source in self.sector_sources),
        )
        concepts = self._first_success(
            "concept_sectors",
            tuple((source, concept_attempts[source]) for source in self.sector_sources),
        )
        return industries, concepts

    def build_snapshot(self, trade_date: str, watch_codes: Sequence[str]) -> dict[str, Any]:
        required_codes = list(self.required_index_codes)
        indices = self.fetch_index_quotes(required_codes, trade_date)
        watch = self.fetch_watch_quotes(watch_codes, trade_date)
        breadth = self.fetch_a_share_breadth(trade_date)
        industries, concepts = self.fetch_sectors(trade_date)
        results = (indices, breadth, industries, concepts, watch)
        warnings = [warning for result in results for warning in result.warnings]
        index_codes = {row["code"] for row in indices.rows}
        quality_ok = set(required_codes).issubset(index_codes) and bool(breadth.rows)
        stocks = _merge_watch_quotes(breadth.rows, watch.rows)
        breadth_summary, breadth_warnings = _summarize_breadth(stocks)
        warnings.extend(breadth_warnings)
        sources = {
            result.dataset: {
                "source": result.source,
                "fetched_at": result.fetched_at,
            }
            for result in results
        }
        if indices.comparisons:
            sources["index_comparisons"] = indices.comparisons
        return {
            "schema_version": "1.0",
            "trade_date": trade_date,
            "generated_at": self.now().isoformat(),
            "market_status": "ready" if indices.rows or breadth.rows else "data_unavailable",
            "quality_status": "passed" if quality_ok else "failed_quality_gate",
            "sources": sources,
            "indices": indices.rows,
            "breadth": breadth_summary,
            "sectors": {"industries": industries.rows, "concepts": concepts.rows},
            "stocks": stocks,
            "warnings": warnings,
        }
