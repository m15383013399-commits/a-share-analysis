from __future__ import annotations

import math
import unittest
from typing import Any

from market_diary.analyze import IncompleteMarketDataError, analyze_payload
from market_diary.quality import validate_snapshot
from market_diary.sample import build_sample_payload


TRADE_DATE = "2026-08-11"
WATCH_CODES = ("600000", "600001", "000001", "000002", "300001")
INDEX_CODES = ("000001", "399001", "399006", "000688", "000905", "000300")


def valid_stock_codes(count: int) -> list[str]:
    prefixes = ("600", "601", "603", "605", "688", "000", "001", "002", "003", "300", "301")
    codes = list(WATCH_CODES)
    seen = set(codes)
    for prefix in prefixes:
        for suffix in range(1000):
            code = f"{prefix}{suffix:03d}"
            if code not in seen:
                codes.append(code)
                seen.add(code)
                if len(codes) == count:
                    return codes
    return codes[:count]


def complete_snapshot(stock_count: int = 5000) -> dict[str, Any]:
    stocks: list[dict[str, Any]] = []
    for index, code in enumerate(valid_stock_codes(stock_count)):
        close = 10.0 + index / 100
        stocks.append(
            {
                "code": code,
                "name": f"股票{index}",
                "trade_date": TRADE_DATE,
                "quote_time": "15:00:00",
                "close": close,
                "previous_close": close / 1.001,
                "pct": 0.1,
                "amount": 1_000_000.0,
                "direction": "up",
            }
        )

    indices = [
        {
            "code": code,
            "name": f"指数{position}",
            "trade_date": TRADE_DATE,
            "quote_time": "15:00:00",
            "close": 3000.0 + position * 100,
            "previous_close": (3000.0 + position * 100) / (1 + (0.30 + position / 100) / 100),
            "pct": 0.30 + position / 100,
            "amount": 100_000_000.0,
            "direction": "up",
        }
        for position, code in enumerate(INDEX_CODES)
    ]
    comparisons = [
        {
            "code": row["code"],
            "primary": {
                "source": "tencent",
                "pct": row["pct"],
                "close": row["close"],
            },
            "secondary": {
                "source": "akshare",
                "pct": row["pct"] + 0.01,
                "close": row["close"] * 1.0001,
            },
        }
        for row in indices
    ]

    snapshot = {
        "schema_version": "1.0",
        "trade_date": TRADE_DATE,
        "generated_at": f"{TRADE_DATE}T15:05:00+08:00",
        "market_status": "ready",
        "quality_status": "pending",
        "sources": {
            "indices": {"source": "tencent"},
            "index_comparisons": comparisons,
        },
        "indices": indices,
        "breadth": {
            "counts": {"up": stock_count, "down": 0, "flat": 0, "total": stock_count},
            "sh_amount": 600_000_000_000.0,
            "sz_amount": 700_000_000_000.0,
        },
        "sectors": {
            "industries": [{"name": f"行业{index}"} for index in range(30)],
            "concepts": [{"name": f"概念{index}"} for index in range(50)],
        },
        "stocks": stocks,
        "warnings": [],
    }
    if set(snapshot) != {
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
    }:
        raise AssertionError("quality fixture must retain the exact eleven-field snapshot contract")
    return snapshot


class QualityGateTest(unittest.TestCase):
    def test_quality_tolerances_must_be_finite_nonnegative_real_numbers(self) -> None:
        invalid_values = (math.nan, math.inf, -math.inf, -1, True, False, "0.05", "bad")
        for field in ("index_pct_tolerance", "index_level_tolerance_pct"):
            for invalid in invalid_values:
                with self.subTest(field=field, invalid=invalid), self.assertRaisesRegex(
                    ValueError, field
                ):
                    validate_snapshot(
                        complete_snapshot(),
                        watch_codes=WATCH_CODES,
                        **{field: invalid},
                    )

        result = validate_snapshot(
            complete_snapshot(),
            watch_codes=WATCH_CODES,
            index_pct_tolerance=0.05,
            index_level_tolerance_pct=0.05,
        )
        self.assertEqual(result.status, "passed")

    def test_complete_eleven_field_snapshot_passes(self) -> None:
        result = validate_snapshot(complete_snapshot(), watch_codes=WATCH_CODES)

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.required_errors, ())

    def test_each_missing_required_index_fails(self) -> None:
        for missing_code in INDEX_CODES:
            with self.subTest(missing_code=missing_code):
                snapshot = complete_snapshot()
                snapshot["indices"] = [
                    row for row in snapshot["indices"] if row["code"] != missing_code
                ]

                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

                self.assertIn(f"missing required index: {missing_code}", errors)

    def test_small_and_duplicate_stock_universes_fail(self) -> None:
        small = validate_snapshot(complete_snapshot(stock_count=4999), watch_codes=WATCH_CODES)
        duplicate_snapshot = complete_snapshot()
        duplicate_snapshot["stocks"][-1]["code"] = duplicate_snapshot["stocks"][0]["code"]
        duplicate = validate_snapshot(duplicate_snapshot, watch_codes=WATCH_CODES)

        self.assertEqual(small.status, "failed_quality_gate")
        self.assertTrue(any("stock sample count" in error for error in small.required_errors))
        self.assertTrue(
            any("duplicate stock code" in error for error in duplicate.required_errors)
        )

    def test_empty_or_noncanonical_stock_codes_fail(self) -> None:
        for invalid_code in ("", "ABC123", "60000", "600000.0", "６０００００"):
            with self.subTest(invalid_code=invalid_code):
                snapshot = complete_snapshot()
                snapshot["stocks"][-1]["code"] = invalid_code

                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

                self.assertTrue(any("invalid stock code" in error for error in errors))

    def test_non_shanghai_shenzhen_six_digit_codes_fail(self) -> None:
        for invalid_code in ("430001", "830001", "399001", "999999"):
            with self.subTest(invalid_code=invalid_code):
                snapshot = complete_snapshot()
                snapshot["stocks"][-1]["code"] = invalid_code

                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

                self.assertTrue(any("invalid stock code" in error for error in errors))

    def test_each_stock_row_requires_a_self_consistent_price_derived_quote(self) -> None:
        invalid_cases = (
            ("close", 0),
            ("previous_close", None),
            ("pct", math.nan),
            ("amount", -1),
            ("direction", "sideways"),
            ("pct", 99),
            ("direction", "down"),
        )
        for field, value in invalid_cases:
            with self.subTest(field=field, value=value):
                snapshot = complete_snapshot()
                snapshot["stocks"][-1][field] = value

                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

                self.assertTrue(any("invalid stock quote" in error for error in errors))

    def test_index_and_stock_trade_date_mismatches_fail(self) -> None:
        snapshot = complete_snapshot()
        snapshot["indices"][0]["trade_date"] = "2026-08-10"
        snapshot["stocks"][-1]["trade_date"] = "2026-08-10"

        errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

        self.assertTrue(any("index 000001 trade date" in error for error in errors))
        self.assertTrue(any("stock" in error and "trade date" in error for error in errors))

    def test_missing_target_trade_date_fails(self) -> None:
        snapshot = complete_snapshot()
        snapshot["trade_date"] = ""
        for row in snapshot["indices"] + snapshot["stocks"]:
            row["trade_date"] = ""

        errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

        self.assertTrue(any("target trade date" in error for error in errors))

    def test_noncanonical_iso_trade_dates_fail_even_when_all_rows_match(self) -> None:
        for noncanonical in ("20260811", "2026-W33-2"):
            with self.subTest(trade_date=noncanonical):
                snapshot = complete_snapshot()
                snapshot["trade_date"] = noncanonical
                for row in snapshot["indices"] + snapshot["stocks"]:
                    row["trade_date"] = noncanonical

                errors = validate_snapshot(
                    snapshot, watch_codes=WATCH_CODES
                ).required_errors

                self.assertTrue(any("target trade date" in error for error in errors))

    def test_zero_exchange_amount_and_breadth_mismatch_fail(self) -> None:
        snapshot = complete_snapshot()
        snapshot["breadth"]["sh_amount"] = 0
        snapshot["breadth"]["counts"]["up"] -= 1

        errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

        self.assertTrue(any("sh_amount" in error for error in errors))
        self.assertTrue(any("breadth total" in error for error in errors))

    def test_breadth_categories_must_match_price_derived_stock_directions(self) -> None:
        snapshot = complete_snapshot()
        snapshot["breadth"]["counts"].update({"up": 4999, "down": 1})

        errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

        self.assertTrue(any("breadth counts do not match" in error for error in errors))

    def test_indices_must_be_the_exact_unique_six_code_set(self) -> None:
        duplicate = complete_snapshot()
        duplicate["indices"][-1] = dict(duplicate["indices"][0])
        extra = complete_snapshot()
        extra["indices"].append({**extra["indices"][0], "code": "000016"})

        duplicate_errors = validate_snapshot(
            duplicate, watch_codes=WATCH_CODES
        ).required_errors
        extra_errors = validate_snapshot(extra, watch_codes=WATCH_CODES).required_errors

        self.assertTrue(any("duplicate index code" in error for error in duplicate_errors))
        self.assertTrue(any("unexpected index" in error for error in extra_errors))

    def test_non_object_index_or_stock_rows_fail_instead_of_being_discarded(self) -> None:
        bad_index = complete_snapshot()
        bad_index["indices"].append(None)
        bad_stock = complete_snapshot()
        bad_stock["stocks"].append(None)

        index_errors = validate_snapshot(
            bad_index, watch_codes=WATCH_CODES
        ).required_errors
        stock_errors = validate_snapshot(
            bad_stock, watch_codes=WATCH_CODES
        ).required_errors

        self.assertTrue(any("index rows must be objects" in error for error in index_errors))
        self.assertTrue(any("stock rows must be objects" in error for error in stock_errors))

    def test_required_index_configuration_cannot_weaken_the_fixed_six(self) -> None:
        reordered = tuple(reversed(INDEX_CODES))

        result = validate_snapshot(
            complete_snapshot(),
            watch_codes=WATCH_CODES,
            required_index_codes=reordered,
        )

        self.assertEqual(result.status, "passed")
        invalid_values = (
            None,
            "000001,399001",
            (*INDEX_CODES[:-1], "000016"),
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_snapshot(
                    complete_snapshot(),
                    watch_codes=WATCH_CODES,
                    required_index_codes=invalid,
                )

    def test_direct_breadth_counts_without_redundant_total_pass(self) -> None:
        snapshot = complete_snapshot()
        snapshot["breadth"] = {
            "up": 5000,
            "down": 0,
            "flat": 0,
            "sh_amount": 600_000_000_000.0,
            "sz_amount": 700_000_000_000.0,
        }

        result = validate_snapshot(snapshot, watch_codes=WATCH_CODES)

        self.assertEqual(result.status, "passed")

    def test_each_breadth_count_must_be_a_nonnegative_integer(self) -> None:
        invalid_cases = (
            (-1, 5001, 0),
            (True, 4999, 0),
            (4999.5, 0.5, 0),
            ("not-a-number", 5001, 0),
        )
        for up, down, flat in invalid_cases:
            with self.subTest(up=up, down=down, flat=flat):
                snapshot = complete_snapshot()
                snapshot["breadth"]["counts"].update(
                    {"up": up, "down": down, "flat": flat}
                )

                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

                self.assertTrue(
                    any(
                        "breadth counts must be non-negative integers" in error
                        for error in errors
                    )
                )

    def test_watch_pool_must_be_exactly_five_official_target_date_closes(self) -> None:
        snapshot = complete_snapshot()
        snapshot["stocks"][0]["quote_time"] = "14:59:59"
        snapshot["stocks"][1]["trade_date"] = "2026-08-10"
        snapshot["stocks"][2]["close"] = None

        errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors
        wrong_count = validate_snapshot(snapshot, watch_codes=WATCH_CODES[:4]).required_errors

        self.assertTrue(any("official closes" in error for error in errors))
        self.assertTrue(any("exactly five" in error for error in wrong_count))

    def test_watch_pool_rejects_non_strict_or_invalid_close_times(self) -> None:
        for quote_time in ("9:00:00", "garbage", "24:00:00", "15:00", None):
            with self.subTest(quote_time=quote_time):
                snapshot = complete_snapshot()
                snapshot["stocks"][0]["quote_time"] = quote_time

                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

                self.assertTrue(any("official closes" in error for error in errors))

    def test_optional_sector_shortage_warns_but_passes(self) -> None:
        snapshot = complete_snapshot()
        snapshot["sectors"] = {"industries": [], "concepts": []}

        result = validate_snapshot(snapshot, watch_codes=WATCH_CODES)

        self.assertEqual(result.status, "passed")
        self.assertEqual(len(result.optional_warnings), 2)

    def test_cross_source_pct_or_level_difference_over_tolerance_fails(self) -> None:
        for field in ("pct", "close"):
            with self.subTest(field=field):
                snapshot = complete_snapshot()
                comparison = snapshot["sources"]["index_comparisons"][0]
                if field == "pct":
                    comparison["secondary"]["pct"] = comparison["primary"]["pct"] + 0.051
                else:
                    comparison["secondary"]["close"] = comparison["primary"]["close"] * 1.00051

                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

                self.assertTrue(any("cross-source difference" in error for error in errors))

    def test_malformed_cross_source_comparison_fails_closed(self) -> None:
        snapshot = complete_snapshot()
        snapshot["sources"]["index_comparisons"][0]["secondary"]["close"] = None

        errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

        self.assertTrue(any("cross-source comparison invalid" in error for error in errors))

    def test_partial_or_duplicate_cross_source_comparison_set_fails_closed(self) -> None:
        partial = complete_snapshot()
        partial["sources"]["index_comparisons"].pop()
        duplicate = complete_snapshot()
        duplicate["sources"]["index_comparisons"][-1] = dict(
            duplicate["sources"]["index_comparisons"][0]
        )

        partial_errors = validate_snapshot(
            partial, watch_codes=WATCH_CODES
        ).required_errors
        duplicate_errors = validate_snapshot(
            duplicate, watch_codes=WATCH_CODES
        ).required_errors

        self.assertTrue(any("comparison set" in error for error in partial_errors))
        self.assertTrue(any("comparison set" in error for error in duplicate_errors))

    def test_present_malformed_comparison_container_or_rows_fail_closed(self) -> None:
        invalid_values = (
            {"code": "000001"},
            [*complete_snapshot()["sources"]["index_comparisons"], None],
        )
        for invalid in invalid_values:
            with self.subTest(invalid_type=type(invalid).__name__):
                snapshot = complete_snapshot()
                snapshot["sources"]["index_comparisons"] = invalid

                errors = validate_snapshot(
                    snapshot, watch_codes=WATCH_CODES
                ).required_errors

                self.assertTrue(any("comparison field invalid" in error for error in errors))

    def test_comparison_provenance_and_primary_values_must_be_authentic(self) -> None:
        mutations = (
            lambda snapshot: snapshot["sources"]["index_comparisons"][0]["primary"].pop("source"),
            lambda snapshot: snapshot["sources"]["index_comparisons"][0]["secondary"].update(
                {"source": "tencent"}
            ),
            lambda snapshot: snapshot["sources"]["index_comparisons"][0]["secondary"].update(
                {"source": "unknown"}
            ),
            lambda snapshot: snapshot["sources"]["index_comparisons"][0]["secondary"].update(
                {"source": []}
            ),
            lambda snapshot: snapshot["sources"]["index_comparisons"][1]["primary"].update(
                {"source": "akshare"}
            ),
            lambda snapshot: snapshot["sources"]["index_comparisons"][0]["primary"].update(
                {"close": snapshot["indices"][0]["close"] + 1}
            ),
            lambda snapshot: snapshot["sources"]["index_comparisons"][0]["primary"].update(
                {"pct": snapshot["indices"][0]["pct"] + 0.01}
            ),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate.__code__.co_firstlineno):
                snapshot = complete_snapshot()
                mutate(snapshot)

                errors = validate_snapshot(
                    snapshot, watch_codes=WATCH_CODES
                ).required_errors

                self.assertTrue(any("comparison authenticity" in error for error in errors))

    def test_comparison_primary_source_must_match_selected_index_source(self) -> None:
        snapshot = complete_snapshot()
        snapshot["sources"]["indices"]["source"] = "akshare"

        errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

        self.assertTrue(any("comparison authenticity" in error for error in errors))

    def test_unavailable_secondary_source_warns_without_fabricating_comparison(self) -> None:
        snapshot = complete_snapshot()
        snapshot["sources"].pop("index_comparisons")

        result = validate_snapshot(snapshot, watch_codes=WATCH_CODES)

        self.assertEqual(result.status, "passed")
        self.assertTrue(any("cross-source comparison unavailable" in warning for warning in result.optional_warnings))

    def test_nonfinite_cross_source_values_fail_closed(self) -> None:
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                snapshot = complete_snapshot()
                comparison = snapshot["sources"]["index_comparisons"][0]
                comparison["primary"]["pct"] = value
                comparison["secondary"]["pct"] = value

                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors

                self.assertTrue(any("cross-source comparison invalid" in error for error in errors))

    def test_nonfinite_amounts_and_closes_fail_closed(self) -> None:
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(field="sh_amount", value=value):
                snapshot = complete_snapshot()
                snapshot["breadth"]["sh_amount"] = value
                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors
                self.assertTrue(any("sh_amount" in error for error in errors))
            with self.subTest(field="index_close", value=value):
                snapshot = complete_snapshot()
                snapshot["indices"][0]["close"] = value
                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors
                self.assertTrue(any("index 000001 official close" in error for error in errors))
            with self.subTest(field="watch_close", value=value):
                snapshot = complete_snapshot()
                snapshot["stocks"][0]["close"] = value
                errors = validate_snapshot(snapshot, watch_codes=WATCH_CODES).required_errors
                self.assertTrue(any("official closes" in error for error in errors))


class AnalysisQualityGateTest(unittest.TestCase):
    def test_analyze_rejects_empty_breadth_instead_of_reporting_freezing(self) -> None:
        payload = build_sample_payload(TRADE_DATE)
        payload["stocks"] = []
        payload["quality_status"] = {
            "status": "failed_quality_gate",
            "required_errors": ["stock sample count: 0"],
        }

        with self.assertRaises(IncompleteMarketDataError):
            analyze_payload(payload)

    def test_analyze_accepts_explicit_sample_only_without_claiming_quality_passed(self) -> None:
        payload = build_sample_payload(TRADE_DATE)

        result = analyze_payload(payload)

        self.assertEqual(payload["quality_status"], {"status": "sample_only"})
        self.assertEqual(result.source, "sample")
        self.assertTrue(any("样例数据" in risk for risk in result.risks))

    def test_analyze_requires_passed_quality_for_non_sample_payload(self) -> None:
        payload = build_sample_payload(TRADE_DATE)
        payload["source"] = "live"
        payload["quality_status"] = {"status": "failed_quality_gate"}

        with self.assertRaises(IncompleteMarketDataError):
            analyze_payload(payload)

        payload["quality_status"] = {"status": "passed"}
        self.assertEqual(analyze_payload(payload).source, "live")


if __name__ == "__main__":
    unittest.main()
