import os
import sys
import types
from pathlib import Path

import pytest

from loop_orchestrator.deck import cli as deck_cli
from loop_orchestrator.engine import cli as engine_cli
from loop_orchestrator.engine.actions import _lane_worktree
from loop_orchestrator.paths import SessionPaths, normalize_project_root


def test_session_paths_normalizes_project_root_and_lane_worktree(tmp_path):
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)

    paths = SessionPaths(project / "nested" / "..", "demo")

    assert paths.project_root == normalize_project_root(project)
    assert _lane_worktree(paths, "web") == (project / ".loop" / "worktrees" / "demo" / "web")


def test_session_paths_preserves_symlink_spelling(tmp_path):
    target = tmp_path / "target"
    (target / "nested").mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    paths = SessionPaths(link / "nested" / "..", "demo")

    assert paths.project_root == Path(os.path.abspath(link))
    assert paths.project_root != target.resolve()
    assert paths.engine_dir == paths.project_root / ".loop" / "sessions" / "demo" / "engine"


def test_session_paths_keeps_engine_state_paths_consistent(tmp_path):
    paths = SessionPaths(tmp_path / "project", "demo")

    assert paths.events_path == paths.engine_dir / "events.jsonl"
    assert paths.snapshot_path == paths.engine_dir / "snapshot.json"
    assert paths.pid_path == paths.engine_dir / "engine.pid"
    assert paths.lock_path == paths.engine_dir / ".lock"
    assert paths.paused_path == paths.engine_dir / "paused"


def test_session_paths_accepts_missing_project_root_without_creating_it(tmp_path):
    missing = tmp_path / "missing"

    paths = SessionPaths(missing / "nested" / "..", "demo")

    assert paths.project_root == normalize_project_root(missing)
    assert not missing.exists()


def test_engine_cli_uses_symlink_preserving_normalizer(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    captured = {}

    def handler(_args, root):
        captured["root"] = root
        return 0

    monkeypatch.setitem(engine_cli._HANDLERS, "status", handler)

    assert engine_cli.main(["--project-root", str(link), "status"]) == 0
    assert captured["root"] == Path(os.path.abspath(link))
    assert captured["root"] != target.resolve()


def test_deck_check_uses_symlink_preserving_normalizer(tmp_path, monkeypatch, capsys):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    monkeypatch.setitem(sys.modules, "textual", types.ModuleType("textual"))

    assert deck_cli.main(["--check", "--project-root", str(link)]) == 0
    out = capsys.readouterr().out
    assert f"project={Path(os.path.abspath(link))}" in out
    assert str(target.resolve()) not in out
