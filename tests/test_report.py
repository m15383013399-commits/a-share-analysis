from __future__ import annotations

import unittest

from market_diary.analyze import AnalysisResult, analyze_payload
from market_diary.models import MarketGateResult, QualityResult
from market_diary.report import (
    render_close_report,
    render_market_status,
    render_markdown,
    render_quality_failure,
)
from market_diary.sample import build_sample_payload


SIX_INDICES = [
    {"code": "000001", "name": "上证指数", "price": 3600.12, "pct": 0.31, "amount": None},
    {"code": "399001", "name": "深证成指", "price": 11200.34, "pct": -0.22, "amount": None},
    {"code": "399006", "name": "创业板指", "price": 2300.56, "pct": 0.45, "amount": None},
    {"code": "000688", "name": "科创50", "price": 1050.78, "pct": 0.18, "amount": None},
    {"code": "000905", "name": "中证500", "price": 6100.90, "pct": -0.09, "amount": None},
    {"code": "000300", "name": "沪深300", "price": 4200.11, "pct": 0.12, "amount": None},
]


def valid_analysis() -> AnalysisResult:
    return AnalysisResult(
        trade_date="2026-08-11",
        source="multi-source",
        status={"warnings": ["主题板块数量不足"]},
        market_temperature={"label": "均衡", "score": 55.0, "components": {}},
        breadth={
            "total": 5200,
            "up": 3000,
            "down": 2000,
            "flat": 200,
            "adv_ratio": 3000 / 5200,
            "avg_pct": 0.35,
            "median_pct": 0.20,
            "turnover": 1_300_000_000_000.0,
            "sh_amount": 600_000_000_000.0,
            "sz_amount": 700_000_000_000.0,
            "limit_up": 45,
            "limit_down_est": 3,
        },
        indices=[dict(row) for row in SIX_INDICES],
        hot_boards=[
            {
                "kind": "行业",
                "name": "有色金属",
                "pct": 2.1,
                "amount": 90_000_000_000.0,
                "up_count": 30,
                "down_count": 5,
                "leader": "紫金矿业",
                "leader_pct": 3.2,
                "score": 25.5,
            }
        ],
        industry_boards=[],
        concept_boards=[],
        limit_ladder=[],
        top_stocks=[],
        news=[],
        inferences=["市场宽度改善。"],
        risks=["放量持续性仍需验证。"],
        watch_signals=["观察成交额是否维持。"],
        sources={
            "indices": {
                "source": "tencent",
                "url": "https://qt.gtimg.cn/",
                "fetched_at": "2026-08-11T15:20:00+08:00",
            }
        },
        optional_warnings=["主题板块数量不足"],
    )


def valid_summary() -> dict:
    return {
        "indices": {
            "sample_count": 24,
            "direction_hit_rate": 0.625,
            "range_hit_rate": 0.50,
            "mean_absolute_error": 0.82,
        },
        "sectors": {
            "sample_count": 0,
            "direction_hit_rate": None,
            "range_hit_rate": None,
            "mean_absolute_error": None,
        },
        "stocks": {
            "sample_count": 30,
            "direction_hit_rate": 0.60,
            "range_hit_rate": 0.40,
            "mean_absolute_error": 1.25,
        },
        "overall": {
            "sample_count": 54,
            "direction_hit_rate": 0.611111,
            "range_hit_rate": 0.444444,
            "mean_absolute_error": 1.058889,
        },
    }


class ReportSafetyTest(unittest.TestCase):
    def test_closed_report_has_next_date_and_no_market_claims(self) -> None:
        text = render_market_status(
            MarketGateResult("closed", "2026-10-02", "2026-10-09", "holiday")
        )

        self.assertIn("2026-10-09", text)
        self.assertIn("holiday", text)
        self.assertIn("closed", text)
        self.assertIn("不构成投资建议", text)
        for forbidden in ("市场温度", "冰点", "热点", "预测", "成交额"):
            self.assertNotIn(forbidden, text)

    def test_not_stable_report_keeps_status_reason_next_date_and_disclaimer(self) -> None:
        text = render_market_status(
            MarketGateResult(
                "market_not_stable",
                "2026-08-11",
                "2026-08-12",
                "before 15:15 Asia/Shanghai",
            )
        )

        for expected in (
            "market_not_stable",
            "before 15:15 Asia/Shanghai",
            "2026-08-12",
            "不构成投资建议",
        ):
            self.assertIn(expected, text)

    def test_failed_quality_report_lists_errors_warnings_and_sources_only(self) -> None:
        snapshot = {
            "trade_date": "2026-08-11",
            "sources": {
                "indices": {
                    "source": "tencent",
                    "url": "https://qt.gtimg.cn/",
                    "fetched_at": "2026-08-11T15:20:00+08:00",
                }
            },
        }
        quality = QualityResult(
            "failed_quality_gate",
            ("stock sample count: 0",),
            ("concept sector source unavailable",),
        )

        text = render_quality_failure(snapshot, quality)

        self.assertIn("failed_quality_gate", text)
        self.assertIn("stock sample count: 0", text)
        self.assertIn("concept sector source unavailable", text)
        self.assertIn("[tencent](https://qt.gtimg.cn/)", text)
        self.assertIn("已停止生成正式收盘结论与累计成绩更新", text)
        for forbidden in ("冰点", "热点", "预测", "市场温度"):
            self.assertNotIn(forbidden, text)

    def test_close_report_labels_facts_inference_proxy_and_disclaimer(self) -> None:
        text = render_close_report(valid_analysis(), evaluation_summary=valid_summary())

        for label in ("事实", "Codex推断", "代理口径", "不构成投资建议"):
            self.assertIn(label, text)
        for index_name in (
            "上证指数",
            "深证成指",
            "创业板指",
            "科创50",
            "中证500",
            "沪深300",
        ):
            self.assertIn(index_name, text)
        self.assertIn("沪市成交额", text)
        self.assertIn("深市成交额", text)
        self.assertIn("两市总成交额", text)
        self.assertIn("[tencent](https://qt.gtimg.cn/)", text)
        self.assertIn("主题板块数量不足", text)
        self.assertIn("62.50%", text)
        self.assertIn("0.82", text)

    def test_known_source_name_is_rendered_as_link_when_snapshot_has_no_url(self) -> None:
        result = valid_analysis()
        result.sources = {
            "indices": {
                "source": "tencent",
                "fetched_at": "2026-08-11T15:20:00+08:00",
            }
        }

        text = render_close_report(result)

        self.assertIn("[tencent](https://qt.gtimg.cn/q=)", text)

    def test_six_index_names_are_canonicalized_by_code(self) -> None:
        result = valid_analysis()
        for position, row in enumerate(result.indices):
            row["name"] = f"伪造指数{position}"

        text = render_close_report(result)

        for _, canonical_name in (
            ("000001", "上证指数"),
            ("399001", "深证成指"),
            ("399006", "创业板指"),
            ("000688", "科创50"),
            ("000905", "中证500"),
            ("000300", "沪深300"),
        ):
            self.assertIn(canonical_name, text)
        self.assertNotIn("伪造指数", text)

    def test_canonical_index_name_cannot_substitute_for_missing_code(self) -> None:
        result = valid_analysis()
        result.indices = [
            row for row in result.indices if row.get("code") != "000001"
        ]
        result.indices.append(
            {
                "code": "999999",
                "name": "上证指数",
                "price": 9999.99,
                "pct": 9.99,
                "amount": 999_999_999.0,
            }
        )

        text = render_close_report(result)

        self.assertIn("| 上证指数 | - | - | - |", text)
        self.assertNotIn("9999.99", text)

    def test_all_markdown_tables_render_missing_values_as_dash_not_zero(self) -> None:
        result = valid_analysis()
        result.market_temperature = {"label": None, "score": None, "components": {"x": None}}
        result.breadth.update(
            {
                "up": None,
                "down": None,
                "flat": None,
                "adv_ratio": None,
                "avg_pct": None,
                "median_pct": None,
                "turnover": None,
                "sh_amount": None,
                "sz_amount": None,
                "limit_up": None,
                "limit_down_est": None,
            }
        )
        result.indices = [
            {"code": "000001", "name": "缺失指数", "price": None, "pct": None, "amount": None}
        ]
        result.hot_boards = [
            {
                "kind": "行业",
                "name": "缺失板块",
                "pct": None,
                "amount": None,
                "up_count": None,
                "down_count": None,
                "leader": None,
                "leader_pct": None,
                "score": None,
            }
        ]
        result.limit_ladder = [{"height": None, "count": None, "names": []}]
        result.top_stocks = [
            {"code": "600000", "name": "缺失个股", "pct": None, "price": None, "amount": None}
        ]

        text = render_close_report(result, evaluation_summary=valid_summary())

        self.assertIn("| 上证指数 | - | - | - |", text)
        self.assertIn("| 1 | 600000 | 缺失个股 | - | - | - |", text)
        self.assertIn("| 板块代理 | 0 | - | - | - |", text)
        self.assertNotIn("上证指数 | 0.00 | 0.00% | 0", text)
        self.assertNotIn("缺失个股 | 0.00% | 0.00 | 0", text)

    def test_render_markdown_remains_close_report_compatibility_wrapper(self) -> None:
        result = valid_analysis()

        self.assertEqual(render_markdown(result), render_close_report(result))

    def test_analysis_preserves_snapshot_exchange_amounts_sources_and_warnings(self) -> None:
        payload = build_sample_payload("2026-08-11")
        payload["breadth"] = {
            "counts": {"up": 8, "down": 3, "flat": 1, "total": 12},
            "sh_amount": 600_000_000_000.0,
            "sz_amount": 700_000_000_000.0,
        }
        payload["sources"] = {
            "indices": {"source": "tencent", "url": "https://qt.gtimg.cn/"}
        }
        payload["warnings"] = ["concept source unavailable"]
        payload.pop("source")
        payload["quality_status"] = "passed"

        result = analyze_payload(payload)

        self.assertEqual(result.breadth["sh_amount"], 600_000_000_000.0)
        self.assertEqual(result.breadth["sz_amount"], 700_000_000_000.0)
        self.assertEqual(result.breadth["turnover"], 1_300_000_000_000.0)
        self.assertEqual(result.source, "multi-source")
        self.assertEqual(result.sources["indices"]["source"], "tencent")
        self.assertIn("concept source unavailable", result.optional_warnings)

    def test_missing_or_none_limit_pool_does_not_infer_limit_activity(self) -> None:
        for mode in ("missing", "none"):
            with self.subTest(mode=mode):
                payload = build_sample_payload("2026-08-11")
                if mode == "missing":
                    payload.pop("limit_up_pool", None)
                else:
                    payload["limit_up_pool"] = None

                result = analyze_payload(payload)

                self.assertIsNone(result.breadth["limit_up"])
                self.assertEqual(result.market_temperature, {})
                self.assertFalse(any("涨停数量" in item for item in result.inferences))
                self.assertFalse(any("涨停池" in item for item in result.watch_signals))

    def test_explicit_empty_limit_pool_preserves_real_zero_behavior(self) -> None:
        payload = build_sample_payload("2026-08-11")
        payload["limit_up_pool"] = []

        result = analyze_payload(payload)

        self.assertEqual(result.breadth["limit_up"], 0)
        self.assertTrue(result.market_temperature)
        self.assertTrue(any("涨停数量不高" in item for item in result.inferences))
        self.assertTrue(any("涨停池" in item for item in result.watch_signals))

    def test_missing_stock_percentages_do_not_create_zero_change_rankings(self) -> None:
        payload = build_sample_payload("2026-08-11")
        for stock in payload["stocks"]:
            stock.pop("涨跌幅", None)

        result = analyze_payload(payload)

        self.assertEqual(result.top_stocks, [])

    def test_analysis_does_not_score_missing_board_heat_as_zero(self) -> None:
        payload = build_sample_payload("2026-08-11")
        payload["industry_boards"] = []
        payload["concept_boards"] = []

        result = analyze_payload(payload)

        self.assertEqual(result.market_temperature, {})


if __name__ == "__main__":
    unittest.main()
