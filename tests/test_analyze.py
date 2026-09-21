from __future__ import annotations

import unittest

from market_diary.analyze import analyze_payload
from market_diary.report import render_markdown
from market_diary.sample import build_sample_payload


class AnalyzePayloadTest(unittest.TestCase):
    def test_sample_payload_generates_hot_boards_and_risks(self) -> None:
        payload = build_sample_payload("2026-06-05")
        result = analyze_payload(payload, {"board_top_n": 5, "stock_top_n": 5})

        self.assertEqual(result.trade_date, "2026-06-05")
        self.assertEqual(result.source, "sample")
        self.assertGreater(result.market_temperature["score"], 0)
        self.assertTrue(result.hot_boards)
        self.assertTrue(result.limit_ladder)
        self.assertTrue(any("样例数据" in item for item in result.risks))

    def test_markdown_contains_core_sections(self) -> None:
        payload = build_sample_payload("2026-06-05")
        result = analyze_payload(payload, {"board_top_n": 5, "stock_top_n": 5})
        markdown = render_markdown(result)

        self.assertIn("# A股市场日报 - 2026-06-05", markdown)
        self.assertIn("## 市场温度", markdown)
        self.assertIn("## 主线强度榜", markdown)
        self.assertIn("## 短期推演", markdown)


if __name__ == "__main__":
    unittest.main()
