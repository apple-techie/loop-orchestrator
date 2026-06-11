"""PM hooks in run_once: pull before the brain, push after execution, and
adapter trouble (unavailable / raising / unknown) never aborts a cycle.

Zero configured adapters (the default) must be byte-for-byte the current
behavior: no discovery scan, no pm-* events.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loop_orchestrator.engine.config import EngineConfig, PmConfig
from loop_orchestrator.engine.loop import run_once
from loop_orchestrator.engine.wiki import MARKER
from loop_orchestrator.paths import SessionPaths
from loop_orchestrator.pm import registry
from loop_orchestrator.pm.base import PMAdapter, PMSyncResult

FAKES_BIN = Path(__file__).resolve().parent / "fakes" / "bin"
COMPILED = "# Checkpoint\n\ncompiled state, docs-owned\n\n" + MARKER + "\n"


class StubAdapter(PMAdapter):
    name = "stub"
    calls: list[tuple[str, Path, bool]] = []

    def validate_env(self) -> list[str]:
        return []

    def pull(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        type(self).calls.append(("pull", tasks_dir, dry_run))
        return PMSyncResult(
            created=["tasks/T0001-fix-login.md"],
            conflicts=["PROJ-9: local status 'open' vs remote 'done'"],
            dry_run=dry_run,
        )

    def push(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        type(self).calls.append(("push", tasks_dir, dry_run))
        return PMSyncResult(updated=["PROJ-9"], dry_run=dry_run)


class UnavailableAdapter(PMAdapter):
    name = "unavail"

    def validate_env(self) -> list[str]:
        return ["STUB_TOKEN"]

    def pull(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        raise AssertionError("must not be called when unavailable")

    push = pull


class ExplodingAdapter(PMAdapter):
    name = "boom"

    def validate_env(self) -> list[str]:
        return []

    def pull(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        raise RuntimeError("pull exploded")

    def push(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        raise RuntimeError("push exploded")


REGISTRY = {"stub": StubAdapter, "unavail": UnavailableAdapter, "boom": ExplodingAdapter}


@pytest.fixture
def project(tmp_path: Path, fakes_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".loop" / "messages" / "processed").mkdir(parents=True)
    (root / "ops-wiki").mkdir()
    (root / "ops-wiki" / "checkpoint.md").write_text(COMPILED, encoding="utf-8")
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(FAKES_BIN / "fake-brain"))
    StubAdapter.calls = []
    return root


@pytest.fixture
def stub_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(registry, "discover", lambda override=None: dict(REGISTRY))


def _events(project: Path) -> list[dict]:
    path = SessionPaths(project, "demo").events_path
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _config(*names: str) -> EngineConfig:
    return EngineConfig(pm=PmConfig(adapters=[{"name": name} for name in names]))


def test_zero_adapters_is_exact_current_behavior(project, monkeypatch):
    def explode(override=None):
        raise AssertionError("discover must not be called with zero adapters")

    monkeypatch.setattr(registry, "discover", explode)

    assert run_once(project, "demo", EngineConfig()) == 0

    assert not [e for e in _events(project) if e["event"].startswith("pm-")]


def test_pull_before_brain_push_after_execution(project, stub_registry):
    assert run_once(project, "demo", _config("stub"), approval_mode_override="auto") == 0

    assert [c[0] for c in StubAdapter.calls] == ["pull", "push"]
    assert all(c[1] == project / "tasks" and c[2] is False for c in StubAdapter.calls)

    seq = {e["event"]: e["seq"] for e in _events(project)}
    assert seq["pm-pull"] < seq["brain-call"] < seq["action"] < seq["pm-push"]

    pull = next(e for e in _events(project) if e["event"] == "pm-pull")
    assert pull["adapter"] == "stub"
    assert (pull["created"], pull["updated"], pull["conflicts"], pull["errors"]) == (1, 0, 1, 0)
    push = next(e for e in _events(project) if e["event"] == "pm-push")
    assert (push["created"], push["updated"], push["conflicts"], push["errors"]) == (0, 1, 0, 0)


def test_push_runs_in_the_pending_approval_path_too(project, stub_registry):
    assert run_once(project, "demo", _config("stub")) == 0  # manual: action awaits approval

    kinds = [e["event"] for e in _events(project)]
    assert "decision-pending" in kinds and "pm-push" in kinds
    assert [c[0] for c in StubAdapter.calls] == ["pull", "push"]


def test_dry_run_pull_only_with_dry_run_flag(project, stub_registry):
    assert run_once(project, "demo", _config("stub"), dry_run=True) == 0

    assert StubAdapter.calls == [("pull", project / "tasks", True)]


def test_unavailable_adapter_is_skipped(project, stub_registry):
    assert run_once(project, "demo", _config("unavail"), approval_mode_override="auto") == 0

    skips = [e for e in _events(project) if e["event"] == "pm-skip"]
    assert [s["reason"] for s in skips] == ["unavailable", "unavailable"]  # pull + push
    assert "cycle-end" in [e["event"] for e in _events(project)]


def test_raising_adapter_never_aborts_the_cycle(project, stub_registry):
    assert run_once(project, "demo", _config("boom", "stub"), approval_mode_override="auto") == 0

    errors = [e for e in _events(project) if e["event"] == "pm-error"]
    assert [(e["adapter"], e["op"]) for e in errors] == [("boom", "pull"), ("boom", "push")]
    assert "pull exploded" in errors[0]["error"]
    # the healthy adapter still synced and the cycle completed normally
    assert [c[0] for c in StubAdapter.calls] == ["pull", "push"]
    events = [e["event"] for e in _events(project)]
    assert "cycle-end" in events and "decision" in events


def test_unknown_adapter_name_is_skipped(project, stub_registry):
    assert run_once(project, "demo", _config("nope"), approval_mode_override="auto") == 0

    skips = [e for e in _events(project) if e["event"] == "pm-skip"]
    assert skips and skips[0]["adapter"] == "nope" and skips[0]["reason"] == "unknown-adapter"
