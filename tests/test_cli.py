from __future__ import annotations

import copy
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from market_diary.forecast_ledger import validate_forecast
from market_diary.market_data import MarketDataClient
from market_diary.utils import load_json_file
from scripts import run_daily_report as cli
from scripts.run_daily_report import main, parse_args


ROOT = Path(__file__).parents[1]
FORECAST_FIXTURE = ROOT / "tests" / "fixtures" / "forecast_valid.json"
HISTORICAL_SNAPSHOT = ROOT / "data" / "snapshots" / "2026-08-11.json"
HISTORICAL_FORECAST = ROOT / "data" / "forecasts" / "2026-08-10.json"


def valid_snapshot(trade_date: str = "2026-08-12") -> dict:
    index_codes = ("000001", "399001", "399006", "000688", "000905", "000300")
    stock_codes = ("601899", "600760", "002458", "300618", "600547")
    stocks = []
    for position, code in enumerate(stock_codes):
        previous_close = 10.0 + position
        pct = 5.0 if position < 3 else -5.0 if position == 3 else 0.0
        stocks.append(
            {
                "code": code,
                "name": code,
                "trade_date": trade_date,
                "close": previous_close * (1 + pct / 100),
                "previous_close": previous_close,
                "pct": pct,
                "amount": 100_000_000.0,
                "direction": "up" if position < 3 else "down" if position == 3 else "flat",
                "quote_time": "15:00:00",
                "source": "fixture",
            }
        )
    return {
        "schema_version": "1.0",
        "trade_date": trade_date,
        "generated_at": f"{trade_date}T15:20:00+08:00",
        "market_status": "ready",
        "quality_status": "passed",
        "sources": {"indices": {"source": "fixture"}},
        "indices": [
            {
                "code": code,
                "name": code,
                "trade_date": trade_date,
                "close": 3000.0 + position,
                "pct": 0.1,
                "amount": 1_000_000_000.0,
                "quote_time": "15:10:00",
                "source": "fixture",
            }
            for position, code in enumerate(index_codes)
        ],
        "breadth": {
            "counts": {"up": 3, "down": 1, "flat": 1, "total": 5},
            "sh_amount": 200_000_000.0,
            "sz_amount": 300_000_000.0,
            "amount": 500_000_000.0,
        },
        "sectors": {"industries": [], "concepts": []},
        "stocks": stocks,
        "warnings": [],
    }


def dated_forecast(
    trade_date: str,
    next_trade_date: str,
    *,
    previous: dict | None = None,
    replacement: tuple[str, str, str] | None = None,
) -> dict:
    document = load_json_file(FORECAST_FIXTURE)
    document["trade_date"] = trade_date
    document["next_trade_date"] = next_trade_date
    if replacement is not None:
        old_code, new_code, new_name = replacement
        row = next(row for row in document["watch_pool"] if row["code"] == old_code)
        row["code"] = new_code
        row["name"] = new_name
    if previous is None:
        document["changes"] = {"exited": [], "entered": [], "reason": "initial_pool"}
    else:
        previous_codes = {row["code"] for row in previous["watch_pool"]}
        current_codes = {row["code"] for row in document["watch_pool"]}
        document["changes"] = {
            "exited": sorted(previous_codes - current_codes),
            "entered": sorted(current_codes - previous_codes),
            "reason": "rolling",
        }
    return document


class FakeMarketDataClient:
    def __init__(
        self,
        snapshot: dict,
        *,
        close_values: tuple[float, float] = (3000.0, 3000.0),
        close_source: str = "tencent",
    ) -> None:
        self.snapshot = snapshot
        self.close_values = close_values
        self.close_source = close_source
        self.read_count = 0
        self.requested_index_codes: list[tuple[str, ...]] = []

    def fetch_index_quotes(self, codes, trade_date):
        self.requested_index_codes.append(tuple(codes))
        value = self.close_values[min(self.read_count, len(self.close_values) - 1)]
        self.read_count += 1
        return SimpleNamespace(
            source=self.close_source,
            rows=[
                {
                    "code": "000001",
                    "trade_date": trade_date,
                    "close": value,
                    "quote_time": "15:00:00",
                    "source": self.close_source,
                }
            ]
        )

    def fetch_close_stability_quotes(self, codes, trade_date):
        return self.fetch_index_quotes(codes, trade_date)

    def build_snapshot(self, trade_date, watch_codes):
        result = copy.deepcopy(self.snapshot)
        result["trade_date"] = trade_date
        return result


def write_config(directory: Path, *, now: str, **updates) -> Path:
    config = {
        "report_name": "CLI fixture",
        "timezone": "Asia/Shanghai",
        "now": now,
        "calendar_path": str(ROOT / "config" / "trading_calendar_2026.json"),
        "output_dir": str(directory / "reports"),
        "raw_data_dir": str(directory / "raw"),
        "snapshot_dir": str(directory / "snapshots"),
        "forecast_dir": str(directory / "forecasts"),
        "evaluation_detail_path": str(directory / "evaluations.jsonl"),
        "evaluation_summary_path": str(directory / "summary.json"),
        "min_stock_count": 5,
        "watch_codes": ["601899", "600760", "002458", "300618", "600547"],
    }
    config.update(updates)
    path = directory / "config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


def quiet_main(argv: list[str]) -> int:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return main(argv)


def quiet_parse_args(argv: list[str]):
    with redirect_stderr(StringIO()):
        return parse_args(argv)


def install_historical_evaluation_case(
    directory: Path,
    *,
    market_status: str,
    quality_status: str,
) -> Path:
    config = write_config(directory, now="2026-08-11T16:00:00+08:00")
    if not HISTORICAL_SNAPSHOT.exists() or not HISTORICAL_FORECAST.exists():
        raise unittest.SkipTest("Private historical ledger is not distributed")
    snapshot = load_json_file(HISTORICAL_SNAPSHOT)
    snapshot["market_status"] = market_status
    snapshot["quality_status"] = quality_status
    snapshot_dir = directory / "snapshots"
    forecast_dir = directory / "forecasts"
    snapshot_dir.mkdir()
    forecast_dir.mkdir()
    (snapshot_dir / "2026-08-11.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )
    (forecast_dir / "2026-08-10.json").write_text(
        HISTORICAL_FORECAST.read_text("utf-8"), encoding="utf-8"
    )
    return config


class CliModeTest(unittest.TestCase):
    def test_close_stability_always_uses_tencent_regardless_of_index_primary_order(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = write_config(
                Path(tmp), now="2026-08-11T16:00:00+08:00"
            )
            config = cli.load_config(str(config_path))
            for order in (("tencent", "akshare"), ("akshare", "tencent")):
                with self.subTest(order=order):
                    akshare_calls: list[str] = []

                    def akshare_fetch(trade_date: str) -> dict:
                        akshare_calls.append(trade_date)
                        return {
                            "indices": [
                                {
                                    "代码": "000001",
                                    "名称": "上证指数",
                                    "最新价": 3320.50,
                                    "昨收": 3300.00,
                                    "成交额": 456789012.34,
                                }
                            ]
                        }

                    client = MarketDataClient(
                        get=lambda *_args, **_kwargs: SimpleNamespace(
                            text=(ROOT / "tests" / "fixtures" / "tencent_quotes.txt").read_text(
                                encoding="utf-8"
                            ),
                            raise_for_status=lambda: None,
                        ),
                        index_sources=order,
                        akshare_fetch=akshare_fetch,
                    )

                    gate = cli._stable_gate(config, "2026-08-11", client)

                    self.assertEqual(gate.status, "ready")
                    self.assertEqual(akshare_calls, [])

    def test_market_data_client_consumes_configured_source_orders_and_fixed_indices(self) -> None:
        required = ("399001", "000001", "399006", "000688", "000905", "000300")
        client = cli.build_market_data_client(
            {
                "market_data": {
                    "timeout_seconds": 3,
                    "index_sources": ["akshare", "tencent"],
                    "breadth_sources": ["akshare", "sina"],
                    "sector_sources": ["sina", "eastmoney"],
                    "required_index_codes": list(required),
                }
            }
        )

        self.assertEqual(client.timeout_seconds, 3)
        self.assertEqual(client.index_sources, ("akshare", "tencent"))
        self.assertEqual(client.breadth_sources, ("akshare", "sina"))
        self.assertEqual(client.sector_sources, ("sina", "eastmoney"))
        self.assertEqual(client.required_index_codes, required)

    def test_quality_consumes_required_indices_from_market_data_config(self) -> None:
        snapshot = valid_snapshot()
        config = {
            "min_stock_count": 5,
            "market_data": {
                "required_index_codes": [
                    "000001",
                    "399001",
                    "399006",
                    "000688",
                    "000905",
                    "000016",
                ]
            },
        }

        with self.assertRaises(ValueError):
            cli._quality(config, snapshot, [row["code"] for row in snapshot["stocks"]])

    def test_explicit_nonobject_market_data_config_is_rejected_by_both_boundaries(self) -> None:
        snapshot = valid_snapshot()
        watch_codes = [row["code"] for row in snapshot["stocks"]]
        for invalid in (None, [], "invalid"):
            config = {"min_stock_count": 5, "market_data": invalid}
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "config.market_data"):
                    cli.build_market_data_client(config)
                with self.assertRaisesRegex(ValueError, "config.market_data"):
                    cli._quality(config, snapshot, watch_codes)

    def test_explicit_nonobject_market_data_config_returns_cli_exit_two(self) -> None:
        with TemporaryDirectory() as tmp:
            config = write_config(
                Path(tmp),
                now="2026-08-11T16:00:00+08:00",
                market_data=[],
            )

            with mock.patch(
                "requests.get", side_effect=AssertionError("network should not be reached")
            ):
                result = quiet_main(
                    ["--snapshot", "--date", "2026-08-11", "--config", str(config)]
                )

            self.assertEqual(result, 2)

    def test_nonfinite_or_string_quality_tolerance_returns_exit_two_without_snapshot(self) -> None:
        invalid_values = ("NaN", "Infinity", "-Infinity", "0.05", -1, True)
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), TemporaryDirectory() as tmp:
                base = Path(tmp)
                config = write_config(
                    base,
                    now="2026-08-11T15:00:00+08:00",
                    index_pct_tolerance=invalid,
                )
                client = FakeMarketDataClient(valid_snapshot("2026-08-11"))

                with mock.patch.object(cli, "build_market_data_client", return_value=client):
                    result = quiet_main(
                        [
                            "--snapshot",
                            "--allow-preclose",
                            "--date",
                            "2026-08-11",
                            "--config",
                            str(config),
                        ]
                    )

                self.assertEqual(result, 2)
                self.assertFalse((base / "snapshots" / "2026-08-11.json").exists())

    def test_finite_numeric_quality_tolerances_still_pass_cli_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(
                base,
                now="2026-08-11T15:00:00+08:00",
                index_pct_tolerance=0.05,
                index_level_tolerance_pct=0.05,
            )
            client = FakeMarketDataClient(valid_snapshot("2026-08-11"))

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(
                    [
                        "--snapshot",
                        "--allow-preclose",
                        "--date",
                        "2026-08-11",
                        "--config",
                        str(config),
                    ]
                )

            self.assertEqual(result, 0)

    def test_new_modes_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            quiet_parse_args(["--snapshot", "--evaluate"])

    def test_sample_cannot_be_combined_with_formal_modes_or_preclose(self) -> None:
        combinations = (
            ["--sample", "--snapshot"],
            ["--sample", "--evaluate"],
            ["--sample", "--forecast-file", "forecast.json"],
            ["--sample", "--full-close-run"],
            ["--sample", "--allow-preclose"],
            ["--evaluate", "--allow-preclose"],
            ["--forecast-file", "forecast.json", "--allow-preclose"],
        )
        for argv in combinations:
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                quiet_parse_args(argv)

    def test_legacy_flags_remain_available(self) -> None:
        args = quiet_parse_args(["--sample", "--no-news", "--save-raw"])

        self.assertTrue(args.sample)
        self.assertTrue(args.no_news)
        self.assertTrue(args.save_raw)

    def test_preclose_snapshot_writes_only_status_report(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-11T15:00:00+08:00")

            result = quiet_main(["--snapshot", "--date", "2026-08-11", "--config", str(config)])

            self.assertEqual(result, 3)
            self.assertFalse((base / "evaluations.jsonl").exists())
            self.assertFalse((base / "snapshots" / "2026-08-11.json").exists())
            report = (base / "reports" / "a_share_daily_2026-08-11.md").read_text("utf-8")
            self.assertIn("market_not_stable", report)
            self.assertNotIn("市场温度", report)

    def test_allow_preclose_writes_unofficial_snapshot_and_never_evaluates(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-11T15:00:00+08:00")
            client = FakeMarketDataClient(valid_snapshot("2026-08-11"))

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(
                    [
                        "--snapshot",
                        "--allow-preclose",
                        "--date",
                        "2026-08-11",
                        "--config",
                        str(config),
                    ]
                )

            snapshot = load_json_file(base / "snapshots" / "2026-08-11.json")
            self.assertEqual(result, 0)
            self.assertEqual(snapshot["market_status"], "unofficial_preclose")
            self.assertFalse((base / "evaluations.jsonl").exists())

    def test_snapshot_writer_preserves_exact_eleven_key_contract(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-11T15:00:00+08:00")
            source_snapshot = valid_snapshot("2026-08-11")
            source_snapshot["watch_pool_quotes"] = [{"code": "601899"}]
            client = FakeMarketDataClient(source_snapshot)

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(
                    [
                        "--snapshot",
                        "--allow-preclose",
                        "--date",
                        "2026-08-11",
                        "--config",
                        str(config),
                    ]
                )

            snapshot = load_json_file(base / "snapshots" / "2026-08-11.json")
            self.assertEqual(result, 0)
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

    def test_late_clock_does_not_bypass_two_read_close_stability(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-11T16:00:00+08:00")
            client = FakeMarketDataClient(
                valid_snapshot("2026-08-11"), close_values=(3000.0, 3001.0)
            )

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(["--snapshot", "--date", "2026-08-11", "--config", str(config)])

            self.assertEqual(result, 3)
            self.assertEqual(client.read_count, 2)
            self.assertEqual(
                client.requested_index_codes, [("000001",), ("000001",)]
            )
            self.assertFalse((base / "snapshots" / "2026-08-11.json").exists())

    def test_akshare_fallback_cannot_prove_historical_close_stability(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T16:00:00+08:00")
            client = FakeMarketDataClient(
                valid_snapshot("2026-08-11"),
                close_values=(3000.0, 3000.0),
                close_source="akshare",
            )

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(
                    ["--snapshot", "--date", "2026-08-11", "--config", str(config)]
                )

            self.assertEqual(result, 3)
            self.assertEqual(client.read_count, 2)
            self.assertFalse((base / "snapshots" / "2026-08-11.json").exists())

    def test_failed_quality_saves_diagnostic_but_renders_no_temperature(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-11T15:20:00+08:00")
            snapshot = valid_snapshot("2026-08-11")
            snapshot["stocks"] = []
            snapshot["breadth"]["counts"] = {"up": 0, "down": 0, "flat": 0, "total": 0}
            client = FakeMarketDataClient(snapshot)

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(["--full-close-run", "--date", "2026-08-11", "--config", str(config)])

            diagnostic = load_json_file(base / "snapshots" / "2026-08-11.json")
            report = (base / "reports" / "a_share_daily_2026-08-11.md").read_text("utf-8")
            self.assertEqual(result, 4)
            self.assertEqual(diagnostic["quality_status"], "failed_quality_gate")
            self.assertIn("failed_quality_gate", report)
            self.assertNotIn("市场温度", report)
            self.assertFalse((base / "evaluations.jsonl").exists())

    def test_forecast_file_is_validated_before_copy(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-11T15:20:00+08:00")

            result = quiet_main(["--forecast-file", str(FORECAST_FIXTURE), "--config", str(config)])

            stored = load_json_file(base / "forecasts" / "2026-08-11.json")
            self.assertEqual(result, 0)
            self.assertEqual(stored, validate_forecast(load_json_file(FORECAST_FIXTURE)))

    def test_invalid_forecast_is_not_copied(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-11T15:20:00+08:00")
            invalid = ROOT / "tests" / "fixtures" / "forecast_invalid_midpoint.json"

            result = quiet_main(["--forecast-file", str(invalid), "--config", str(config)])

            self.assertEqual(result, 5)
            self.assertFalse((base / "forecasts").exists())

    def test_evaluation_rejects_incomplete_previous_without_writing_results(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T16:00:00+08:00")
            forecast_dir = base / "forecasts"
            snapshot_dir = base / "snapshots"
            forecast_dir.mkdir()
            snapshot_dir.mkdir()
            incomplete_previous = {
                "trade_date": "2026-08-10",
                "next_trade_date": "2026-08-11",
                "watch_pool": [
                    {"code": code}
                    for code in ("601899", "600760", "002458", "300618", "600547")
                ],
            }
            current = dated_forecast(
                "2026-08-11", "2026-08-12", previous=incomplete_previous
            )
            (forecast_dir / "2026-08-10.json").write_text(
                json.dumps(incomplete_previous, ensure_ascii=False), encoding="utf-8"
            )
            (forecast_dir / "2026-08-11.json").write_text(
                json.dumps(current, ensure_ascii=False), encoding="utf-8"
            )
            (snapshot_dir / "2026-08-12.json").write_text(
                json.dumps(valid_snapshot(), ensure_ascii=False), encoding="utf-8"
            )

            result = quiet_main(
                ["--evaluate", "--date", "2026-08-12", "--config", str(config)]
            )

            self.assertEqual(result, 5)
            self.assertFalse((base / "evaluations.jsonl").exists())
            self.assertFalse((base / "summary.json").exists())

    def test_evaluation_rejects_forecast_filename_that_disagrees_with_trade_date(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T16:00:00+08:00")
            forecast_dir = base / "forecasts"
            snapshot_dir = base / "snapshots"
            forecast_dir.mkdir()
            snapshot_dir.mkdir()
            (forecast_dir / "wrong-name.json").write_text(
                FORECAST_FIXTURE.read_text("utf-8"), encoding="utf-8"
            )
            (snapshot_dir / "2026-08-12.json").write_text(
                json.dumps(valid_snapshot(), ensure_ascii=False), encoding="utf-8"
            )

            result = quiet_main(
                ["--evaluate", "--date", "2026-08-12", "--config", str(config)]
            )

            self.assertEqual(result, 5)
            self.assertFalse((base / "evaluations.jsonl").exists())

    def test_evaluation_rejects_duplicate_trade_dates_even_with_distinct_targets(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T16:00:00+08:00")
            forecast_dir = base / "forecasts"
            snapshot_dir = base / "snapshots"
            forecast_dir.mkdir()
            snapshot_dir.mkdir()
            duplicate = load_json_file(FORECAST_FIXTURE)
            duplicate["next_trade_date"] = "2026-08-13"
            (forecast_dir / "2026-08-11.json").write_text(
                FORECAST_FIXTURE.read_text("utf-8"), encoding="utf-8"
            )
            (forecast_dir / "duplicate.json").write_text(
                json.dumps(duplicate, ensure_ascii=False), encoding="utf-8"
            )
            (snapshot_dir / "2026-08-12.json").write_text(
                json.dumps(valid_snapshot(), ensure_ascii=False), encoding="utf-8"
            )

            result = quiet_main(
                ["--evaluate", "--date", "2026-08-12", "--config", str(config)]
            )

            self.assertEqual(result, 5)
            self.assertFalse((base / "evaluations.jsonl").exists())

    def test_register_rejects_middle_overwrite_that_breaks_later_rolling_chain(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T16:00:00+08:00")
            forecast_dir = base / "forecasts"
            forecast_dir.mkdir()
            initial = dated_forecast("2026-08-10", "2026-08-11")
            middle = dated_forecast(
                "2026-08-11", "2026-08-12", previous=initial
            )
            later = dated_forecast("2026-08-12", "2026-08-13", previous=middle)
            for document in (initial, middle, later):
                (forecast_dir / f"{document['trade_date']}.json").write_text(
                    json.dumps(document, ensure_ascii=False), encoding="utf-8"
                )
            middle_path = forecast_dir / "2026-08-11.json"
            original_bytes = middle_path.read_bytes()
            candidate = dated_forecast(
                "2026-08-11",
                "2026-08-12",
                previous=initial,
                replacement=("601899", "600000", "浦发银行"),
            )
            candidate_path = base / "candidate.json"
            candidate_path.write_text(
                json.dumps(candidate, ensure_ascii=False), encoding="utf-8"
            )

            result = quiet_main(
                ["--forecast-file", str(candidate_path), "--config", str(config)]
            )

            self.assertEqual(result, 5)
            self.assertEqual(middle_path.read_bytes(), original_bytes)

    def test_register_refuses_overwriting_damaged_canonical_middle(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T16:00:00+08:00")
            forecast_dir = base / "forecasts"
            forecast_dir.mkdir()
            initial = dated_forecast("2026-08-10", "2026-08-11")
            candidate = dated_forecast(
                "2026-08-11",
                "2026-08-12",
                previous=initial,
                replacement=("601899", "600000", "浦发银行"),
            )
            later = dated_forecast("2026-08-12", "2026-08-13", previous=candidate)
            damaged = copy.deepcopy(candidate)
            damaged.pop("indices")
            for document in (initial, later):
                (forecast_dir / f"{document['trade_date']}.json").write_text(
                    json.dumps(document, ensure_ascii=False), encoding="utf-8"
                )
            middle_path = forecast_dir / "2026-08-11.json"
            middle_path.write_text(
                json.dumps(damaged, ensure_ascii=False), encoding="utf-8"
            )
            candidate_path = base / "candidate.json"
            candidate_path.write_text(
                json.dumps(candidate, ensure_ascii=False), encoding="utf-8"
            )

            result = quiet_main(
                ["--forecast-file", str(candidate_path), "--config", str(config)]
            )

            self.assertEqual(result, 5)
            self.assertEqual(
                load_json_file(middle_path),
                damaged,
            )

    def test_register_replacement_does_not_ignore_other_invalid_history_files(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T16:00:00+08:00")
            forecast_dir = base / "forecasts"
            forecast_dir.mkdir()
            initial = dated_forecast("2026-08-10", "2026-08-11")
            candidate = dated_forecast("2026-08-11", "2026-08-12", previous=initial)
            (forecast_dir / "2026-08-10.json").write_text(
                json.dumps(initial, ensure_ascii=False), encoding="utf-8"
            )
            (forecast_dir / "broken.json").write_text("{not-json", encoding="utf-8")
            candidate_path = base / "candidate.json"
            candidate_path.write_text(
                json.dumps(candidate, ensure_ascii=False), encoding="utf-8"
            )

            result = quiet_main(
                ["--forecast-file", str(candidate_path), "--config", str(config)]
            )

            self.assertEqual(result, 5)
            self.assertFalse((forecast_dir / "2026-08-11.json").exists())

    def test_register_replacement_keeps_same_date_noncanonical_duplicate_visible(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T16:00:00+08:00")
            forecast_dir = base / "forecasts"
            forecast_dir.mkdir()
            initial = dated_forecast("2026-08-10", "2026-08-11")
            candidate = dated_forecast("2026-08-11", "2026-08-12", previous=initial)
            (forecast_dir / "2026-08-10.json").write_text(
                json.dumps(initial, ensure_ascii=False), encoding="utf-8"
            )
            canonical_path = forecast_dir / "2026-08-11.json"
            canonical_bytes = b"{not-json"
            canonical_path.write_bytes(canonical_bytes)
            (forecast_dir / "duplicate.json").write_text(
                json.dumps(candidate, ensure_ascii=False), encoding="utf-8"
            )
            candidate_path = base / "candidate.json"
            candidate_path.write_text(
                json.dumps(candidate, ensure_ascii=False), encoding="utf-8"
            )

            result = quiet_main(
                ["--forecast-file", str(candidate_path), "--config", str(config)]
            )

            self.assertEqual(result, 5)
            self.assertEqual(canonical_path.read_bytes(), canonical_bytes)

    def test_register_invalid_replacement_preserves_existing_canonical_bytes(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T16:00:00+08:00")
            forecast_dir = base / "forecasts"
            forecast_dir.mkdir()
            existing = dated_forecast("2026-08-11", "2026-08-12")
            canonical_path = forecast_dir / "2026-08-11.json"
            canonical_path.write_text(
                json.dumps(existing, ensure_ascii=False), encoding="utf-8"
            )
            original_bytes = canonical_path.read_bytes()
            candidate = copy.deepcopy(existing)
            candidate["indices"][0]["midpoint"] = 99
            candidate_path = base / "candidate.json"
            candidate_path.write_text(
                json.dumps(candidate, ensure_ascii=False), encoding="utf-8"
            )

            result = quiet_main(
                ["--forecast-file", str(candidate_path), "--config", str(config)]
            )

            self.assertEqual(result, 5)
            self.assertEqual(canonical_path.read_bytes(), original_bytes)

    def test_register_accepts_initial_then_rolling_forecast_chain(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T16:00:00+08:00")
            initial = dated_forecast("2026-08-10", "2026-08-11")
            rolling = dated_forecast(
                "2026-08-11",
                "2026-08-12",
                previous=initial,
                replacement=("601899", "600000", "浦发银行"),
            )
            initial_path = base / "initial.json"
            rolling_path = base / "rolling.json"
            initial_path.write_text(
                json.dumps(initial, ensure_ascii=False), encoding="utf-8"
            )
            rolling_path.write_text(
                json.dumps(rolling, ensure_ascii=False), encoding="utf-8"
            )

            initial_result = quiet_main(
                ["--forecast-file", str(initial_path), "--config", str(config)]
            )
            rolling_result = quiet_main(
                ["--forecast-file", str(rolling_path), "--config", str(config)]
            )

            self.assertEqual((initial_result, rolling_result), (0, 0))
            self.assertTrue((base / "forecasts" / "2026-08-10.json").exists())
            self.assertTrue((base / "forecasts" / "2026-08-11.json").exists())

    def test_missing_calendar_config_returns_fetch_or_config_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(
                base,
                now="2026-08-11T15:20:00+08:00",
                calendar_path=str(base / "missing-calendar.json"),
            )

            result = quiet_main(["--snapshot", "--date", "2026-08-11", "--config", str(config)])

            self.assertEqual(result, 2)

    def test_exact_historical_transcription_pair_allows_offline_evaluation(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = install_historical_evaluation_case(
                base,
                market_status="historical_migration",
                quality_status="historical_transcription_not_live_gate_validated",
            )

            result = quiet_main(
                ["--evaluate", "--date", "2026-08-11", "--config", str(config)]
            )

            self.assertEqual(result, 0)
            rows = [
                json.loads(line)
                for line in (base / "evaluations.jsonl").read_text("utf-8").splitlines()
            ]
            self.assertTrue(rows)
            self.assertEqual({row["actual_date"] for row in rows}, {"2026-08-11"})

    def test_ready_passed_pair_allows_offline_evaluation(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = install_historical_evaluation_case(
                base,
                market_status="ready",
                quality_status="passed",
            )

            result = quiet_main(
                ["--evaluate", "--date", "2026-08-11", "--config", str(config)]
            )

            self.assertEqual(result, 0)
            self.assertTrue((base / "evaluations.jsonl").exists())

    def test_partial_or_other_nonpassed_status_pairs_remain_rejected(self) -> None:
        cases = (
            ("ready", "failed_quality_gate", 4),
            ("historical_migration", "failed_quality_gate", 4),
            ("ready", "historical_transcription_not_live_gate_validated", 4),
            ("historical_migration", "passed", 4),
            ("closed", "passed", 4),
            ("unknown", "passed", 4),
            ("unofficial_preclose", "passed", 3),
        )
        for market_status, quality_status, expected in cases:
            with self.subTest(
                market_status=market_status, quality_status=quality_status
            ), TemporaryDirectory() as tmp:
                base = Path(tmp)
                config = install_historical_evaluation_case(
                    base,
                    market_status=market_status,
                    quality_status=quality_status,
                )

                result = quiet_main(
                    ["--evaluate", "--date", "2026-08-11", "--config", str(config)]
                )

                self.assertEqual(result, expected)
                self.assertFalse((base / "evaluations.jsonl").exists())

    def test_full_close_without_forecast_writes_facts_and_marks_no_review(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T15:20:00+08:00")
            client = FakeMarketDataClient(valid_snapshot())

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(["--full-close-run", "--date", "2026-08-12", "--config", str(config)])

            report = (base / "reports" / "a_share_daily_2026-08-12.md").read_text("utf-8")
            self.assertEqual(result, 0)
            self.assertIn("无可复盘预测", report)
            self.assertIn("正式收盘事实", report)
            self.assertFalse((base / "evaluations.jsonl").exists())

    def test_target_forecast_watch_pool_overrides_stale_config_codes(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(
                base,
                now="2026-08-12T15:20:00+08:00",
                watch_codes=["000001", "000002", "000003", "000004", "000005"],
            )
            forecast_dir = base / "forecasts"
            forecast_dir.mkdir()
            (forecast_dir / "2026-08-11.json").write_text(
                FORECAST_FIXTURE.read_text("utf-8"), encoding="utf-8"
            )
            client = FakeMarketDataClient(valid_snapshot())

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(["--snapshot", "--date", "2026-08-12", "--config", str(config)])

            self.assertEqual(result, 0)

    def test_invalid_forecast_json_during_snapshot_returns_validation_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T15:20:00+08:00")
            forecast_dir = base / "forecasts"
            forecast_dir.mkdir()
            (forecast_dir / "broken.json").write_text("{not-json", encoding="utf-8")
            client = FakeMarketDataClient(valid_snapshot())

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(
                    ["--snapshot", "--date", "2026-08-12", "--config", str(config)]
                )

            self.assertEqual(result, 5)
            self.assertFalse((base / "snapshots" / "2026-08-12.json").exists())

    def test_invalid_forecast_date_during_snapshot_returns_validation_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T15:20:00+08:00")
            forecast_dir = base / "forecasts"
            forecast_dir.mkdir()
            document = load_json_file(FORECAST_FIXTURE)
            document["next_trade_date"] = "20260812"
            (forecast_dir / "invalid-date.json").write_text(
                json.dumps(document, ensure_ascii=False), encoding="utf-8"
            )
            client = FakeMarketDataClient(valid_snapshot())

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(
                    ["--snapshot", "--date", "2026-08-12", "--config", str(config)]
                )

            self.assertEqual(result, 5)
            self.assertFalse((base / "snapshots" / "2026-08-12.json").exists())

    def test_duplicate_target_forecasts_during_full_close_return_validation_exit(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T15:20:00+08:00")
            forecast_dir = base / "forecasts"
            forecast_dir.mkdir()
            for name in ("one.json", "two.json"):
                (forecast_dir / name).write_text(
                    FORECAST_FIXTURE.read_text("utf-8"), encoding="utf-8"
                )
            client = FakeMarketDataClient(valid_snapshot())

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(
                    ["--full-close-run", "--date", "2026-08-12", "--config", str(config)]
                )

            self.assertEqual(result, 5)
            self.assertFalse((base / "snapshots" / "2026-08-12.json").exists())

    def test_full_close_loads_real_previous_forecast_for_rolling_validation(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write_config(base, now="2026-08-12T15:20:00+08:00")
            forecast_dir = base / "forecasts"
            forecast_dir.mkdir()
            current = load_json_file(FORECAST_FIXTURE)
            previous = copy.deepcopy(current)
            previous["trade_date"] = "2026-08-10"
            previous["next_trade_date"] = "2026-08-11"
            previous["watch_pool"][0]["code"] = "600000"
            previous["watch_pool"][0]["name"] = "浦发银行"
            previous["changes"] = {"exited": [], "entered": [], "reason": "initial_pool"}
            current["changes"] = {
                "exited": ["600000"],
                "entered": ["601899"],
                "reason": "rolling",
            }
            (forecast_dir / "2026-08-10.json").write_text(
                json.dumps(validate_forecast(previous), ensure_ascii=False), encoding="utf-8"
            )
            (forecast_dir / "2026-08-11.json").write_text(
                json.dumps(validate_forecast(current, previous=previous), ensure_ascii=False),
                encoding="utf-8",
            )
            client = FakeMarketDataClient(valid_snapshot())

            with mock.patch.object(cli, "build_market_data_client", return_value=client):
                result = quiet_main(["--full-close-run", "--date", "2026-08-12", "--config", str(config)])

            rows = [
                json.loads(line)
                for line in (base / "evaluations.jsonl").read_text("utf-8").splitlines()
            ]
            self.assertEqual(result, 0)
            self.assertEqual(len(rows), 12)
            self.assertEqual({row["forecast_date"] for row in rows}, {"2026-08-11"})

    def test_sample_default_keeps_explicit_non_official_warning(self) -> None:
        with TemporaryDirectory() as tmp:
            output = Path(tmp) / "sample.md"

            result = quiet_main(["--sample", "--date", "2026-08-11", "--output", str(output)])

            self.assertEqual(result, 0)
            self.assertIn("样例警告", output.read_text("utf-8"))

    def test_shell_wrapper_forwards_cli_exit_code(self) -> None:
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts" / "run_report.sh"),
                "--snapshot",
                "--evaluate",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)


if __name__ == "__main__":
    unittest.main()
