from __future__ import annotations

import time
import unittest

from market_diary.providers import AkshareProvider, FetchLog


class ProviderTimeoutTest(unittest.TestCase):
    def test_call_times_out_and_records_warning(self) -> None:
        provider = AkshareProvider.__new__(AkshareProvider)
        provider.log = FetchLog()
        provider.timeout_seconds = 1

        def slow_endpoint() -> list[dict[str, int]]:
            time.sleep(2)
            return [{"value": 1}]

        started = time.monotonic()
        rows = provider._call("slow_endpoint", slow_endpoint)
        elapsed = time.monotonic() - started

        self.assertEqual(rows, [])
        self.assertLess(elapsed, 1.8)
        self.assertTrue(provider.log.warnings)
        self.assertIn("slow_endpoint", provider.log.warnings[0])


if __name__ == "__main__":
    unittest.main()
