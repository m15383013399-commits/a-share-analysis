#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_diary.analyze import analyze_payload
from market_diary.calendar import evaluate_market_gate, load_calendar
from market_diary.evaluation import evaluate_forecast, persist_evaluation
from market_diary.forecast_ledger import validate_forecast
from market_diary.market_data import MarketDataClient
from market_diary.models import MarketGateResult, QualityResult
from market_diary.quality import validate_snapshot
from market_diary.report import (
    render_close_report,
    render_market_status,
    render_quality_failure,
)
from market_diary.sample import build_sample_payload
from market_diary.storage import atomic_write_json, atomic_write_text
from market_diary.utils import load_json_file, normalize_date, safe_filename


EXIT_SUCCESS = 0
EXIT_FETCH_OR_CONFIG = 2
EXIT_MARKET_UNAVAILABLE = 3
EXIT_QUALITY = 4
EXIT_FORECAST_OR_EVALUATION = 5
SNAPSHOT_KEYS = (
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
)


class ForecastHistoryError(ValueError):
    """Raised when the persisted forecast ledger cannot be trusted."""

DEFAULT_CONFIG: dict[str, Any] = {
    "report_name": "A股市场日报",
    "timezone": "Asia/Shanghai",
    "output_dir": "reports",
    "raw_data_dir": "data/raw",
    "snapshot_dir": "data/snapshots",
    "forecast_dir": "data/forecasts",
    "evaluation_detail_path": "data/evaluations/evaluations.jsonl",
    "evaluation_summary_path": "data/evaluations/summary.json",
    "calendar_path": "config/trading_calendar_2026.json",
    "min_stock_count": 5000,
    "flat_threshold": 0.30,
    "watch_codes": [],
    "board_top_n": 10,
    "stock_top_n": 20,
    "news_top_n": 12,
    "include_news": True,
    "news_keywords": [],
}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and audit an A-share daily market diary."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--snapshot", action="store_true", help="Collect and validate a close snapshot.")
    mode.add_argument("--evaluate", action="store_true", help="Evaluate the forecast for a saved snapshot.")
    mode.add_argument("--forecast-file", help="Validate and register a forecast JSON file.")
    mode.add_argument("--full-close-run", action="store_true", help="Run close gate, snapshot, evaluation and report.")
    parser.add_argument(
        "--allow-preclose",
        action="store_true",
        help="Allow an unofficial debug snapshot before 15:15; evaluation remains forbidden.",
    )
    parser.add_argument("--date", help="Trade date, YYYY-MM-DD or YYYYMMDD. Default: today in Asia/Shanghai.")
    parser.add_argument("--config", help="Path to JSON config. Default: config.example.json if present.")
    parser.add_argument("--output", help="Output markdown path. Default: reports/a_share_daily_YYYY-MM-DD.md.")
    parser.add_argument("--sample", action="store_true", help="Use built-in sample data; no network or dependencies needed.")
    parser.add_argument("--no-news", action="store_true", help="Skip news fetching.")
    parser.add_argument("--save-raw", action="store_true", help="Save fetched raw payload JSON under data/raw.")
    args = parser.parse_args(argv)
    formal_mode = bool(
        args.snapshot or args.evaluate or args.forecast_file or args.full_close_run
    )
    if args.sample and formal_mode:
        parser.error("--sample cannot be combined with a formal mode")
    if args.allow_preclose and (args.sample or args.evaluate or args.forecast_file):
        parser.error(
            "--allow-preclose is only valid for snapshot, full-close, or legacy live collection"
        )
    return args


def _error(message: Any) -> None:
    print(f"[error] {message}", file=sys.stderr)


def _config_path(config: Mapping[str, Any], key: str, default: str) -> Path:
    raw = config.get(key, default)
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        raise ValueError(f"config.{key} must be a nonempty path")
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _output_path(args: argparse.Namespace, config: Mapping[str, Any], trade_date: str) -> Path:
    if args.output:
        return Path(args.output)
    return _config_path(config, "output_dir", "reports") / f"a_share_daily_{safe_filename(trade_date)}.md"


def _snapshot_path(config: Mapping[str, Any], trade_date: str) -> Path:
    return _config_path(config, "snapshot_dir", "data/snapshots") / f"{safe_filename(trade_date)}.json"


def _write_report(
    args: argparse.Namespace,
    config: Mapping[str, Any],
    trade_date: str,
    markdown: str,
) -> Path:
    path = _output_path(args, config, trade_date)
    atomic_write_text(path, markdown)
    print(path)
    return path


def load_config(path: str | None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    candidate = Path(path) if path else ROOT / "config.example.json"
    if path and not candidate.exists():
        raise FileNotFoundError(f"config file does not exist: {candidate}")
    if candidate.exists():
        loaded = load_json_file(candidate)
        if not isinstance(loaded, Mapping):
            raise ValueError("config must be a JSON object")
        config.update(loaded)
    return config


def _trade_date(args: argparse.Namespace, config: Mapping[str, Any]) -> str:
    return normalize_date(args.date, str(config.get("timezone", "Asia/Shanghai")))


def _now(config: Mapping[str, Any]) -> datetime:
    timezone_name = str(config.get("timezone", "Asia/Shanghai"))
    injected = config.get("now")
    if injected is None:
        return datetime.now(ZoneInfo(timezone_name))
    if not isinstance(injected, str):
        raise ValueError("config.now must be an ISO-8601 timestamp when supplied")
    parsed = datetime.fromisoformat(injected)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("config.now must include a timezone offset")
    return parsed


def build_market_data_client(config: Mapping[str, Any]) -> MarketDataClient:
    import requests

    market_data = config.get("market_data", {})
    if not isinstance(market_data, Mapping):
        raise ValueError("config.market_data must be a JSON object")
    timeout = float(market_data.get("timeout_seconds", 8))
    client_kwargs: dict[str, Any] = {}
    for key in (
        "index_sources",
        "breadth_sources",
        "sector_sources",
        "required_index_codes",
    ):
        if key in market_data:
            client_kwargs[key] = market_data[key]
    return MarketDataClient(
        get=requests.get,
        min_breadth_count=int(config.get("min_stock_count", 5000)),
        now=lambda: _now(config),
        timeout_seconds=timeout,
        **client_kwargs,
    )


def _calendar(config: Mapping[str, Any]) -> dict[str, Any]:
    return load_calendar(_config_path(config, "calendar_path", "config/trading_calendar_2026.json"))


def _base_gate(config: Mapping[str, Any], trade_date: str) -> MarketGateResult:
    return evaluate_market_gate(
        now=_now(config),
        target_date=date.fromisoformat(trade_date),
        calendar=_calendar(config),
    )


def _close_samples(client: Any, trade_date: str, code: str) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    # Two independent reads are intentional. There is no sleep: either a supplier's
    # >=15:10 quote timestamp or identical closes for this same index proves stability.
    for _ in range(2):
        result = client.fetch_close_stability_quotes([code], trade_date)
        if str(getattr(result, "source", "")).lower() != "tencent":
            continue
        rows = getattr(result, "rows", ())
        matching = [
            dict(row)
            for row in rows
            if (
                isinstance(row, Mapping)
                and str(row.get("source") or "").lower() == "tencent"
                and str(row.get("code") or "") == code
            )
        ]
        if matching:
            samples.append(matching[-1])
    return samples


def _stable_gate(
    config: Mapping[str, Any], trade_date: str, client: Any
) -> MarketGateResult:
    code = str(config.get("stability_index_code", "000001"))
    samples = _close_samples(client, trade_date, code)
    return evaluate_market_gate(
        now=_now(config),
        target_date=date.fromisoformat(trade_date),
        calendar=_calendar(config),
        close_samples=samples,
    )


def _validate_forecast_chain(
    entries: Sequence[tuple[date, Path, Mapping[str, Any]]],
) -> list[tuple[date, Path, dict[str, Any]]]:
    ordered = sorted(entries, key=lambda entry: entry[0])
    seen: dict[date, Path] = {}
    for trade_date, path, _ in ordered:
        if trade_date in seen:
            raise ForecastHistoryError(
                f"duplicate forecast trade_date {trade_date}: {seen[trade_date]}, {path}"
            )
        seen[trade_date] = path
        expected_name = f"{trade_date.isoformat()}.json"
        if path.name != expected_name:
            raise ForecastHistoryError(
                f"forecast filename {path.name} does not match trade_date {trade_date}"
            )

    validated: list[tuple[date, Path, dict[str, Any]]] = []
    previous: dict[str, Any] | None = None
    for trade_date, path, document in ordered:
        try:
            normalized = validate_forecast(document, previous=previous)
        except Exception as exc:
            raise ForecastHistoryError(f"invalid forecast history file {path}: {exc}") from exc
        validated.append((trade_date, path, normalized))
        previous = normalized
    return validated


def _raw_forecast_entries(
    directory: Path, *, skip_path: Path | None = None
) -> list[tuple[date, Path, Mapping[str, Any]]]:
    if not directory.exists():
        return []
    entries: list[tuple[date, Path, Mapping[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        if path == skip_path:
            continue
        try:
            document = load_json_file(path)
            if not isinstance(document, Mapping):
                raise ValueError("document must be an object")
            trade_date_text = str(document.get("trade_date") or "")
            trade_date = date.fromisoformat(trade_date_text)
            if trade_date.isoformat() != trade_date_text:
                raise ValueError("trade_date must use YYYY-MM-DD format")
        except Exception as exc:
            raise ForecastHistoryError(f"invalid forecast history file {path}: {exc}") from exc
        entries.append((trade_date, path, document))
    return entries


def _forecast_entries(config: Mapping[str, Any]) -> list[tuple[date, Path, dict[str, Any]]]:
    directory = _config_path(config, "forecast_dir", "data/forecasts")
    return _validate_forecast_chain(_raw_forecast_entries(directory))


def _forecast_for_actual_date(
    entries: Sequence[tuple[date, Path, dict[str, Any]]], actual_date: str
) -> tuple[date, Path, dict[str, Any]] | None:
    matches = [entry for entry in entries if entry[2].get("next_trade_date") == actual_date]
    if len(matches) > 1:
        paths = ", ".join(str(entry[1]) for entry in matches)
        raise ForecastHistoryError(f"multiple forecasts target {actual_date}: {paths}")
    return matches[0] if matches else None


def _previous_forecast(
    entries: Sequence[tuple[date, Path, dict[str, Any]]], current_date: date
) -> dict[str, Any] | None:
    earlier = [entry for entry in entries if entry[0] < current_date]
    return max(earlier, key=lambda entry: entry[0])[2] if earlier else None


def _watch_codes(config: Mapping[str, Any], trade_date: str) -> list[str]:
    target = _forecast_for_actual_date(_forecast_entries(config), trade_date)
    if target is not None:
        rows = target[2].get("watch_pool")
        if isinstance(rows, list):
            return [
                str(row.get("code") or "").strip()
                for row in rows
                if isinstance(row, Mapping)
            ]
    configured = config.get("watch_codes")
    if isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)):
        return [str(code).strip() for code in configured]
    return []


def _quality(config: Mapping[str, Any], snapshot: Mapping[str, Any], watch_codes: Sequence[str]) -> QualityResult:
    market_data = config.get("market_data", {})
    if not isinstance(market_data, Mapping):
        raise ValueError("config.market_data must be a JSON object")
    quality_kwargs: dict[str, Any] = {
        "watch_codes": watch_codes,
        "min_stock_count": int(config.get("min_stock_count", 5000)),
        "index_pct_tolerance": config.get("index_pct_tolerance", 0.05),
        "index_level_tolerance_pct": config.get(
            "index_level_tolerance_pct", 0.05
        ),
    }
    if "required_index_codes" in market_data:
        quality_kwargs["required_index_codes"] = market_data["required_index_codes"]
    return validate_snapshot(snapshot, **quality_kwargs)


def _save_snapshot_and_raw(
    args: argparse.Namespace,
    config: Mapping[str, Any],
    trade_date: str,
    snapshot: Mapping[str, Any],
) -> Path:
    path = _snapshot_path(config, trade_date)
    atomic_write_json(path, snapshot)
    if args.save_raw:
        raw = _config_path(config, "raw_data_dir", "data/raw") / f"payload_{safe_filename(trade_date)}.json"
        atomic_write_json(raw, snapshot)
    print(path)
    return path


def _status_exit(
    args: argparse.Namespace,
    config: Mapping[str, Any],
    gate: MarketGateResult,
) -> int:
    _write_report(
        args,
        config,
        gate.trade_date,
        render_market_status(gate, report_name=str(config.get("report_name", "A股市场日报"))),
    )
    return EXIT_MARKET_UNAVAILABLE


def _collect_snapshot(
    args: argparse.Namespace,
    config: Mapping[str, Any],
) -> tuple[int, dict[str, Any] | None, QualityResult | None]:
    trade_date = _trade_date(args, config)
    gate = _base_gate(config, trade_date)
    if gate.status == "closed":
        return _status_exit(args, config, gate), None, None
    preclose = gate.status == "market_not_stable" and gate.reason.startswith("before 15:15")
    if preclose and not args.allow_preclose:
        return _status_exit(args, config, gate), None, None

    try:
        client = build_market_data_client(config)
        if not preclose:
            gate = _stable_gate(config, trade_date, client)
            if gate.status != "ready":
                return _status_exit(args, config, gate), None, None
        watch_codes = _watch_codes(config, trade_date)
        snapshot = client.build_snapshot(trade_date, watch_codes)
        if not isinstance(snapshot, Mapping):
            raise ValueError("market data client returned a non-object snapshot")
        snapshot = dict(snapshot)
    except ForecastHistoryError as exc:
        _error(exc)
        return EXIT_FORECAST_OR_EVALUATION, None, None
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports provider/config failures
        _error(exc)
        return EXIT_FETCH_OR_CONFIG, None, None

    extra_keys = sorted(set(snapshot) - set(SNAPSHOT_KEYS))
    snapshot = {key: snapshot.get(key) for key in SNAPSHOT_KEYS}
    if extra_keys:
        warnings = snapshot.get("warnings")
        warnings = list(warnings) if isinstance(warnings, list) else []
        warnings.append(
            "snapshot: discarded non-contract top-level fields: "
            + ", ".join(extra_keys)
        )
        snapshot["warnings"] = warnings
    snapshot["market_status"] = "unofficial_preclose" if preclose else "ready"
    quality = _quality(config, snapshot, watch_codes)
    snapshot["quality_status"] = quality.status
    if quality.optional_warnings:
        warnings = snapshot.get("warnings")
        warnings = list(warnings) if isinstance(warnings, list) else []
        snapshot["warnings"] = list(dict.fromkeys([*warnings, *quality.optional_warnings]))
    _save_snapshot_and_raw(args, config, trade_date, snapshot)

    if quality.status != "passed":
        _write_report(
            args,
            config,
            trade_date,
            render_quality_failure(
                snapshot,
                quality,
                report_name=str(config.get("report_name", "A股市场日报")),
            ),
        )
        return EXIT_QUALITY, snapshot, quality
    return EXIT_SUCCESS, snapshot, quality


def run_snapshot(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    code, _, _ = _collect_snapshot(args, config)
    return code


def _evaluate_saved_snapshot(
    trade_date: str,
    snapshot: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    missing_ok: bool,
) -> tuple[int, Mapping[str, Any] | None, bool]:
    entries = _forecast_entries(config)
    selected = _forecast_for_actual_date(entries, trade_date)
    if selected is None:
        if not missing_ok:
            _error(f"no forecast targets {trade_date}")
            return EXIT_FORECAST_OR_EVALUATION, None, False
        return EXIT_SUCCESS, None, False

    forecast_date, _, forecast = selected
    previous = _previous_forecast(entries, forecast_date)
    evaluation_kwargs: dict[str, Any] = {
        "flat_threshold": float(config.get("flat_threshold", 0.30))
    }
    if previous is None:
        # This authorization is based only on a confirmed directory scan. It is
        # never inferred from the current forecast's changes declaration.
        evaluation_kwargs["allow_initial_without_previous"] = True
    else:
        evaluation_kwargs["previous_forecast"] = previous
    records = evaluate_forecast(forecast, snapshot, **evaluation_kwargs)
    summary = persist_evaluation(
        records,
        detail_path=_config_path(
            config, "evaluation_detail_path", "data/evaluations/evaluations.jsonl"
        ),
        summary_path=_config_path(
            config, "evaluation_summary_path", "data/evaluations/summary.json"
        ),
    )
    return EXIT_SUCCESS, summary, True


def _snapshot_is_evaluable(snapshot: Mapping[str, Any]) -> bool:
    market_status = snapshot.get("market_status")
    quality_status = snapshot.get("quality_status")
    if market_status == "unofficial_preclose":
        return False
    return (market_status, quality_status) in {
        ("ready", "passed"),
        (
            "historical_migration",
            "historical_transcription_not_live_gate_validated",
        ),
    }


def run_evaluation(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    if args.allow_preclose:
        _error("--allow-preclose cannot be used to run an evaluation")
        return EXIT_FORECAST_OR_EVALUATION
    try:
        trade_date = _trade_date(args, config)
        path = _snapshot_path(config, trade_date)
        if not path.exists():
            _error(f"snapshot does not exist: {path}")
            return EXIT_FETCH_OR_CONFIG
        snapshot = load_json_file(path)
        if snapshot.get("market_status") == "unofficial_preclose":
            _error("unofficial preclose snapshots cannot be evaluated")
            return EXIT_MARKET_UNAVAILABLE
        if not _snapshot_is_evaluable(snapshot):
            _error("snapshot did not pass the quality gate")
            return EXIT_QUALITY
        code, _, _ = _evaluate_saved_snapshot(
            trade_date, snapshot, config, missing_ok=False
        )
        if code == EXIT_SUCCESS:
            print(_config_path(config, "evaluation_summary_path", "data/evaluations/summary.json"))
        return code
    except Exception as exc:  # noqa: BLE001 - forecast/evaluation validation boundary
        _error(exc)
        return EXIT_FORECAST_OR_EVALUATION


def register_forecast(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    import fcntl
    directory = _config_path(config, "forecast_dir", "data/forecasts")
    directory.parent.mkdir(parents=True, exist_ok=True)
    with (directory.parent / ("." + directory.name + ".registration.lock")).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return _register_forecast_locked(args, config)


def _register_forecast_locked(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    try:
        if not args.forecast_file:
            raise ValueError("--forecast-file is required")
        document = load_json_file(args.forecast_file)
        current_date = date.fromisoformat(str(document.get("trade_date") or ""))
        directory = _config_path(config, "forecast_dir", "data/forecasts")
        path = directory / f"{current_date.isoformat()}.json"
        candidate_entries = _raw_forecast_entries(directory, skip_path=path)
        candidate_entries.append((current_date, path, document))
        validated_entries = _validate_forecast_chain(candidate_entries)
        normalized = next(
            entry[2] for entry in validated_entries if entry[0] == current_date
        )
        # New platform publications are strict; legacy records remain readable.
        from backend.contracts import validate_new_forecast
        previous = _previous_forecast(validated_entries, current_date)
        if document.get("schema_version") == "2.0":
            normalized = validate_new_forecast(document, previous=previous)
        if path.exists():
            if load_json_file(path) != normalized:
                raise ValueError("同日正式预测已存在，禁止覆盖；请保存为研究草稿")
        else:
            atomic_write_json(path, normalized)
        print(path)
        return EXIT_SUCCESS
    except Exception as exc:  # noqa: BLE001 - validation failures use deterministic exit 5
        _error(exc)
        return EXIT_FORECAST_OR_EVALUATION


def _close_markdown(
    snapshot: Mapping[str, Any],
    config: Mapping[str, Any],
    summary: Mapping[str, Any] | None,
    *,
    review_note: str | None = None,
) -> str:
    analysis = analyze_payload(dict(snapshot), dict(config))
    markdown = render_close_report(
        analysis,
        evaluation_summary=summary,
        report_name=str(config.get("report_name", "A股市场日报")),
    )
    if review_note:
        markdown += f"\n> 复盘状态：{review_note}\n"
    return markdown


def run_full_close(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    code, snapshot, _ = _collect_snapshot(args, config)
    if code != EXIT_SUCCESS or snapshot is None:
        return code
    trade_date = str(snapshot["trade_date"])
    if snapshot.get("market_status") == "unofficial_preclose":
        # Debug snapshots are deliberately terminal and can never reach evaluation.
        return EXIT_SUCCESS
    try:
        evaluation_code, summary, found = _evaluate_saved_snapshot(
            trade_date, snapshot, config, missing_ok=True
        )
        note = None if found else "无可复盘预测"
        markdown = _close_markdown(snapshot, config, summary, review_note=note)
        _write_report(args, config, trade_date, markdown)
        return evaluation_code
    except Exception as exc:  # noqa: BLE001 - factual report remains available on audit failure
        _error(exc)
        markdown = _close_markdown(snapshot, config, None, review_note=f"复盘失败：{exc}")
        _write_report(args, config, trade_date, markdown)
        return EXIT_FORECAST_OR_EVALUATION


def _run_sample(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    trade_date = _trade_date(args, config)
    payload = build_sample_payload(trade_date)
    if args.save_raw:
        raw = _config_path(config, "raw_data_dir", "data/raw") / f"payload_{safe_filename(trade_date)}.json"
        atomic_write_json(raw, payload)
    analysis = analyze_payload(payload, dict(config))
    markdown = render_close_report(
        analysis,
        report_name=str(config.get("report_name", "A股市场日报")),
    )
    markdown += "\n> 样例警告：内置样例数据仅用于验证格式和逻辑，不是正式收盘数据。\n"
    _write_report(args, config, trade_date, markdown)
    return EXIT_SUCCESS


def _run_legacy_live(args: argparse.Namespace, config: Mapping[str, Any]) -> int:
    code, snapshot, _ = _collect_snapshot(args, config)
    if code != EXIT_SUCCESS or snapshot is None:
        return code
    if snapshot.get("market_status") == "unofficial_preclose":
        return EXIT_SUCCESS
    trade_date = str(snapshot["trade_date"])
    _write_report(args, config, trade_date, _close_markdown(snapshot, config, None))
    return EXIT_SUCCESS


def main(argv: Sequence[str] | None = None) -> int:
    import os
    if os.environ.get("ASHARE_HTTP_AUDIT"):
        from backend.evidence import install_http_audit
        install_http_audit(os.environ["ASHARE_HTTP_AUDIT"])
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        # Resolve date/config errors before dispatch so every mode uses exit 2.
        if not args.forecast_file:
            _trade_date(args, config)
    except Exception as exc:  # noqa: BLE001 - deterministic config/fetch failure boundary
        _error(exc)
        return EXIT_FETCH_OR_CONFIG

    try:
        if args.forecast_file:
            return register_forecast(args, config)
        if args.snapshot:
            return run_snapshot(args, config)
        if args.evaluate:
            return run_evaluation(args, config)
        if args.full_close_run:
            return run_full_close(args, config)
        if args.sample:
            return _run_sample(args, config)
        return _run_legacy_live(args, config)
    except Exception as exc:  # noqa: BLE001 - final CLI fetch/config safety boundary
        _error(exc)
        return EXIT_FETCH_OR_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
