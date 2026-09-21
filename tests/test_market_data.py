from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from market_diary.calendar import evaluate_market_gate
from market_diary.market_data import (
    MarketDataError,
    MarketDataClient,
    normalize_quote,
    parse_eastmoney_boards,
    parse_sina_stock_pages,
    parse_tencent_quotes,
)
from market_diary.quality import validate_snapshot


FIXTURES = Path(__file__).with_name("fixtures")


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json(name: str) -> Any:
    return json.loads(load_text(name))


def fixed_now() -> datetime:
    return datetime(2026, 8, 11, 15, 5, tzinfo=timezone.utc)


WATCH_CODES = ("600000", "600001", "000001", "000002", "300001")
AKSHARE_INDICES = [
    {"代码": "000001", "名称": "上证指数", "最新价": 3320.50, "昨收": 3300.00, "成交额": 456789012.34},
    {"代码": "399001", "名称": "深证成指", "最新价": 10680.20, "昨收": 10650.00, "成交额": 356789012.34},
    {"代码": "399006", "名称": "创业板指", "最新价": 2210.4902, "昨收": 2203.00, "成交额": 156789012.34},
    {"代码": "000688", "名称": "科创50", "最新价": 1030.00, "昨收": 1025.00, "成交额": 96789012.34},
    {"代码": "000905", "名称": "中证500", "最新价": 6100.00, "昨收": 6075.00, "成交额": 86789012.34},
    {"代码": "000300", "名称": "沪深300", "最新价": 3900.00, "昨收": 3880.00, "成交额": 76789012.34},
]


class FakeResponse:
    def __init__(self, payload: Any = None, *, text: str | None = None, status: int = 200) -> None:
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)
        self.status_code = status

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class MarketDataTest(unittest.TestCase):
    def test_tencent_parser_returns_all_six_standard_indices(self) -> None:
        rows = parse_tencent_quotes(load_text("tencent_quotes.txt"), trade_date="2026-08-11")

        self.assertEqual(
            {row["code"] for row in rows},
            {"000001", "399001", "399006", "000688", "000905", "000300"},
        )
        self.assertEqual(next(row for row in rows if row["code"] == "000905")["name"], "中证500")
        self.assertEqual(next(row for row in rows if row["code"] == "399006")["pct"], 0.34)

    def test_tencent_1500_quote_does_not_pass_calendar_stability_gate(self) -> None:
        row = parse_tencent_quotes(
            load_text("tencent_quotes.txt"),
            trade_date="2026-08-11",
        )[0]

        result = evaluate_market_gate(
            now=datetime(2026, 8, 11, 7, 20, tzinfo=timezone.utc),
            target_date=date(2026, 8, 11),
            calendar={"year": 2026, "closed_dates": []},
            close_samples=[row],
        )

        self.assertEqual(row["quote_time"], "15:00:00")
        self.assertEqual(result.status, "market_not_stable")

    def test_tencent_previous_day_response_falls_back_with_warning(self) -> None:
        stale_payload = load_text("tencent_quotes.txt").replace("20260811", "20260810")

        def stale_tencent(url: str, **_: Any) -> FakeResponse:
            return FakeResponse(text=stale_payload)

        client = MarketDataClient(
            get=stale_tencent,
            now=fixed_now,
            akshare_fetch=lambda _: {"indices": []},
        )

        result = client.fetch_index_quotes(
            ["000001", "399001", "399006", "000688", "000905", "000300"],
            "2026-08-11",
        )

        self.assertEqual(result.source, "none")
        self.assertEqual(result.rows, [])
        self.assertIn("response date 2026-08-10 does not match 2026-08-11", " ".join(result.warnings))

    def test_tencent_parser_rejects_calendar_invalid_timestamp_components(self) -> None:
        payload = load_text("tencent_quotes.txt")
        for invalid in ("20261311150000", "20260811240000", "20260811156000", "20260811150060"):
            with self.subTest(timestamp=invalid), self.assertRaises(MarketDataError):
                parse_tencent_quotes(
                    payload.replace("20260811150000", invalid),
                    trade_date="2026-08-11",
                )

    def test_tencent_amount_is_normalized_to_yuan_from_both_schema_fields(self) -> None:
        rows = parse_tencent_quotes(load_text("tencent_quotes.txt"), "2026-08-11")

        composite_amount = next(row for row in rows if row["code"] == "000001")["amount"]
        ten_thousand_yuan_amount = next(row for row in rows if row["code"] == "399006")["amount"]

        self.assertAlmostEqual(composite_amount, 456789012.34, places=2)
        self.assertAlmostEqual(ten_thousand_yuan_amount, 156789012.34, places=2)

    def test_index_fetch_uses_exchange_specific_six_index_symbols(self) -> None:
        expected_symbols = set(
            ("sh000001", "sz399001", "sz399006", "sh000688", "sh000905", "sh000300")
        )

        def tencent_response(url: str, **_: Any) -> FakeResponse:
            requested_symbols = set(url.rsplit("=", 1)[-1].split(","))
            if requested_symbols != expected_symbols:
                raise AssertionError(f"wrong index symbols: {sorted(requested_symbols)}")
            return FakeResponse(text=load_text("tencent_quotes.txt"))

        client = MarketDataClient(
            get=tencent_response,
            now=fixed_now,
            akshare_fetch=lambda _: {"indices": []},
        )

        result = client.fetch_index_quotes(
            ["000001", "399001", "399006", "000688", "000905", "000300"],
            "2026-08-11",
        )

        self.assertEqual(result.source, "tencent")
        self.assertEqual(len(result.rows), 6)
        self.assertEqual(result.comparisons, [])
        self.assertTrue(any("akshare" in warning for warning in result.warnings))

    def test_index_fetch_attempts_both_sources_and_builds_real_comparisons(self) -> None:
        client = MarketDataClient(
            get=lambda *_args, **_kwargs: FakeResponse(text=load_text("tencent_quotes.txt")),
            now=fixed_now,
            akshare_fetch=lambda _: {"indices": AKSHARE_INDICES},
        )

        result = client.fetch_index_quotes(
            ["000001", "399001", "399006", "000688", "000905", "000300"],
            "2026-08-11",
        )

        self.assertEqual(result.source, "tencent")
        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.comparisons), 6)
        self.assertEqual(
            result.comparisons[0]["primary"]["source"],
            "tencent",
        )
        self.assertEqual(result.comparisons[0]["secondary"]["source"], "akshare")

    def test_configured_index_source_order_selects_primary_but_still_compares(self) -> None:
        calls: list[str] = []

        def tencent_get(*_args: Any, **_kwargs: Any) -> FakeResponse:
            calls.append("tencent")
            return FakeResponse(text=load_text("tencent_quotes.txt"))

        def akshare_fetch(_: str) -> dict[str, Any]:
            calls.append("akshare")
            return {"indices": AKSHARE_INDICES}

        client = MarketDataClient(
            get=tencent_get,
            now=fixed_now,
            akshare_fetch=akshare_fetch,
            index_sources=("akshare", "tencent"),
        )

        result = client.fetch_index_quotes(
            ["000001", "399001", "399006", "000688", "000905", "000300"],
            "2026-08-11",
        )

        self.assertEqual(calls, ["akshare", "tencent"])
        self.assertEqual(result.source, "akshare")
        self.assertEqual(len(result.comparisons), 6)

    def test_configured_breadth_source_order_is_consumed(self) -> None:
        calls: list[str] = []

        def unexpected_sina(*_args: Any, **_kwargs: Any) -> FakeResponse:
            calls.append("sina")
            raise AssertionError("sina should not run after akshare succeeds")

        def akshare_fetch(_: str) -> dict[str, Any]:
            calls.append("akshare")
            return {
                "stocks": [
                    {
                        "代码": "600000",
                        "名称": "浦发银行",
                        "最新价": 10.2,
                        "昨收": 10.0,
                        "涨跌幅": 2.0,
                        "成交额": 100.0,
                    }
                ]
            }

        client = MarketDataClient(
            get=unexpected_sina,
            now=fixed_now,
            akshare_fetch=akshare_fetch,
            breadth_sources=("akshare", "sina"),
        )

        result = client.fetch_a_share_breadth("2026-08-11")

        self.assertEqual(calls, ["akshare"])
        self.assertEqual(result.source, "akshare")

    def test_configured_sector_source_order_is_consumed(self) -> None:
        requested_urls: list[str] = []

        def sina_first(url: str, **_kwargs: Any) -> FakeResponse:
            requested_urls.append(url)
            if "eastmoney" in url:
                raise AssertionError("eastmoney should not run after sina succeeds")
            fixture = "sina_concepts.txt" if url.endswith("newFLJK.php") else "sina_industries.txt"
            return FakeResponse(text=load_text(fixture))

        client = MarketDataClient(
            get=sina_first,
            now=fixed_now,
            sector_sources=("sina", "eastmoney"),
        )

        industries, concepts = client.fetch_sectors("2026-08-11")

        self.assertEqual(industries.source, "sina")
        self.assertEqual(concepts.source, "sina")
        self.assertTrue(all("sina.com.cn" in url for url in requested_urls))

    def test_market_data_config_rejects_unknown_duplicate_or_incomplete_fixed_sets(self) -> None:
        invalid_kwargs = (
            {"index_sources": None},
            {"breadth_sources": 42},
            {"required_index_codes": None},
            {"index_sources": ("tencent", "unknown")},
            {"breadth_sources": ("sina", "sina")},
            {"sector_sources": ()},
            {"required_index_codes": ("000001",) * 6},
            {
                "required_index_codes": (
                    "000001",
                    "399001",
                    "399006",
                    "000688",
                    "000905",
                    "000016",
                )
            },
        )
        for kwargs in invalid_kwargs:
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketDataError):
                MarketDataClient(get=lambda *_args, **_kwargs: None, **kwargs)

    def test_sina_pages_are_deduplicated_and_keep_zero_change_as_flat(self) -> None:
        rows = parse_sina_stock_pages(load_json("sina_stocks.json"), "2026-08-11")

        self.assertEqual(len({row["code"] for row in rows}), len(rows))
        self.assertEqual(sum(row["direction"] == "flat" for row in rows), 1)
        self.assertEqual(
            {row["code"] for row in rows},
            {"600000", "600001", "000001", "000002", "300001"},
        )

    def test_sina_pct_is_cross_checked_but_never_overrides_price_derived_pct(self) -> None:
        raw = {
            "code": "600000",
            "name": "浦发银行",
            "trade": "10.20",
            "settlement": "10.00",
            "changepercent": "2.00004",
            "amount": "100",
            "ticktime": "15:00:00",
        }

        row = parse_sina_stock_pages([raw], "2026-08-11")[0]

        self.assertEqual(row["pct"], 2.0)
        self.assertEqual(row["direction"], "up")

    def test_sina_three_decimal_pct_rounding_is_accepted_without_overriding_prices(self) -> None:
        raw = {
            "code": "600001",
            "name": "邯郸钢铁",
            "trade": "8.00",
            "settlement": "8.035",
            "changepercent": "-0.436",
            "amount": "200000000",
            "ticktime": "15:00:00",
        }

        row = parse_sina_stock_pages([raw], "2026-08-12")[0]

        self.assertEqual(row["pct"], -0.435594)

    def test_sina_implausible_supplier_pct_rejects_source(self) -> None:
        raw = {
            "code": "600000",
            "name": "浦发银行",
            "trade": "10.20",
            "settlement": "10.00",
            "changepercent": "99",
            "amount": "100",
        }

        with self.assertRaisesRegex(MarketDataError, "supplier pct"):
            parse_sina_stock_pages([raw], "2026-08-11")

    def test_sina_supplier_pct_mismatch_falls_back_with_explicit_warning(self) -> None:
        inconsistent = [
            {
                "code": "600000",
                "name": "浦发银行",
                "trade": "10.20",
                "settlement": "10.00",
                "changepercent": "99",
                "amount": "100",
            }
        ]
        client = MarketDataClient(
            get=lambda *_args, **_kwargs: FakeResponse(inconsistent),
            now=fixed_now,
            akshare_fetch=lambda _: {
                "stocks": [
                    {
                        "代码": "600000",
                        "名称": "浦发银行",
                        "最新价": 10.2,
                        "昨收": 10.0,
                        "涨跌幅": 2.0,
                        "成交额": 100.0,
                    }
                ]
            },
        )

        result = client.fetch_a_share_breadth("2026-08-11")

        self.assertEqual(result.source, "akshare")
        self.assertTrue(any("supplier pct" in warning for warning in result.warnings))

    def test_sina_breadth_skips_one_invalid_quote_but_keeps_valid_universe(self) -> None:
        payload = [
            {
                "code": "600000",
                "name": "浦发银行",
                "trade": "10.20",
                "settlement": "10.00",
                "changepercent": "2.0",
                "amount": "100",
                "ticktime": "15:00:00",
            },
            {
                "code": "300333",
                "name": "兆日科技",
                "trade": "0",
                "settlement": "10.00",
                "changepercent": "-100.0",
                "amount": "0",
                "ticktime": "15:00:00",
            },
        ]
        client = MarketDataClient(
            get=lambda *_args, **_kwargs: FakeResponse(payload),
            now=fixed_now,
            akshare_fetch=lambda _: {"stocks": []},
        )

        result = client.fetch_a_share_breadth("2026-08-12")

        self.assertEqual(result.source, "sina")
        self.assertEqual([row["code"] for row in result.rows], ["600000"])
        self.assertTrue(any("300333" in warning and "skipped invalid" in warning for warning in result.warnings))

    def test_akshare_two_decimal_supplier_pct_accepts_rounding_but_keeps_derived_value(self) -> None:
        raw = {
            "code": "600001",
            "name": "邯郸钢铁",
            "trade": "8.00",
            "settlement": "8.10",
            "changepercent": "-1.23",
            "amount": "100",
        }

        row = parse_sina_stock_pages([raw], "2026-08-11", source="akshare")[0]

        self.assertEqual(row["pct"], -1.234568)
        self.assertEqual(row["direction"], "down")

    def test_akshare_two_decimal_supplier_pct_still_rejects_clear_conflict(self) -> None:
        raw = {
            "code": "600001",
            "name": "邯郸钢铁",
            "trade": "8.00",
            "settlement": "8.10",
            "changepercent": "-1.22",
            "amount": "100",
        }

        with self.assertRaisesRegex(MarketDataError, "supplier pct"):
            parse_sina_stock_pages([raw], "2026-08-11", source="akshare")

    def test_quote_normalization_requires_positive_prices_and_nonnegative_amount(self) -> None:
        invalid_values = (
            {"close": 0, "previous_close": 10, "amount": 100},
            {"close": 10, "previous_close": 0, "amount": 100},
            {"close": 10, "previous_close": 9, "amount": -1},
        )
        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(MarketDataError):
                normalize_quote(
                    code="600000",
                    name="浦发银行",
                    trade_date="2026-08-11",
                    source="test",
                    quote_time="15:00:00",
                    **values,
                )

    def test_sector_fetch_falls_back_to_sina(self) -> None:
        sina_requests: list[tuple[str, dict[str, Any]]] = []

        def eastmoney_fails_sina_succeeds(url: str, **kwargs: Any) -> FakeResponse:
            if "eastmoney" in url:
                return FakeResponse(status=503)
            sina_requests.append((url, kwargs["params"]))
            fixture = "sina_concepts.txt" if url.endswith("newFLJK.php") else "sina_industries.txt"
            return FakeResponse(text=load_text(fixture))

        client = MarketDataClient(get=eastmoney_fails_sina_succeeds, now=fixed_now)
        industries, concepts = client.fetch_sectors("2026-08-11")

        self.assertEqual(industries.source, "sina")
        self.assertEqual(concepts.source, "sina")
        self.assertEqual(industries.rows[0]["name"], "银行")
        self.assertEqual(concepts.rows[0]["name"], "人工智能")
        self.assertEqual(
            sina_requests,
            [
                ("https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php", {}),
                ("https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php", {"param": "class"}),
            ],
        )
        self.assertIn("eastmoney", " ".join(industries.warnings))

    def test_eastmoney_sector_requests_use_industry_then_concept_board_types(self) -> None:
        fixture = load_json("eastmoney_boards.json")
        requested_filters: list[str] = []

        def eastmoney_response(url: str, **kwargs: Any) -> FakeResponse:
            self.assertIn("eastmoney", url)
            board_filter = kwargs["params"]["fs"]
            requested_filters.append(board_filter)
            kind = "industry" if "t:2" in board_filter else "concept"
            return FakeResponse(fixture[kind])

        client = MarketDataClient(get=eastmoney_response, now=fixed_now)

        industries, concepts = client.fetch_sectors("2026-08-11")

        self.assertEqual(industries.source, "eastmoney")
        self.assertEqual(concepts.source, "eastmoney")
        self.assertEqual(requested_filters, ["m:90+t:2", "m:90+t:3"])

    def test_all_sector_sources_fail_without_zero_rows(self) -> None:
        def all_fail(url: str, **_: Any) -> FakeResponse:
            raise OSError(f"offline: {url}")

        client = MarketDataClient(get=all_fail, now=fixed_now)
        industries, concepts = client.fetch_sectors("2026-08-11")

        self.assertEqual(industries.rows, [])
        self.assertEqual(concepts.rows, [])
        self.assertEqual(industries.source, "none")
        self.assertTrue(industries.warnings)

    def test_akshare_invalid_numeric_warning_keeps_akshare_field_context(self) -> None:
        def sina_fails(url: str, **_: Any) -> FakeResponse:
            raise OSError(f"offline: {url}")

        client = MarketDataClient(
            get=sina_fails,
            now=fixed_now,
            akshare_fetch=lambda _: {
                "stocks": [
                    {
                        "代码": "600000",
                        "名称": "浦发银行",
                        "最新价": 10.2,
                        "昨收": 10,
                        "涨跌幅": 2,
                        "成交额": "--",
                    }
                ]
            },
        )

        result = client.fetch_a_share_breadth("2026-08-11")

        self.assertEqual(result.rows, [])
        self.assertIn("akshare:600000:amount", " ".join(result.warnings))

    def test_missing_numeric_quote_field_raises_instead_of_defaulting_to_zero(self) -> None:
        with self.assertRaises(MarketDataError):
            normalize_quote(
                code="600000",
                name="浦发银行",
                trade_date="2026-08-11",
                close="",
                previous_close=10,
                amount=100,
                source="test",
                quote_time="15:00:00",
            )

    def test_eastmoney_board_fixture_normalizes_numeric_rows(self) -> None:
        payload = load_json("eastmoney_boards.json")["industry"]

        rows = parse_eastmoney_boards(payload, "2026-08-11")

        self.assertEqual(rows[0]["name"], "银行")
        self.assertEqual(rows[0]["pct"], 1.25)
        self.assertEqual(rows[1]["direction"], "down")

    def test_snapshot_merges_watch_closes_into_full_universe_and_passes_quality(self) -> None:
        board_fixture = load_json("eastmoney_boards.json")

        def offline_get(url: str, **kwargs: Any) -> FakeResponse:
            if "qt.gtimg.cn" in url:
                fixture = (
                    "tencent_watch_quotes.txt"
                    if "sh600000" in url
                    else "tencent_quotes.txt"
                )
                return FakeResponse(text=load_text(fixture))
            if "Market_Center.getHQNodeData" in url:
                return FakeResponse(load_json("sina_stocks.json"))
            if "eastmoney" in url:
                kind = "industry" if "t:2" in kwargs["params"]["fs"] else "concept"
                return FakeResponse(board_fixture[kind])
            raise AssertionError(f"unexpected URL: {url}")

        client = MarketDataClient(
            get=offline_get,
            now=fixed_now,
            akshare_fetch=lambda _: {"indices": AKSHARE_INDICES, "stocks": []},
        )

        snapshot = client.build_snapshot("2026-08-11", WATCH_CODES)

        self.assertEqual(
            set(snapshot),
            {
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
            },
        )
        self.assertEqual(len(snapshot["stocks"]), 5)
        self.assertEqual(
            snapshot["breadth"],
            {
                "counts": {"up": 3, "down": 2, "flat": 0, "total": 5},
                "sh_amount": 300_000_000.0,
                "sz_amount": 250_000_000.0,
                "amount": 550_000_000.0,
            },
        )
        merged = next(row for row in snapshot["stocks"] if row["code"] == "600000")
        self.assertEqual(merged["close"], 10.3)
        self.assertEqual(merged["previous_close"], 10.0)
        self.assertEqual(merged["pct"], 3.0)
        self.assertEqual(merged["quote_time"], "15:00:00")
        self.assertEqual(merged["amount"], 100_000_000.0)
        self.assertEqual(merged["source"], "tencent")
        self.assertEqual(merged["fetched_at"], fixed_now().isoformat())
        self.assertEqual(merged["direction"], "up")
        self.assertEqual(
            snapshot["sources"]["indices"],
            {"source": "tencent", "fetched_at": fixed_now().isoformat()},
        )
        self.assertEqual(
            snapshot["sources"]["breadth"],
            {"source": "sina", "fetched_at": fixed_now().isoformat()},
        )
        self.assertEqual(len(snapshot["sources"]["index_comparisons"]), 6)
        normalized_rows = (
            snapshot["indices"]
            + snapshot["sectors"]["industries"]
            + snapshot["sectors"]["concepts"]
            + snapshot["stocks"]
        )
        self.assertTrue(normalized_rows)
        self.assertTrue(
            all(row["fetched_at"] == fixed_now().isoformat() for row in normalized_rows)
        )
        quality = validate_snapshot(snapshot, watch_codes=WATCH_CODES, min_stock_count=5)
        self.assertEqual(quality.status, "passed")
        self.assertEqual(quality.required_errors, ())

    def test_missing_watch_code_is_not_fabricated_in_stock_universe(self) -> None:
        board_fixture = load_json("eastmoney_boards.json")

        def offline_get(url: str, **kwargs: Any) -> FakeResponse:
            if "qt.gtimg.cn" in url:
                fixture = (
                    "tencent_watch_quotes.txt"
                    if "sh600000" in url
                    else "tencent_quotes.txt"
                )
                return FakeResponse(text=load_text(fixture))
            if "Market_Center.getHQNodeData" in url:
                return FakeResponse(load_json("sina_stocks.json"))
            if "eastmoney" in url:
                kind = "industry" if "t:2" in kwargs["params"]["fs"] else "concept"
                return FakeResponse(board_fixture[kind])
            raise AssertionError(f"unexpected URL: {url}")

        requested_watch = (*WATCH_CODES[:4], "002999")
        client = MarketDataClient(
            get=offline_get,
            now=fixed_now,
            akshare_fetch=lambda _: {"indices": [], "stocks": []},
        )

        snapshot = client.build_snapshot("2026-08-11", requested_watch)

        self.assertEqual(len(snapshot["stocks"]), 5)
        self.assertNotIn("002999", {row["code"] for row in snapshot["stocks"]})
        self.assertEqual(snapshot["sources"]["watch"]["source"], "tencent")
        self.assertNotIn("index_comparisons", snapshot["sources"])
        self.assertTrue(any("akshare" in warning for warning in snapshot["warnings"]))
        quality = validate_snapshot(snapshot, watch_codes=requested_watch, min_stock_count=5)
        self.assertEqual(quality.status, "failed_quality_gate")
        self.assertTrue(any("002999" in error for error in quality.required_errors))


if __name__ == "__main__":
    unittest.main()
