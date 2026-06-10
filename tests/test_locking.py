"""Locking primitives: atomic visibility + mutual exclusion under contention."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from loop_orchestrator.locking import atomic_write_json, file_lock, read_json


def _tmp_leftovers(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.suffix == ".tmp"]


def test_atomic_write_json_clean_success(tmp_path):
    target = tmp_path / "doc.json"
    atomic_write_json(target, {"a": 1})
    content = target.read_text(encoding="utf-8")
    assert json.loads(content) == {"a": 1}
    assert content.endswith("\n")
    assert _tmp_leftovers(tmp_path) == []


def test_read_json_default_on_missing(tmp_path):
    assert read_json(tmp_path / "absent.json", default={"d": True}) == {"d": True}


def test_concurrent_read_modify_write(tmp_path):
    """5 threads x 10 locked increments => exactly 50; readers never see a torn
    file because atomic_write_json replaces, never truncates in place."""
    target = tmp_path / "counter.json"
    lock = tmp_path / ".lock"
    atomic_write_json(target, {"count": 0})
    failures: list[Exception] = []

    def writer():
        try:
            for _ in range(10):
                with file_lock(lock):
                    doc = json.loads(target.read_text(encoding="utf-8"))
                    doc["count"] += 1
                    atomic_write_json(target, doc)
        except Exception as exc:  # surfaced via the failures list
            failures.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(5)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        json.loads(target.read_text(encoding="utf-8"))  # must always parse
        time.sleep(0.001)
    for t in threads:
        t.join()
    assert failures == []
    assert json.loads(target.read_text(encoding="utf-8")) == {"count": 50}
    assert _tmp_leftovers(tmp_path) == []
