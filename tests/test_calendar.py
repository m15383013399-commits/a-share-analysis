from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from market_diary.calendar import (
    evaluate_market_gate,
    is_trade_day,
    load_calendar,
    next_trade_day,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
CALENDAR_PATH = Path(__file__).parents[1] / "config" / "trading_calendar_2026.json"


class CalendarGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.calendar = load_calendar(CALENDAR_PATH)

    def setUp(self) -> None:
        self.after_close = datetime(2026, 8, 11, 15, 20, tzinfo=SHANGHAI)

    def test_calendar_excludes_weekends_and_configured_weekday_closures(self) -> None:
        self.assertFalse(is_trade_day(date(2026, 8, 9), self.calendar))
        self.assertFalse(is_trade_day(date(2026, 10, 2), self.calendar))
        self.assertTrue(is_trade_day(date(2026, 8, 11), self.calendar))

    def test_holiday_reports_next_trade_day(self) -> None:
        result = evaluate_market_gate(
            now=datetime(2026, 10, 2, 16, 0, tzinfo=SHANGHAI),
            target_date=date(2026, 10, 2),
            calendar=self.calendar,
        )
        self.assertEqual(result.status, "closed")
        self.assertEqual(result.next_trade_date, "2026-10-08")

    def test_trade_day_before_1515_is_not_stable(self) -> None:
        result = evaluate_market_gate(
            now=datetime(2026, 8, 11, 15, 14, tzinfo=SHANGHAI),
            target_date=date(2026, 8, 11),
            calendar=self.calendar,
        )
        self.assertEqual(result.status, "market_not_stable")

    def test_timezone_aware_now_is_converted_to_shanghai_before_cutoff(self) -> None:
        result = evaluate_market_gate(
            now=datetime(2026, 8, 11, 7, 14, tzinfo=timezone.utc),
            target_date=date(2026, 8, 11),
            calendar=self.calendar,
        )
        self.assertEqual(result.status, "market_not_stable")

    def test_quote_timestamp_after_1510_is_ready(self) -> None:
        samples = [
            {
                "code": "399006",
                "trade_date": "2026-08-11",
                "quote_time": "15:10:01",
                "close": 3549.16,
            }
        ]
        result = evaluate_market_gate(
            now=self.after_close,
            target_date=date(2026, 8, 11),
            calendar=self.calendar,
            close_samples=samples,
        )
        self.assertEqual(result.status, "ready")

    def test_invalid_quote_time_cannot_pass_timestamp_stability(self) -> None:
        result = evaluate_market_gate(
            now=self.after_close,
            target_date=date(2026, 8, 11),
            calendar=self.calendar,
            close_samples=[
                {
                    "code": "000001",
                    "trade_date": "2026-08-11",
                    "quote_time": "99:99:99",
                    "close": 3934.09,
                }
            ],
        )
        self.assertEqual(result.status, "market_not_stable")

    def test_two_identical_close_reads_are_ready(self) -> None:
        samples = [
            {
                "code": "399006",
                "trade_date": "2026-08-11",
                "quote_time": "15:05:00",
                "close": 3549.16,
            },
            {
                "code": "399006",
                "trade_date": "2026-08-11",
                "quote_time": "15:06:00",
                "close": 3549.16,
            },
        ]
        result = evaluate_market_gate(
            now=self.after_close,
            target_date=date(2026, 8, 11),
            calendar=self.calendar,
            close_samples=samples,
        )
        self.assertEqual(result.status, "ready")

    def test_identical_close_reads_for_different_codes_are_not_stable(self) -> None:
        samples = [
            {
                "code": "000001",
                "trade_date": "2026-08-11",
                "quote_time": "15:05:00",
                "close": 3549.16,
            },
            {
                "code": "399006",
                "trade_date": "2026-08-11",
                "quote_time": "15:06:00",
                "close": 3549.16,
            },
        ]
        result = evaluate_market_gate(
            now=self.after_close,
            target_date=date(2026, 8, 11),
            calendar=self.calendar,
            close_samples=samples,
        )
        self.assertEqual(result.status, "market_not_stable")

    def test_calendar_rejects_dates_outside_its_declared_year(self) -> None:
        with self.assertRaisesRegex(ValueError, "calendar year"):
            is_trade_day(date(2027, 1, 4), self.calendar)
        with self.assertRaisesRegex(ValueError, "calendar year"):
            evaluate_market_gate(
                now=datetime(2027, 1, 4, 16, 0, tzinfo=SHANGHAI),
                target_date=date(2027, 1, 4),
                calendar=self.calendar,
                close_samples=[
                    {
                        "code": "000001",
                        "trade_date": "2027-01-04",
                        "quote_time": "15:10:01",
                        "close": 4000.0,
                    }
                ],
            )

    def test_next_trade_day_fails_closed_when_it_crosses_calendar_year(self) -> None:
        with self.assertRaisesRegex(ValueError, "next calendar year"):
            next_trade_day(date(2026, 12, 31), self.calendar)

    def test_load_calendar_validates_schema_and_closed_date_year(self) -> None:
        from tempfile import TemporaryDirectory

        cases = (
            ({"closed_dates": []}, "year"),
            ({"year": True, "closed_dates": []}, "year"),
            ({"year": 2026, "closed_dates": "bad"}, "closed_dates"),
            ({"year": 2026, "closed_dates": ["not-a-date"]}, "closed_dates"),
            ({"year": 2026, "closed_dates": ["2027-01-01"]}, "calendar year"),
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "calendar.json"
            for payload, message in cases:
                with self.subTest(payload=payload):
                    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_calendar(path)


if __name__ == "__main__":
    unittest.main()
