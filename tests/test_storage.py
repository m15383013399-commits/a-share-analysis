from __future__ import annotations

import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from market_diary.storage import atomic_write_json, upsert_jsonl


class StorageTest(unittest.TestCase):
    def test_atomic_write_json_replaces_complete_document(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            atomic_write_json(path, {"value": 1})
            atomic_write_json(path, {"value": 2})
            self.assertEqual(json.loads(path.read_text("utf-8")), {"value": 2})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_upsert_jsonl_replaces_same_business_key(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "evaluations.jsonl"
            upsert_jsonl(
                path,
                {"date": "2026-08-11", "code": "399006", "v": 1},
                key_fields=("date", "code"),
            )
            upsert_jsonl(
                path,
                {"date": "2026-08-11", "code": "399006", "v": 2},
                key_fields=("date", "code"),
            )
            rows = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
            self.assertEqual(rows, [{"date": "2026-08-11", "code": "399006", "v": 2}])

    def test_atomic_write_json_rejects_nonfinite_and_unsupported_values_atomically(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            atomic_write_json(path, {"value": 1})
            original = path.read_bytes()

            for invalid in (math.nan, math.inf, -math.inf, object()):
                with self.subTest(invalid=invalid), self.assertRaises(
                    (TypeError, ValueError)
                ):
                    atomic_write_json(path, {"value": invalid})
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_upsert_jsonl_rejects_nonstandard_json_without_changing_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "evaluations.jsonl"
            upsert_jsonl(
                path,
                {"date": "2026-08-11", "code": "399006", "v": 1},
                key_fields=("date", "code"),
            )
            original = path.read_bytes()

            for invalid in (math.nan, math.inf, -math.inf, object()):
                with self.subTest(invalid=invalid), self.assertRaises(
                    (TypeError, ValueError)
                ):
                    upsert_jsonl(
                        path,
                        {"date": "2026-08-11", "code": "399006", "v": invalid},
                        key_fields=("date", "code"),
                    )
                self.assertEqual(path.read_bytes(), original)
