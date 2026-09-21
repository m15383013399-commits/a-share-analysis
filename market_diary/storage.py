from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def upsert_jsonl(
    path: str | Path,
    record: dict[str, Any],
    *,
    key_fields: tuple[str, ...],
) -> None:
    target = Path(path)
    rows = []
    if target.exists():
        rows = [
            json.loads(line)
            for line in target.read_text("utf-8").splitlines()
            if line.strip()
        ]
    wanted = tuple(record.get(field) for field in key_fields)
    rows = [
        row
        for row in rows
        if tuple(row.get(field) for field in key_fields) != wanted
    ]
    rows.append(record)
    rows.sort(key=lambda row: tuple(str(row.get(field, "")) for field in key_fields))
    atomic_write_text(
        target,
        "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
            for row in rows
        ),
    )
