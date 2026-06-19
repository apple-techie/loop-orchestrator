"""Cross-session fleet view (T0033/B3): read-only aggregation over many loops.

Builds throwaway `.loop/sessions/<s>/engine/` state under tmp_path — no tmux,
no engine, no subprocess. Asserts engine state (running/paused/stopped), pending
queue depth + awaiting flag, lane-health counts, multi-root discovery, the
`loop-deck --all` CLI render, and the deck NON-WRITER invariant (it writes nothing).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from loop_orchestrator.deck import cli as deck_cli
from loop_orchestrator.substrate import discover_loops, render_fleet


def _make_loop(
    root: Path,
    session: str,
    *,
    pid: int | None = None,
    paused: bool = False,
    lanes: dict[str, str] | None = None,
    pending: dict | None = None,
) -> Path:
    engine = root / ".loop" / "sessions" / session / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    if pid is not None:
        (engine / "engine.pid").write_text(f"{pid}\n", encoding="utf-8")
    if paused:
        (engine / "paused").touch()
    if lanes is not None:
        snap = {"lanes": {name: {"status": st} for name, st in lanes.items()}}
        (engine / "snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
    if pending is not None:
        (engine / "pending-decision.json").write_text(json.dumps(pending), encoding="utf-8")
    return engine


def test_no_sessions_is_empty_fleet(tmp_path):
    assert discover_loops([tmp_path]) == []
    assert "no loops found" in render_fleet([])


def test_running_loop_summary(tmp_path):
    _make_loop(
        tmp_path, "demo", pid=os.getpid(), lanes={"web": "working", "docs": "idle", "ops": "idle"}
    )
    (summary,) = discover_loops([tmp_path])
    assert summary.session == "demo"
    assert summary.engine == "running" and summary.pid == os.getpid()
    assert summary.pending == 0 and summary.awaiting_approval is False
    assert summary.lane_health == {"working": 1, "idle": 2}


def test_paused_loop_renders_paused(tmp_path):
    _make_loop(tmp_path, "demo", pid=os.getpid(), paused=True, lanes={"web": "idle"})
    (summary,) = discover_loops([tmp_path])
    assert summary.engine == "paused" and summary.pid == os.getpid()


def test_stopped_loop_without_pid(tmp_path):
    _make_loop(tmp_path, "demo", lanes={"web": "idle"})  # engine dir but no pid file
    (summary,) = discover_loops([tmp_path])
    assert summary.engine == "stopped" and summary.pid is None


def test_stopped_loop_with_stale_pid(tmp_path):
    _make_loop(tmp_path, "demo", pid=2147483646)  # a pid that does not exist
    (summary,) = discover_loops([tmp_path])
    assert summary.engine == "stopped" and summary.pid is None


def test_pending_decision_awaiting_approval(tmp_path):
    _make_loop(
        tmp_path,
        "demo",
        pid=os.getpid(),
        pending={"id": "d-1", "actions": [{"status": "awaiting-approval"}]},
    )
    (summary,) = discover_loops([tmp_path])
    assert summary.pending == 1 and summary.awaiting_approval is True
    assert "1*" in render_fleet([summary])  # the awaiting marker


def test_pending_decision_not_awaiting(tmp_path):
    _make_loop(
        tmp_path,
        "demo",
        pid=os.getpid(),
        pending={"id": "d-1", "actions": [{"status": "executed"}]},
    )
    (summary,) = discover_loops([tmp_path])
    assert summary.pending == 1 and summary.awaiting_approval is False


def test_two_loops_across_two_roots(tmp_path):
    r1, r2 = tmp_path / "p1", tmp_path / "p2"
    _make_loop(r1, "alpha", pid=os.getpid(), lanes={"web": "working"})
    _make_loop(r2, "beta", lanes={"web": "idle"})  # stopped (no pid)
    by_session = {s.session: s for s in discover_loops([r1, r2])}
    assert set(by_session) == {"alpha", "beta"}
    assert by_session["alpha"].engine == "running"
    assert by_session["beta"].engine == "stopped"
    out = render_fleet(list(by_session.values()))
    assert "alpha" in out and "beta" in out and "running" in out and "stopped" in out


def test_discover_dedupes_repeated_roots(tmp_path):
    _make_loop(tmp_path, "demo", pid=os.getpid())
    assert len(discover_loops([tmp_path, tmp_path])) == 1


def test_discover_dedup_uses_reported_root_normalization(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    _make_loop(target, "demo", pid=os.getpid())

    roots = {summary.project_root for summary in discover_loops([link, target])}
    assert roots == {str(Path(os.path.abspath(link))), str(Path(os.path.abspath(target)))}


def test_discover_writes_nothing(tmp_path):
    engine = _make_loop(tmp_path, "demo", pid=os.getpid(), lanes={"web": "idle"})
    before = sorted(p.name for p in engine.iterdir())
    discover_loops([tmp_path])
    assert sorted(p.name for p in engine.iterdir()) == before  # non-writer


def test_deck_all_cli_prints_fleet(tmp_path, capsys):
    _make_loop(tmp_path, "demo", pid=os.getpid(), lanes={"web": "working"})
    assert deck_cli.main(["--all", "--project-root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "demo" in out and "running" in out and "working:1" in out


def test_deck_all_cli_multi_roots(tmp_path, capsys):
    r1, r2 = tmp_path / "p1", tmp_path / "p2"
    _make_loop(r1, "alpha", pid=os.getpid())
    _make_loop(r2, "beta", lanes={"web": "idle"})
    assert deck_cli.main(["--all", "--roots", f"{r1},{r2}"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out


def test_deck_all_cli_empty_no_crash(tmp_path, capsys):
    assert deck_cli.main(["--all", "--project-root", str(tmp_path)]) == 0
    assert "no loops found" in capsys.readouterr().out
