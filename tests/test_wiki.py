from __future__ import annotations

import pytest

from loop_orchestrator.engine import wiki
from loop_orchestrator.engine.wiki import MARKER, file_decision, render_decision_entry

COMPILED = "# Checkpoint\n\ncompiled state, docs-owned\n"


def test_appends_below_marker_preserving_above(tmp_path):
    page = tmp_path / "checkpoint.md"
    original = COMPILED + MARKER + "\n### [t0] decision d-0 (approved)\n"
    page.write_text(original, encoding="utf-8")

    file_decision(page, "### [t1] decision d-1 (approved)\nnew entry\n")

    content = page.read_text(encoding="utf-8")
    assert content.startswith(original)  # everything above (and prior entries) byte-for-byte
    assert content == original + "### [t1] decision d-1 (approved)\nnew entry\n"


def test_creates_marker_when_absent(tmp_path):
    page = tmp_path / "checkpoint.md"
    page.write_text(COMPILED, encoding="utf-8")
    file_decision(page, "entry one\n")
    assert page.read_text(encoding="utf-8") == COMPILED + MARKER + "\n" + "entry one\n"


def test_creates_file_when_missing(tmp_path):
    page = tmp_path / "checkpoint.md"
    file_decision(page, "entry one")
    assert page.read_text(encoding="utf-8") == MARKER + "\n" + "entry one\n"


def test_mtime_conflict_retries_then_writes(tmp_path, monkeypatch):
    page = tmp_path / "checkpoint.md"
    page.write_text(COMPILED + MARKER + "\n", encoding="utf-8")

    real = wiki._mtime_ns
    fakes = iter([1, 2])  # attempt 1: read sees 1, re-stat sees 2 -> conflict
    calls = []

    def fake_mtime(path):
        calls.append(path)
        try:
            return next(fakes)
        except StopIteration:
            return real(path)

    monkeypatch.setattr(wiki, "_mtime_ns", fake_mtime)
    file_decision(page, "entry\n")

    assert len(calls) == 4  # two stats per attempt, two attempts
    assert page.read_text(encoding="utf-8") == COMPILED + MARKER + "\n" + "entry\n"


def test_mtime_conflict_exhausts_retries(tmp_path, monkeypatch):
    page = tmp_path / "checkpoint.md"
    page.write_text(COMPILED + MARKER + "\n", encoding="utf-8")
    counter = iter(range(100))  # every stat differs -> permanent conflict
    monkeypatch.setattr(wiki, "_mtime_ns", lambda path: next(counter))

    with pytest.raises(RuntimeError, match="3 write attempts"):
        file_decision(page, "entry\n")
    assert page.read_text(encoding="utf-8") == COMPILED + MARKER + "\n"  # untouched


def test_render_decision_entry():
    doc = {
        "id": "d-20260610-120000",
        "status": "approved",
        "created_at": "2026-06-10T12:00:00Z",
        "decided_at": "2026-06-10T12:05:00Z",
        "critique": "web lane stalled on review.",
        "actions": [
            {
                "idx": 0,
                "kind": "dispatch",
                "lane": "web",
                "classification": "safe",
                "status": "executed",
                "rationale": "unblock review",
            },
            {
                "idx": 1,
                "kind": "drop_lane",
                "window": "scratch",
                "classification": "destructive",
                "status": "approved",
                "rationale": "lane finished",
            },
            {
                "idx": 2,
                "kind": "escalate",
                "classification": "safe",
                "status": "executed",
                "rationale": "needs human",
            },
        ],
    }
    entry = render_decision_entry(doc)
    lines = entry.splitlines()
    assert lines[0] == "### [2026-06-10T12:05:00Z] decision d-20260610-120000 (approved)"
    assert "web lane stalled on review." in lines
    assert "0. dispatch web [safe/executed]: unblock review" in lines
    assert "1. drop_lane scratch [destructive/approved]: lane finished" in lines
    assert "2. escalate - [safe/executed]: needs human" in lines
    assert entry.endswith("\n")
