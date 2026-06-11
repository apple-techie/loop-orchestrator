"""loop-pm CLI over the registry override seam (no installed metadata needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from loop_orchestrator.pm import registry
from loop_orchestrator.pm.base import PMAdapter, PMSyncResult
from loop_orchestrator.pm.cli import main


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


class MissingEnvAdapter(PMAdapter):
    name = "missing"

    def validate_env(self) -> list[str]:
        return ["STUB_BASE_URL", "STUB_TOKEN"]

    def pull(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        raise AssertionError("must not be called when unavailable")

    push = pull


class FailingAdapter(PMAdapter):
    name = "failing"

    def validate_env(self) -> list[str]:
        return []

    def pull(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        return PMSyncResult(errors=["search exploded"], dry_run=dry_run)

    push = pull


REGISTRY = {"stub": StubAdapter, "missing": MissingEnvAdapter, "failing": FailingAdapter}


@pytest.fixture(autouse=True)
def _reset_stub_calls():
    StubAdapter.calls = []


def test_registry_override_seam():
    assert registry.discover(override=[("stub", StubAdapter)]) == {"stub": StubAdapter}
    assert registry.discover(override=[]) == {}


def test_list_adapters_with_override(capsys):
    assert main(["list-adapters"], registry=REGISTRY) == 0
    out = capsys.readouterr().out
    assert "stub  available" in out
    assert "missing  unavailable (missing: STUB_BASE_URL, STUB_TOKEN)" in out


def test_list_adapters_empty_registry(capsys):
    assert main(["list-adapters"], registry={}) == 0
    assert "(no PM adapters installed)" in capsys.readouterr().out


def test_sync_unknown_adapter_exits_64(capsys):
    rc = main(["sync", "--adapter", "nope"], registry=REGISTRY)
    assert rc == 64
    err = capsys.readouterr().err
    assert "unknown adapter 'nope'" in err
    assert "failing, missing, stub" in err  # known list


def test_sync_unavailable_adapter_exits_64_with_missing_vars(capsys):
    rc = main(["sync", "--adapter", "missing"], registry=REGISTRY)
    assert rc == 64
    err = capsys.readouterr().err
    assert "STUB_BASE_URL, STUB_TOKEN" in err


def test_sync_happy_path_both(tmp_path: Path, capsys):
    rc = main(
        ["sync", "--adapter", "stub", "both", "--project-root", str(tmp_path)],
        registry=REGISTRY,
    )
    assert rc == 0  # conflicts are not failures
    captured = capsys.readouterr()
    assert "pull: 1 created, 0 updated, 1 conflict(s), 0 error(s)" in captured.out
    assert "push: 0 created, 1 updated, 0 conflict(s), 0 error(s)" in captured.out
    assert "created tasks/T0001-fix-login.md" in captured.out
    assert "local status 'open'" not in captured.out  # conflict details: stderr only
    assert "conflict (file wins): PROJ-9" in captured.err
    assert StubAdapter.calls == [
        ("pull", tmp_path.resolve() / "tasks", False),
        ("push", tmp_path.resolve() / "tasks", False),
    ]


def test_sync_pull_only_with_dry_run_and_tasks_dir(tmp_path: Path, capsys):
    tasks_dir = tmp_path / "elsewhere"
    rc = main(
        ["sync", "--adapter", "stub", "pull", "--dry-run", "--tasks-dir", str(tasks_dir)],
        registry=REGISTRY,
    )
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert StubAdapter.calls == [("pull", tasks_dir, True)]


def test_sync_errors_exit_1(capsys):
    rc = main(["sync", "--adapter", "failing", "pull"], registry=REGISTRY)
    assert rc == 1
    assert "error: search exploded" in capsys.readouterr().err
