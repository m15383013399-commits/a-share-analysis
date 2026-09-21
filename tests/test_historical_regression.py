from __future__ import annotations

import json
import unittest
from pathlib import Path

from market_diary.evaluation import evaluate_forecast, summarize_evaluations
from market_diary.forecast_ledger import validate_forecast


ROOT = Path(__file__).resolve().parents[1]
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


def reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def load_json_file(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"), parse_constant=reject_nonfinite)


@unittest.skipUnless((ROOT / "data/forecasts/2026-08-10.json").exists(), "Private historical ledger is not distributed; synthetic regression tests run separately")
class HistoricalRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.forecast_0810 = load_json_file(
            ROOT / "data/forecasts/2026-08-10.json"
        )
        self.snapshot_0811 = load_json_file(
            ROOT / "data/snapshots/2026-08-11.json"
        )
        self.forecast_0811 = load_json_file(
            ROOT / "data/forecasts/2026-08-11.json"
        )

    def test_august_10_forecast_against_august_11_close(self) -> None:
        records = evaluate_forecast(
            self.forecast_0810,
            self.snapshot_0811,
            allow_initial_without_previous=True,
        )
        summary = summarize_evaluations(records)

        self.assertAlmostEqual(
            summary["indices"]["mean_absolute_error"], 0.9925, places=4
        )
        self.assertAlmostEqual(
            summary["stocks"]["mean_absolute_error"], 4.858, places=3
        )
        self.assertEqual(summary["indices"]["direction_hit_rate"], 0.25)
        self.assertEqual(summary["indices"]["range_hit_rate"], 0.75)
        self.assertEqual(summary["stocks"]["direction_hit_rate"], 0.20)
        self.assertEqual(summary["stocks"]["range_hit_rate"], 0.40)
        self.assertEqual(
            {row["asset_code"] for row in records if row["asset_type"] == "stock"},
            {row["code"] for row in self.snapshot_0811["stocks"]},
        )

    def test_snapshot_is_an_explicit_eleven_field_historical_migration(self) -> None:
        self.assertEqual(set(self.snapshot_0811), SNAPSHOT_KEYS)
        self.assertEqual(self.snapshot_0811["market_status"], "historical_migration")
        self.assertEqual(
            self.snapshot_0811["quality_status"],
            "historical_transcription_not_live_gate_validated",
        )
        migration_text = " ".join(
            [
                str(self.snapshot_0811["quality_status"]),
                *map(str, self.snapshot_0811["warnings"]),
            ]
        )
        self.assertIn("historical", migration_text.lower())
        self.assertIn("5000", migration_text)
        self.assertTrue(
            any(
                "did not rerun the live 5000-stock quality gate" in warning.lower()
                for warning in self.snapshot_0811["warnings"]
            )
        )
        self.assertEqual(
            self.snapshot_0811["breadth"]["counts"],
            {"up": 1559, "down": 3506, "flat": 142, "total": 5207},
        )
        self.assertEqual(self.snapshot_0811["breadth"]["amount"], 2321000000000)

        self.assertEqual(
            {
                row["code"]: (row["name"], row["close"], row["pct"])
                for row in self.snapshot_0811["indices"]
            },
            {
                "000001": ("上证指数", 3934.09, -0.82),
                "399001": ("深证成指", 14259.44, -0.40),
                "399006": ("创业板指", 3549.16, 0.34),
                "000688": ("科创50", 1709.50, -1.63),
                "000905": ("中证500", 7967.54, -0.79),
                "000300": ("沪深300", 4663.79, -0.81),
            },
        )
        self.assertEqual(
            {
                row["code"]: (row["previous_close"], row["close"], row["pct"])
                for row in self.snapshot_0811["stocks"]
            },
            {
                "601899": (35.45, 33.18, -6.40),
                "600760": (45.10, 44.50, -1.33),
                "002458": (9.99, 10.52, 5.31),
                "300618": (39.90, 37.61, -5.74),
                "600547": (31.47, 30.05, -4.51),
            },
        )

    def test_forecasts_validate_as_initial_then_rolling_documents(self) -> None:
        normalized_0810 = validate_forecast(self.forecast_0810)
        normalized_0811 = validate_forecast(
            self.forecast_0811, previous=self.forecast_0810
        )

        self.assertEqual(normalized_0810["trade_date"], "2026-08-10")
        self.assertEqual(normalized_0811["trade_date"], "2026-08-11")

    def test_august_10_ranges_are_the_exact_approved_literals(self) -> None:
        index_ranges = {
            row["code"]: (row["lower"], row["upper"], row["midpoint"])
            for row in self.forecast_0810["indices"]
        }
        stock_ranges = {
            row["code"]: (row["lower"], row["upper"], row["midpoint"])
            for row in self.forecast_0810["watch_pool"]
        }
        expected_index_ranges = {
            "399006": (-1.8, 1.2, -0.3),
            "000688": (-2.0, 1.6, -0.2),
            "000905": (-0.8, 1.2, 0.2),
            "000300": (-0.7, 0.9, 0.1),
        }
        expected_stock_ranges = {
            "601899": (-2.5, 3.5, 0.5),
            "600760": (-2.5, 3.5, 0.5),
            "002458": (-4.0, 6.0, 1.0),
            "300618": (-5.0, 6.0, 0.5),
            "600547": (-3.0, 4.0, 0.5),
        }

        self.assertEqual(index_ranges, expected_index_ranges)
        self.assertEqual(stock_ranges, expected_stock_ranges)

    def test_august_11_pool_and_two_stock_history_are_exact(self) -> None:
        old_codes = [row["code"] for row in self.forecast_0810["watch_pool"]]
        new_codes = [row["code"] for row in self.forecast_0811["watch_pool"]]

        self.assertEqual(
            new_codes, ["601899", "600760", "002458", "300274", "300502"]
        )
        self.assertEqual(
            self.forecast_0811["changes"]["exited"], ["300618", "600547"]
        )
        self.assertEqual(
            self.forecast_0811["changes"]["entered"], ["300274", "300502"]
        )
        self.assertIn("300618", old_codes)
        self.assertIn("600547", old_codes)


if __name__ == "__main__":
    unittest.main()
