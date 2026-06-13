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


def _entry(i: str, body: str = "") -> str:
    head = f"### [t{i}] decision d-{i} (approved)\n"
    if not body:
        return head
    return head + (body if body.endswith("\n") else body + "\n")


def test_rotation_keeps_last_n_and_archives_overflow(tmp_path):
    import re

    page = tmp_path / "checkpoint.md"
    archive = tmp_path / "decisions-archive.md"
    page.write_text(COMPILED + MARKER + "\n", encoding="utf-8")
    for i in range(5):
        file_decision(page, _entry(str(i), f"body {i}"), keep=3)
    content = page.read_text(encoding="utf-8")
    assert re.findall(r"decision (d-\d)", content) == ["d-2", "d-3", "d-4"]  # last 3
    assert content.startswith(COMPILED + MARKER)  # above + marker byte-for-byte
    arch = archive.read_text(encoding="utf-8")
    assert "d-0" in arch and "d-1" in arch  # overflow archived
    assert "d-2" not in arch  # kept entries are not also archived


def test_rotation_no_op_below_n_writes_no_archive(tmp_path):
    page = tmp_path / "checkpoint.md"
    page.write_text(COMPILED + MARKER + "\n", encoding="utf-8")
    for i in range(3):
        file_decision(page, _entry(str(i)), keep=10)
    assert not (tmp_path / "decisions-archive.md").exists()
    assert all(f"d-{i}" in page.read_text(encoding="utf-8") for i in range(3))


def test_rotation_preserves_marker_and_preamble(tmp_path):
    page = tmp_path / "checkpoint.md"
    page.write_text(COMPILED + MARKER + "\n## Decision needed\n(none)\n", encoding="utf-8")
    for i in range(4):
        file_decision(page, _entry(str(i)), keep=2)
    content = page.read_text(encoding="utf-8")
    assert MARKER in content
    assert "## Decision needed\n(none)" in content  # preamble preserved...
    # ...and never rotated into the archive
    assert "(none)" not in (tmp_path / "decisions-archive.md").read_text(encoding="utf-8")


def test_rotation_steady_state_archives_one_at_a_time(tmp_path):
    import re

    page = tmp_path / "checkpoint.md"
    page.write_text(COMPILED + MARKER + "\n", encoding="utf-8")
    for i in range(3):
        file_decision(page, _entry(str(i)), keep=3)
    assert not (tmp_path / "decisions-archive.md").exists()  # exactly N: nothing yet
    file_decision(page, _entry("3"), keep=3)  # N+1 -> rotate one
    assert re.findall(r"decision (d-\d)", page.read_text(encoding="utf-8")) == ["d-1", "d-2", "d-3"]


def test_rotation_never_splits_a_partial_entry(tmp_path):
    page = tmp_path / "checkpoint.md"
    page.write_text(COMPILED + MARKER + "\n", encoding="utf-8")
    file_decision(page, "### [t0] decision d-0 (approved)\nline a\nline b\nline c\n", keep=1)
    file_decision(page, "### [t1] decision d-1 (approved)\nonly\n", keep=1)
    archive = (tmp_path / "decisions-archive.md").read_text(encoding="utf-8")
    # the whole multi-line d-0 block rotated out intact, not truncated.
    assert "### [t0] decision d-0 (approved)\nline a\nline b\nline c\n" in archive
    content = page.read_text(encoding="utf-8")
    assert "d-1" in content and "d-0" not in content


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
