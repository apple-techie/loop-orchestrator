"""Ledger loops-registry sync (T0035/B5): the ledger `loops` registry projects
the task `loop:` fields so loop-digest / the deck show every active loop.

Pure + file-based (no tmux, no engine cycle): derivation from task frontmatter,
the non-destructive merge (F5/T0024 — hand-authored branch/name preserved), and
sync_loops_registry's idempotent, additive ledger write + backfill.
"""

from __future__ import annotations

from pathlib import Path

from loop_orchestrator.engine.events import EventLog
from loop_orchestrator.engine.observe import (
    derive_loops_from_tasks,
    merge_loops_registry,
    sync_loops_registry,
)
from loop_orchestrator.locking import atomic_write_json, read_json
from loop_orchestrator.paths import SessionPaths


def _task(tasks_dir: Path, tid: str, loop: str | None, status: str) -> None:
    directory = tasks_dir if status in ("open", "in-progress") else tasks_dir / "archive"
    directory.mkdir(parents=True, exist_ok=True)
    loop_line = f"loop: {loop}\n" if loop else ""
    (directory / f"{tid}-x.md").write_text(
        f"---\nid: {tid}\ntitle: t\nstatus: {status}\ndepends_on: []\n{loop_line}scope: s\n---\n\n"
        "## Objective\no\n",
        encoding="utf-8",
    )


# ── derivation ───────────────────────────────────────────────────────────────


def test_derive_status_in_progress_if_any_open(tmp_path):
    _task(tmp_path, "T1", "alpha", "open")
    _task(tmp_path, "T2", "alpha", "done")  # one open + one done => in-progress
    _task(tmp_path, "T3", "beta", "done")  # all done => done
    derived = derive_loops_from_tasks(tmp_path, tmp_path / "loops")
    assert derived["alpha"]["status"] == "in-progress"
    assert derived["beta"]["status"] == "done"
    assert derived["alpha"]["name"] == "alpha"  # no doc => the loop id


def test_derive_name_from_ops_wiki_loop_doc(tmp_path):
    _task(tmp_path, "T1", "operating-model", "open")
    loops_dir = tmp_path / "loops"
    loops_dir.mkdir()
    (loops_dir / "operating-model.md").write_text(
        "# loop: operating-model\n\nbody\n", encoding="utf-8"
    )
    derived = derive_loops_from_tasks(tmp_path, loops_dir)
    assert derived["operating-model"]["name"] == "operating-model"  # 'loop:' tag stripped


def test_derive_ignores_tasks_without_a_loop_field(tmp_path):
    _task(tmp_path, "T9", None, "open")
    assert derive_loops_from_tasks(tmp_path, tmp_path / "loops") == {}


# ── non-destructive merge ────────────────────────────────────────────────────


def test_merge_refreshes_status_preserves_handauthored_fields():
    existing = {
        "alpha": {
            "status": "done",
            "branch": "loop/govern/alpha",
            "name": "Alpha Loop",
            "blast_radius": "high",
        }
    }
    derived = {
        "alpha": {"status": "in-progress", "name": "alpha"},
        "beta": {"status": "done", "name": "beta"},
    }
    merged = merge_loops_registry(existing, derived)
    assert merged["alpha"]["status"] == "in-progress"  # derived status is canonical
    assert merged["alpha"]["branch"] == "loop/govern/alpha"  # preserved
    assert merged["alpha"]["name"] == "Alpha Loop"  # hand-authored name preserved
    assert merged["alpha"]["blast_radius"] == "high"  # preserved
    assert merged["beta"] == {"status": "done", "name": "beta"}  # new loop added


def test_merge_keeps_loops_with_no_derived_tasks():
    existing = {"ghost": {"status": "done", "name": "Ghost"}}
    merged = merge_loops_registry(existing, {"alpha": {"status": "done", "name": "alpha"}})
    assert "ghost" in merged  # not deleted — non-destructive


# ── sync_loops_registry (ledger write) ───────────────────────────────────────


def test_sync_creates_ledger_and_backfills(tmp_path):
    paths = SessionPaths(tmp_path, "govern")
    paths.ensure()
    _task(paths.tasks_dir, "T10", "harness-governance", "done")
    _task(paths.tasks_dir, "T31", "operating-model", "done")
    _task(paths.tasks_dir, "T35", "operating-model", "open")
    events = EventLog(paths.events_path)

    count = sync_loops_registry(paths, events)

    assert count == 2
    loops = read_json(paths.state_file, {})["loops"]
    assert loops["harness-governance"]["status"] == "done"
    assert loops["operating-model"]["status"] == "in-progress"
    assert any(e["event"] == "loops-sync" for e in events.tail(5))


def test_sync_backfills_leo_like_loops(tmp_path):
    paths = SessionPaths(tmp_path, "leo")
    paths.ensure()
    _task(paths.tasks_dir, "T1", "mvp-first-time-flow", "open")
    _task(paths.tasks_dir, "T2", "shakedown", "done")

    sync_loops_registry(paths, EventLog(paths.events_path))

    loops = read_json(paths.state_file, {})["loops"]
    assert loops["mvp-first-time-flow"]["status"] == "in-progress"
    assert loops["shakedown"]["status"] == "done"


def test_sync_preserves_existing_ledger(tmp_path):
    paths = SessionPaths(tmp_path, "govern")
    paths.ensure()
    _task(paths.tasks_dir, "T1", "alpha", "open")
    atomic_write_json(
        paths.state_file,
        {
            "schema_version": 2,
            "objective": "ship it",
            "loops": {"alpha": {"status": "done", "branch": "loop/govern/alpha"}},
        },
    )

    sync_loops_registry(paths, EventLog(paths.events_path))

    ledger = read_json(paths.state_file, {})
    assert ledger["objective"] == "ship it"  # top-level preserved
    assert ledger["schema_version"] == 2
    assert ledger["loops"]["alpha"]["branch"] == "loop/govern/alpha"  # preserved
    assert ledger["loops"]["alpha"]["status"] == "in-progress"  # refreshed from tasks


def test_sync_no_tasks_writes_nothing(tmp_path):
    paths = SessionPaths(tmp_path, "govern")
    paths.ensure()
    assert sync_loops_registry(paths, EventLog(paths.events_path)) == 0
    assert not paths.state_file.exists()  # nothing derived => no ledger write


def test_sync_is_idempotent(tmp_path):
    paths = SessionPaths(tmp_path, "govern")
    paths.ensure()
    _task(paths.tasks_dir, "T1", "alpha", "open")
    sync_loops_registry(paths, EventLog(paths.events_path))
    mtime = paths.state_file.stat().st_mtime_ns

    sync_loops_registry(paths, EventLog(paths.events_path))  # nothing changed

    assert paths.state_file.stat().st_mtime_ns == mtime  # no rewrite
