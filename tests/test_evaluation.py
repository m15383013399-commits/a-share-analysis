from __future__ import annotations

import copy
import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from market_diary.evaluation import (
    evaluate_forecast,
    evaluate_range,
    persist_evaluation,
    summarize_evaluations,
)
from market_diary.storage import atomic_write_text as storage_atomic_write_text


FIXTURES = Path(__file__).parent / "fixtures"
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


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def evaluation_pair() -> tuple[dict, dict]:
    forecast = load_fixture("forecast_valid.json")
    snapshot = load_fixture("snapshot_for_evaluation.json")
    if set(snapshot) != SNAPSHOT_KEYS:
        raise AssertionError("evaluation fixture must retain the 11-field snapshot contract")
    return forecast, snapshot


def rolling_evaluation_pair() -> tuple[dict, dict, dict]:
    current, snapshot = evaluation_pair()
    previous = copy.deepcopy(current)
    previous["trade_date"] = "2026-08-10"
    previous["next_trade_date"] = "2026-08-11"
    previous["watch_pool"][0]["code"] = "600000"
    previous["watch_pool"][0]["name"] = "浦发银行"
    current["changes"] = {
        "exited": ["600000"],
        "entered": ["601899"],
        "reason": "daily roll",
    }
    return current, snapshot, previous


def extreme_evaluation_records() -> list[dict]:
    common = {
        "forecast_date": "2026-08-11",
        "actual_date": "2026-08-12",
        "asset_type": "stock",
        "status": "evaluated",
        "lower": 0.0,
        "upper": 0.0,
        "midpoint": 0.0,
        "absolute_error": 1e308,
        "direction_hit": False,
        "range_hit": False,
    }
    return [
        {**common, "asset_code": "600000", "actual_pct": 1e308, "error": 1e308},
        {**common, "asset_code": "600001", "actual_pct": -1e308, "error": -1e308},
    ]


def evaluate_initial(forecast: dict, snapshot: dict) -> list[dict]:
    return evaluate_forecast(
        forecast,
        snapshot,
        allow_initial_without_previous=True,
    )


class EvaluationMathTest(unittest.TestCase):
    def test_error_is_actual_minus_midpoint(self) -> None:
        result = evaluate_range(actual_pct=0.34, lower=-0.40, upper=0.60)

        self.assertEqual(result["midpoint"], 0.10)
        self.assertAlmostEqual(result["error"], 0.24)
        self.assertTrue(result["direction_hit"])
        self.assertTrue(result["range_hit"])

    def test_range_boundaries_are_inclusive(self) -> None:
        self.assertTrue(
            evaluate_range(actual_pct=-0.4, lower=-0.4, upper=0.6)["range_hit"]
        )
        self.assertTrue(
            evaluate_range(actual_pct=0.6, lower=-0.4, upper=0.6)["range_hit"]
        )

    def test_direction_uses_midpoint_sign_and_strict_actual_sign(self) -> None:
        self.assertTrue(
            evaluate_range(actual_pct=0.01, lower=0.0, upper=1.0)["direction_hit"]
        )
        self.assertFalse(
            evaluate_range(actual_pct=0.0, lower=0.0, upper=1.0)["direction_hit"]
        )
        self.assertTrue(
            evaluate_range(actual_pct=-0.01, lower=-1.0, upper=0.0)["direction_hit"]
        )
        self.assertFalse(
            evaluate_range(actual_pct=0.0, lower=-1.0, upper=0.0)["direction_hit"]
        )

    def test_zero_midpoint_uses_inclusive_flat_threshold(self) -> None:
        self.assertTrue(
            evaluate_range(actual_pct=0.30, lower=-0.5, upper=0.5)["direction_hit"]
        )
        self.assertTrue(
            evaluate_range(actual_pct=-0.30, lower=-0.5, upper=0.5)["direction_hit"]
        )
        self.assertFalse(
            evaluate_range(actual_pct=0.31, lower=-0.5, upper=0.5)["direction_hit"]
        )

    def test_invalid_or_nonfinite_numbers_fail_instead_of_becoming_zero(self) -> None:
        cases = (
            {"actual_pct": None, "lower": -1, "upper": 1},
            {"actual_pct": True, "lower": -1, "upper": 1},
            {"actual_pct": "bad", "lower": -1, "upper": 1},
            {"actual_pct": math.nan, "lower": -1, "upper": 1},
            {"actual_pct": 0, "lower": math.inf, "upper": 1},
            {"actual_pct": 0, "lower": 2, "upper": 1},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                evaluate_range(**values)

        with self.assertRaises(ValueError):
            evaluate_range(actual_pct=0, lower=-1, upper=1, flat_threshold=-0.01)


class ForecastEvaluationTest(unittest.TestCase):
    def test_empty_changes_without_previous_is_rejected_by_default(self) -> None:
        forecast, snapshot = evaluation_pair()

        with self.assertRaisesRegex(
            ValueError, "previous_forecast.*allow_initial_without_previous"
        ):
            evaluate_forecast(forecast, snapshot)

    def test_true_initial_requires_explicit_initial_authorization(self) -> None:
        forecast, snapshot = evaluation_pair()

        rows = evaluate_forecast(
            forecast,
            snapshot,
            allow_initial_without_previous=True,
        )

        self.assertEqual(len(rows), 12)

    def test_initial_authorization_cannot_admit_nonempty_changes(self) -> None:
        forecast, snapshot, _ = rolling_evaluation_pair()

        with self.assertRaisesRegex(ValueError, "initial watch pool.*changes.*empty"):
            evaluate_forecast(
                forecast,
                snapshot,
                allow_initial_without_previous=True,
            )

    def test_previous_and_initial_authorization_are_mutually_exclusive(self) -> None:
        forecast, snapshot, previous = rolling_evaluation_pair()

        with self.assertRaisesRegex(
            ValueError, "previous_forecast.*allow_initial_without_previous"
        ):
            evaluate_forecast(
                forecast,
                snapshot,
                previous_forecast=previous,
                allow_initial_without_previous=True,
            )

    def test_initial_authorization_requires_an_actual_boolean(self) -> None:
        forecast, snapshot = evaluation_pair()

        with self.assertRaisesRegex(
            ValueError, "allow_initial_without_previous.*boolean"
        ):
            evaluate_forecast(
                forecast,
                snapshot,
                allow_initial_without_previous=1,  # type: ignore[arg-type]
            )

    def test_rolling_forecast_without_real_previous_is_rejected(self) -> None:
        forecast, snapshot, _ = rolling_evaluation_pair()

        with self.assertRaisesRegex(ValueError, "previous_forecast.*required"):
            evaluate_forecast(forecast, snapshot)

    def test_rolling_forecast_accepts_real_previous_document(self) -> None:
        forecast, snapshot, previous = rolling_evaluation_pair()

        rows = evaluate_forecast(
            forecast, snapshot, previous_forecast=previous
        )

        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0]["forecast_date"], "2026-08-11")

    def test_rolling_forecast_rejects_previous_with_wrong_pool(self) -> None:
        forecast, snapshot, previous = rolling_evaluation_pair()
        previous["watch_pool"][0]["code"] = "601899"

        with self.assertRaisesRegex(ValueError, "changes.*pool difference"):
            evaluate_forecast(
                forecast, snapshot, previous_forecast=previous
            )

    def test_option_a_reads_stock_actuals_from_stocks_and_recomputes_pct(self) -> None:
        forecast, snapshot = evaluation_pair()

        rows = evaluate_initial(forecast, snapshot)

        stock = next(row for row in rows if row["asset_code"] == "601899")
        self.assertEqual(len(rows), 12)
        self.assertEqual(stock["asset_type"], "stock")
        self.assertEqual(stock["actual_pct"], 5.0)
        self.assertEqual(stock["error"], 4.5)
        self.assertNotIn("watch_pool_quotes", snapshot)
        self.assertEqual(set(snapshot), SNAPSHOT_KEYS)

    def test_indices_and_numeric_sectors_use_their_proxy_rows(self) -> None:
        forecast, snapshot = evaluation_pair()
        forecast["sectors"][0].update({"lower": 1.0, "upper": 2.0})

        rows = evaluate_initial(forecast, snapshot)

        index = next(row for row in rows if row["asset_code"] == "399006")
        sector = next(row for row in rows if row["asset_code"] == "BK0475")
        self.assertEqual(index["actual_pct"], 0.34)
        self.assertEqual(sector["actual_pct"], 1.5)
        self.assertTrue(sector["range_hit"])

    def test_suspended_stock_is_not_evaluable_or_in_denominator(self) -> None:
        forecast, snapshot = evaluation_pair()
        snapshot["stocks"][0]["status"] = "suspended"
        snapshot["stocks"][0]["close"] = None

        rows = evaluate_initial(forecast, snapshot)

        stock = next(row for row in rows if row["asset_code"] == "601899")
        self.assertEqual(stock["status"], "not_evaluable")
        self.assertEqual(stock["reason"], "suspended")
        self.assertNotIn("absolute_error", stock)
        self.assertEqual(summarize_evaluations(rows)["stocks"]["sample_count"], 4)

    def test_stock_without_close_is_not_evaluable(self) -> None:
        forecast, snapshot = evaluation_pair()
        snapshot["stocks"][1]["close"] = None

        stock = next(
            row
            for row in evaluate_initial(forecast, snapshot)
            if row["asset_code"] == "600760"
        )

        self.assertEqual(stock["status"], "not_evaluable")
        self.assertEqual(stock["reason"], "close_missing")
        self.assertNotIn("actual_pct", stock)

    def test_qualitative_sectors_do_not_enter_mae(self) -> None:
        forecast, snapshot = evaluation_pair()

        rows = evaluate_initial(forecast, snapshot)

        sector_rows = [row for row in rows if row["asset_type"] == "sector"]
        self.assertTrue(all(row["status"] == "not_evaluable" for row in sector_rows))
        self.assertTrue(all("absolute_error" not in row for row in sector_rows))
        self.assertEqual(summarize_evaluations(rows)["sectors"]["sample_count"], 0)

    def test_missing_actual_row_is_explicitly_not_evaluable(self) -> None:
        forecast, snapshot = evaluation_pair()
        snapshot["stocks"] = [
            row for row in snapshot["stocks"] if row["code"] != "601899"
        ]

        stock = next(
            row
            for row in evaluate_initial(forecast, snapshot)
            if row["asset_code"] == "601899"
        )

        self.assertEqual(stock["status"], "not_evaluable")
        self.assertEqual(stock["reason"], "actual_missing")
        self.assertNotIn("actual_pct", stock)

    def test_forecast_and_snapshot_dates_must_align(self) -> None:
        forecast, snapshot = evaluation_pair()
        snapshot["trade_date"] = "2026-08-13"

        with self.assertRaisesRegex(ValueError, "next_trade_date.*snapshot.trade_date"):
            evaluate_initial(forecast, snapshot)

    def test_actual_row_dates_must_match_snapshot_date(self) -> None:
        forecast, snapshot = evaluation_pair()
        snapshot["indices"][0]["trade_date"] = "2026-08-11"

        with self.assertRaisesRegex(ValueError, "trade_date.*2026-08-12"):
            evaluate_initial(forecast, snapshot)

    def test_malformed_actuals_and_stock_basis_fail_explicitly(self) -> None:
        forecast, snapshot = evaluation_pair()
        snapshot["indices"][0]["pct"] = "bad"
        with self.assertRaisesRegex(ValueError, "pct.*finite number"):
            evaluate_initial(forecast, snapshot)

        forecast, snapshot = evaluation_pair()
        snapshot["stocks"][0]["previous_close"] = 0
        with self.assertRaisesRegex(ValueError, "previous_close.*positive"):
            evaluate_initial(forecast, snapshot)

    def test_partial_sector_range_fails_explicitly(self) -> None:
        forecast, snapshot = evaluation_pair()
        forecast["sectors"][0]["lower"] = -1.0

        with self.assertRaisesRegex(ValueError, "sector.*lower.*upper"):
            evaluate_initial(forecast, snapshot)

    def test_duplicate_actual_codes_fail_instead_of_selecting_arbitrarily(self) -> None:
        forecast, snapshot = evaluation_pair()
        snapshot["stocks"].append(copy.deepcopy(snapshot["stocks"][0]))

        with self.assertRaisesRegex(ValueError, "duplicate.*601899"):
            evaluate_initial(forecast, snapshot)


class EvaluationSummaryTest(unittest.TestCase):
    def test_mae_mean_does_not_overflow_for_finite_extreme_errors(self) -> None:
        summary = summarize_evaluations(extreme_evaluation_records())

        mae = summary["stocks"]["mean_absolute_error"]
        self.assertTrue(math.isfinite(mae))
        self.assertEqual(mae, 1e308)
        self.assertEqual(summary["overall"]["mean_absolute_error"], 1e308)

    def test_summary_reports_counts_hit_rates_and_mae_by_asset_type(self) -> None:
        forecast, snapshot = evaluation_pair()
        forecast["sectors"][0].update({"lower": 1.0, "upper": 2.0})
        rows = evaluate_initial(forecast, snapshot)

        summary = summarize_evaluations(rows)

        self.assertEqual(summary["indices"]["sample_count"], 4)
        self.assertEqual(summary["sectors"]["sample_count"], 1)
        self.assertEqual(summary["stocks"]["sample_count"], 5)
        self.assertEqual(summary["overall"]["sample_count"], 10)
        self.assertAlmostEqual(summary["sectors"]["mean_absolute_error"], 0.0)

    def test_empty_denominator_yields_none_rates_and_mae(self) -> None:
        summary = summarize_evaluations(
            [
                {
                    "forecast_date": "2026-08-11",
                    "actual_date": "2026-08-12",
                    "asset_type": "stock",
                    "asset_code": "600000",
                    "status": "not_evaluable",
                }
            ]
        )

        self.assertEqual(
            summary["stocks"],
            {
                "sample_count": 0,
                "direction_hit_rate": None,
                "range_hit_rate": None,
                "mean_absolute_error": None,
            },
        )


class EvaluationPersistenceTest(unittest.TestCase):
    def test_extreme_finite_summary_persists_as_strict_json(self) -> None:
        with TemporaryDirectory() as tmp:
            detail_path = Path(tmp) / "e.jsonl"
            summary_path = Path(tmp) / "s.json"

            summary = persist_evaluation(
                extreme_evaluation_records(),
                detail_path=detail_path,
                summary_path=summary_path,
            )
            summary_text = summary_path.read_text("utf-8")

            self.assertTrue(
                math.isfinite(summary["overall"]["mean_absolute_error"])
            )
            self.assertNotIn("Infinity", summary_text)
            self.assertNotIn("NaN", summary_text)
            self.assertEqual(json.loads(summary_text), summary)

    def test_repeat_persist_replaces_same_daily_assets(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = evaluate_initial(*evaluation_pair())
            detail_path = Path(tmp) / "e.jsonl"
            summary_path = Path(tmp) / "s.json"

            persist_evaluation(rows, detail_path=detail_path, summary_path=summary_path)
            persist_evaluation(rows, detail_path=detail_path, summary_path=summary_path)

            persisted = detail_path.read_text("utf-8").splitlines()
            self.assertEqual(len(persisted), len(rows))

    def test_cumulative_summary_and_exited_stock_history_are_preserved(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = evaluate_initial(*evaluation_pair())
            detail_path = Path(tmp) / "e.jsonl"
            summary_path = Path(tmp) / "s.json"
            persist_evaluation(rows, detail_path=detail_path, summary_path=summary_path)

            next_rows = copy.deepcopy(rows)
            for row in next_rows:
                row["forecast_date"] = "2026-08-12"
                row["actual_date"] = "2026-08-13"
            next_rows = [row for row in next_rows if row["asset_code"] != "601899"]

            summary = persist_evaluation(
                next_rows, detail_path=detail_path, summary_path=summary_path
            )
            persisted = [
                json.loads(line) for line in detail_path.read_text("utf-8").splitlines()
            ]

            self.assertTrue(
                any(
                    row["forecast_date"] == "2026-08-11"
                    and row["asset_code"] == "601899"
                    for row in persisted
                )
            )
            self.assertEqual(summary["stocks"]["sample_count"], 9)
            self.assertEqual(
                json.loads(summary_path.read_text("utf-8")), summary
            )

    def test_same_four_field_key_is_replaced(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = evaluate_initial(*evaluation_pair())
            detail_path = Path(tmp) / "e.jsonl"
            summary_path = Path(tmp) / "s.json"
            persist_evaluation(rows, detail_path=detail_path, summary_path=summary_path)
            replacement = copy.deepcopy(rows[0])
            replacement["actual_pct"] = 9.99
            replacement["absolute_error"] = 9.99

            persist_evaluation(
                [replacement], detail_path=detail_path, summary_path=summary_path
            )
            persisted = [
                json.loads(line) for line in detail_path.read_text("utf-8").splitlines()
            ]
            matches = [
                row
                for row in persisted
                if tuple(row[field] for field in (
                    "forecast_date", "actual_date", "asset_type", "asset_code"
                ))
                == tuple(replacement[field] for field in (
                    "forecast_date", "actual_date", "asset_type", "asset_code"
                ))
            ]

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["actual_pct"], 9.99)

    def test_detail_write_failure_leaves_both_existing_files_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = evaluate_initial(*evaluation_pair())
            detail_path = Path(tmp) / "e.jsonl"
            summary_path = Path(tmp) / "s.json"
            persist_evaluation(rows, detail_path=detail_path, summary_path=summary_path)
            old_detail = detail_path.read_bytes()
            old_summary = summary_path.read_bytes()

            calls = 0

            def fail_before_replace(path: str | Path, text: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("detail failed")
                storage_atomic_write_text(path, text)

            with patch(
                "market_diary.evaluation.atomic_write_text",
                side_effect=fail_before_replace,
            ), self.assertRaisesRegex(OSError, "detail failed"):
                persist_evaluation(
                    rows, detail_path=detail_path, summary_path=summary_path
                )

            self.assertEqual(detail_path.read_bytes(), old_detail)
            self.assertEqual(summary_path.read_bytes(), old_summary)

    def test_post_replace_detail_failure_rolls_back_both_files(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = evaluate_initial(*evaluation_pair())
            detail_path = Path(tmp) / "e.jsonl"
            summary_path = Path(tmp) / "s.json"
            persist_evaluation(rows, detail_path=detail_path, summary_path=summary_path)
            old_detail = detail_path.read_bytes()
            old_summary = summary_path.read_bytes()
            replacement = copy.deepcopy(rows[0])
            replacement["actual_pct"] = 9.99
            replacement["absolute_error"] = 9.99
            calls = 0

            def replace_then_fail(path: str | Path, text: str) -> None:
                nonlocal calls
                calls += 1
                storage_atomic_write_text(path, text)
                if calls == 1:
                    raise OSError("detail failed after replace")

            with patch(
                "market_diary.evaluation.atomic_write_text",
                side_effect=replace_then_fail,
            ), self.assertRaisesRegex(OSError, "detail failed after replace"):
                persist_evaluation(
                    [replacement], detail_path=detail_path, summary_path=summary_path
                )

            self.assertEqual(detail_path.read_bytes(), old_detail)
            self.assertEqual(summary_path.read_bytes(), old_summary)

    def test_one_rollback_failure_does_not_prevent_other_file_restore(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = evaluate_initial(*evaluation_pair())
            detail_path = Path(tmp) / "e.jsonl"
            summary_path = Path(tmp) / "s.json"
            persist_evaluation(rows, detail_path=detail_path, summary_path=summary_path)
            old_summary = summary_path.read_bytes()
            replacement = copy.deepcopy(rows[0])
            replacement["actual_pct"] = 9.99
            replacement["absolute_error"] = 9.99
            calls = 0

            def fail_write_then_detail_restore(path: str | Path, text: str) -> None:
                nonlocal calls
                calls += 1
                target = Path(path)
                if calls == 1:
                    storage_atomic_write_text(target, text)
                    storage_atomic_write_text(summary_path, "corrupt summary\n")
                    raise OSError("detail failed after replace")
                if target == detail_path:
                    raise OSError("detail rollback failed")
                storage_atomic_write_text(target, text)

            with patch(
                "market_diary.evaluation.atomic_write_text",
                side_effect=fail_write_then_detail_restore,
            ), self.assertRaisesRegex(
                RuntimeError, "rollback failed.*detail_path.*detail rollback failed"
            ):
                persist_evaluation(
                    [replacement], detail_path=detail_path, summary_path=summary_path
                )

            self.assertEqual(summary_path.read_bytes(), old_summary)

    def test_summary_write_failure_rolls_back_detail(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = evaluate_initial(*evaluation_pair())
            detail_path = Path(tmp) / "e.jsonl"
            summary_path = Path(tmp) / "s.json"
            persist_evaluation(rows, detail_path=detail_path, summary_path=summary_path)
            old_detail = detail_path.read_bytes()
            old_summary = summary_path.read_bytes()
            replacement = copy.deepcopy(rows[0])
            replacement["actual_pct"] = 9.99
            replacement["absolute_error"] = 9.99

            with patch(
                "market_diary.evaluation._atomic_write_summary",
                side_effect=OSError("summary failed"),
            ), self.assertRaisesRegex(OSError, "summary failed"):
                persist_evaluation(
                    [replacement], detail_path=detail_path, summary_path=summary_path
                )

            self.assertEqual(detail_path.read_bytes(), old_detail)
            self.assertEqual(summary_path.read_bytes(), old_summary)


if __name__ == "__main__":
    unittest.main()
