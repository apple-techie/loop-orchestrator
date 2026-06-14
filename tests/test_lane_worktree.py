"""lib/lane-worktree.sh — conditional worktree isolation lifecycle (T0025).

Exercises the sourceable lib directly in a throwaway git repo (no tmux): a
worktree provisions on its own branch and records loops.<window>.branch; a clean
teardown removes it without orphaning; a dirty tree is PRESERVED (never
force-removed). LOOP_WORKTREE_SKIP_VENV avoids the uv sync.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib" / "lane-worktree.sh"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-qm", "init")
    return root


def _run(func: str, *args: str) -> subprocess.CompletedProcess[str]:
    script = f'source "{LIB}"; {func} "$@"'
    return subprocess.run(
        ["bash", "-c", script, "_", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "LOOP_WORKTREE_SKIP_VENV": "1"},
    )


def test_provision_creates_worktree_and_records_branch(tmp_path):
    root = _repo(tmp_path)
    r = _run("lane_worktree_provision", str(root), "govern", "worker-1")
    assert r.returncode == 0, r.stderr
    wt = root / ".loop" / "worktrees" / "govern" / "worker-1"
    assert wt.is_dir()
    assert r.stdout.strip() == str(wt)  # prints the resolved cwd
    assert "loop/govern/worker-1" in _git(root, "worktree", "list")
    ledger = json.loads((root / ".loop" / "orchestrator-state.json").read_text(encoding="utf-8"))
    assert ledger["loops"]["worker-1"]["branch"] == "loop/govern/worker-1"


def test_teardown_clean_removes_without_orphan(tmp_path):
    root = _repo(tmp_path)
    _run("lane_worktree_provision", str(root), "govern", "w2")
    r = _run("lane_worktree_teardown", str(root), "govern", "w2")
    assert r.returncode == 0, r.stderr
    assert not (root / ".loop" / "worktrees" / "govern" / "w2").exists()
    assert "w2" not in _git(root, "worktree", "list")  # no orphan in git admin


def test_teardown_dirty_preserves_worktree(tmp_path):
    root = _repo(tmp_path)
    out = _run("lane_worktree_provision", str(root), "govern", "w3")
    wt = Path(out.stdout.strip())
    (wt / "uncommitted.txt").write_text("WIP\n", encoding="utf-8")
    r = _run("lane_worktree_teardown", str(root), "govern", "w3")
    assert r.returncode == 0
    assert "PRESERVING the worktree" in r.stderr
    assert wt.is_dir()  # dirty tree NOT removed — no work lost, no force


def test_teardown_noop_for_shared_lane(tmp_path):
    root = _repo(tmp_path)
    r = _run("lane_worktree_teardown", str(root), "govern", "never-provisioned")
    assert r.returncode == 0  # no worktree dir -> no-op (shared path)
