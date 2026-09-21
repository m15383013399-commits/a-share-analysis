from __future__ import annotations

import copy
import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from market_diary.forecast_ledger import ForecastValidationError, save_forecast, validate_forecast


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def changed_pool(
    previous: dict, replacements: int, exception_reason: str | None = None
) -> dict:
    current = copy.deepcopy(previous)
    current["trade_date"] = "2026-08-12"
    current["next_trade_date"] = "2026-08-13"
    exited = [row["code"] for row in current["watch_pool"][:replacements]]
    entered: list[str] = []
    for index in range(replacements):
        replacement = copy.deepcopy(current["watch_pool"][index])
        replacement["code"] = f"00000{index + 1}"
        replacement["name"] = f"替换股{index + 1}"
        current["watch_pool"][index] = replacement
        entered.append(replacement["code"])
    current["changes"] = {"exited": exited, "entered": entered, "reason": "rolling_update"}
    if exception_reason is not None:
        current["changes"]["exception_reason"] = exception_reason
    return current


class ForecastLedgerTest(unittest.TestCase):
    def test_valid_forecast_computes_midpoints_without_mutating_input(self) -> None:
        document = load_fixture("forecast_valid.json")
        original = copy.deepcopy(document)

        normalized = validate_forecast(document)

        self.assertEqual(normalized["indices"][0]["midpoint"], -0.3)
        self.assertEqual(normalized["watch_pool"][0]["midpoint"], 0.5)
        self.assertEqual(document, original)

    def test_requires_exactly_four_unique_core_indices(self) -> None:
        missing = load_fixture("forecast_valid.json")
        missing["indices"] = missing["indices"][:-1]
        with self.assertRaisesRegex(ForecastValidationError, "four core indices"):
            validate_forecast(missing)

        duplicate = load_fixture("forecast_valid.json")
        duplicate["indices"][-1]["code"] = "399006"
        with self.assertRaisesRegex(ForecastValidationError, "four core indices"):
            validate_forecast(duplicate)

    def test_core_index_codes_must_be_exact_strings(self) -> None:
        document = load_fixture("forecast_valid.json")
        document["indices"][0]["code"] = 399006
        with self.assertRaisesRegex(ForecastValidationError, "four core indices"):
            validate_forecast(document)

    def test_requires_three_to_five_unique_sectors(self) -> None:
        document = load_fixture("forecast_valid.json")
        document["sectors"] = document["sectors"][:2]
        with self.assertRaisesRegex(ForecastValidationError, "3 to 5 sectors"):
            validate_forecast(document)

        document = load_fixture("forecast_valid.json")
        document["sectors"][1]["code"] = document["sectors"][0]["code"]
        with self.assertRaisesRegex(ForecastValidationError, "unique sector"):
            validate_forecast(document)

    def test_rejects_wrong_midpoint_before_other_invalid_fields(self) -> None:
        with self.assertRaisesRegex(ForecastValidationError, "midpoint"):
            validate_forecast(load_fixture("forecast_invalid_midpoint.json"))

    def test_requires_exact_probability_keys_and_sum_of_100(self) -> None:
        document = load_fixture("forecast_valid.json")
        document["indices"][0]["probabilities"] = {"up": 40, "flat": 30, "other": 30}
        with self.assertRaisesRegex(ForecastValidationError, "up, flat, and down"):
            validate_forecast(document)

        document = load_fixture("forecast_valid.json")
        document["sectors"][0]["probabilities"]["up"] = 99
        with self.assertRaisesRegex(ForecastValidationError, "sum to 100"):
            validate_forecast(document)

    def test_rejects_reversed_or_non_finite_ranges(self) -> None:
        document = load_fixture("forecast_valid.json")
        document["watch_pool"][0]["lower"] = 4
        document["watch_pool"][0]["upper"] = -4
        with self.assertRaisesRegex(ForecastValidationError, "lower exceeds upper"):
            validate_forecast(document)

        document = load_fixture("forecast_valid.json")
        document["indices"][0]["lower"] = "NaN"
        with self.assertRaisesRegex(ForecastValidationError, "finite"):
            validate_forecast(document)

    def test_midpoint_derivation_does_not_overflow_large_positive_range(self) -> None:
        document = load_fixture("forecast_valid.json")
        document["indices"][0]["lower"] = 1e308
        document["indices"][0]["upper"] = 1e308

        with TemporaryDirectory() as tmp:
            path = save_forecast(document, tmp)
            persisted_text = path.read_text("utf-8")
            midpoint = json.loads(persisted_text)["indices"][0]["midpoint"]

        self.assertTrue(math.isfinite(midpoint))
        self.assertEqual(midpoint, 1e308)
        self.assertNotIn("Infinity", persisted_text)

    def test_midpoint_derivation_handles_opposite_finite_extremes(self) -> None:
        document = load_fixture("forecast_valid.json")
        document["indices"][0]["lower"] = -1e308
        document["indices"][0]["upper"] = 1e308

        midpoint = validate_forecast(document)["indices"][0]["midpoint"]

        self.assertTrue(math.isfinite(midpoint))
        self.assertEqual(midpoint, 0.0)

    def test_requires_five_unique_stocks(self) -> None:
        document = load_fixture("forecast_valid.json")
        document["watch_pool"].pop()
        with self.assertRaisesRegex(ForecastValidationError, "exactly 5"):
            validate_forecast(document)

        document = load_fixture("forecast_valid.json")
        document["watch_pool"][1]["code"] = document["watch_pool"][0]["code"]
        with self.assertRaisesRegex(ForecastValidationError, "unique stock"):
            validate_forecast(document)

    def test_requires_all_six_factor_assessments(self) -> None:
        document = load_fixture("forecast_valid.json")
        del document["watch_pool"][0]["factors"]["valuation"]
        with self.assertRaisesRegex(ForecastValidationError, "valuation"):
            validate_forecast(document)

    def test_factor_requires_allowed_state_and_nonempty_conclusion(self) -> None:
        document = load_fixture("forecast_valid.json")
        document["watch_pool"][0]["factors"]["risk"]["state"] = "bullish"
        with self.assertRaisesRegex(ForecastValidationError, "positive, neutral, or negative"):
            validate_forecast(document)

        document = load_fixture("forecast_valid.json")
        document["watch_pool"][0]["factors"]["risk"]["conclusion"] = "   "
        with self.assertRaisesRegex(ForecastValidationError, "conclusion"):
            validate_forecast(document)

    def test_requires_ordered_iso_dates_https_or_http_sources_and_fixed_disclaimer(self) -> None:
        document = load_fixture("forecast_valid.json")
        document["next_trade_date"] = document["trade_date"]
        with self.assertRaisesRegex(ForecastValidationError, "after trade_date"):
            validate_forecast(document)

        document = load_fixture("forecast_valid.json")
        document["sources"] = ["file:///tmp/quote.json"]
        with self.assertRaisesRegex(ForecastValidationError, "HTTP or HTTPS"):
            validate_forecast(document)

        document = load_fixture("forecast_valid.json")
        document["disclaimer"] = "仅供参考"
        with self.assertRaisesRegex(ForecastValidationError, "不构成投资建议"):
            validate_forecast(document)

    def test_first_pool_is_not_a_replacement_and_requires_empty_change_lists(self) -> None:
        normalized = validate_forecast(load_fixture("forecast_valid.json"))
        self.assertEqual(normalized["changes"]["exited"], [])

        document = load_fixture("forecast_valid.json")
        document["changes"]["entered"] = ["601899"]
        with self.assertRaisesRegex(ForecastValidationError, "initial watch pool"):
            validate_forecast(document)

    def test_normal_replacement_limit_is_two(self) -> None:
        previous = load_fixture("forecast_valid.json")
        current = changed_pool(previous, replacements=3)
        with self.assertRaisesRegex(ForecastValidationError, "at most 2"):
            validate_forecast(current, previous=previous)

    def test_non_opinion_exception_allows_more_than_two_replacements(self) -> None:
        previous = load_fixture("forecast_valid.json")
        current = changed_pool(previous, replacements=3, exception_reason="data_error")
        self.assertEqual(len(validate_forecast(current, previous=previous)["watch_pool"]), 5)

    def test_only_declared_non_opinion_exceptions_are_allowed(self) -> None:
        previous = load_fixture("forecast_valid.json")
        current = changed_pool(previous, replacements=3, exception_reason="market_opinion")
        with self.assertRaisesRegex(ForecastValidationError, "exception_reason"):
            validate_forecast(current, previous=previous)

    def test_declared_changes_must_match_actual_exits_and_entries(self) -> None:
        previous = load_fixture("forecast_valid.json")
        current = changed_pool(previous, replacements=1)
        current["changes"]["entered"] = []
        with self.assertRaisesRegex(ForecastValidationError, "equal size"):
            validate_forecast(current, previous=previous)

        current = changed_pool(previous, replacements=1)
        current["changes"]["exited"] = ["not-the-actual-code"]
        with self.assertRaisesRegex(ForecastValidationError, "actual watch-pool difference"):
            validate_forecast(current, previous=previous)

    def test_previous_forecast_must_precede_current_forecast(self) -> None:
        previous = load_fixture("forecast_valid.json")
        current = changed_pool(previous, replacements=1)
        current["trade_date"] = previous["trade_date"]
        with self.assertRaisesRegex(ForecastValidationError, "after previous"):
            validate_forecast(current, previous=previous)

    def test_save_forecast_uses_trade_date_filename_and_writes_normalized_json(self) -> None:
        document = load_fixture("forecast_valid.json")
        with TemporaryDirectory() as tmp:
            path = save_forecast(document, tmp)

            self.assertEqual(path, Path(tmp) / "2026-08-11.json")
            persisted = json.loads(path.read_text("utf-8"))
            self.assertEqual(persisted["indices"][0]["midpoint"], -0.3)
            self.assertEqual(persisted["watch_pool"][0]["midpoint"], 0.5)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
