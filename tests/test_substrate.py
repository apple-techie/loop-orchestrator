"""Substrate wrappers against tests/fakes/bin — no tmux, no real harnesses."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from loop_orchestrator import substrate as substrate_mod
from loop_orchestrator.contract import ContractMismatch
from loop_orchestrator.paths import normalize_project_root
from loop_orchestrator.substrate import (
    LaneInfo,
    LaneStatus,
    Substrate,
    SubstrateError,
    _loop_summary,
)

FAKES_BIN = Path(__file__).resolve().parent / "fakes" / "bin"


@pytest.fixture
def sub(fakes_env: Path, tmp_path: Path) -> Substrate:
    return Substrate(tmp_path, "demo")


def test_resolution_prefers_loop_substrate_bin(sub, tmp_path, monkeypatch):
    decoy = tmp_path / "decoy-path"
    decoy.mkdir()
    decoy_tmux = decoy / "loop-tmux"
    decoy_tmux.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
    decoy_tmux.chmod(0o755)
    monkeypatch.setenv("PATH", f"{decoy}{os.pathsep}{os.environ['PATH']}")
    assert sub._resolve("loop-tmux") == [str(FAKES_BIN / "loop-tmux")]
    monkeypatch.delenv("LOOP_SUBSTRATE_BIN")
    assert sub._resolve("loop-tmux") == [str(decoy_tmux)]


def test_substrate_normalizes_project_root_like_session_paths(tmp_path):
    project = tmp_path / "project"
    (project / "nested").mkdir(parents=True)

    sub = Substrate(project / "nested" / "..", "demo")

    assert sub.project_root == normalize_project_root(project)


def test_loop_summary_reports_normalized_project_root(tmp_path):
    project = tmp_path / "project"
    (project / "nested").mkdir(parents=True)
    (project / ".loop" / "sessions" / "demo" / "engine").mkdir(parents=True)

    summary = _loop_summary(project / "nested" / "..", "demo")

    assert summary.project_root == str(normalize_project_root(project))


def test_lanes_parses_canned_json(sub, call_log):
    lanes = sub.lanes()
    assert [lane.window for lane in lanes] == ["coord", "web", "docs", "helper"]
    assert lanes[3] == LaneInfo(
        window="helper",
        harness="claude",
        model=None,
        role="impl",
        cmd="claude",
        base=False,
        kind="worker",  # T0019: dynamic lane resolves to worker
    )
    assert all(lane.base for lane in lanes[:3])
    assert all(lane.kind == "standing" for lane in lanes[:3])  # T0019: base -> standing
    assert call_log() == ["loop-tmux list-lanes --session demo --json"]


def test_lane_status_all(sub, call_log):
    statuses = sub.lane_status_all()
    assert set(statuses) == {"coord", "web", "docs", "helper"}
    assert statuses["web"] == LaneStatus(
        lane="web", status="idle", target="demo:web.1", kind="fixed"
    )
    assert statuses["helper"].kind == "dynamic"
    assert call_log() == ["loop-lane-status --json --all demo"]


def test_lane_status_override(sub, monkeypatch):
    monkeypatch.setenv("FAKE_LANE_STATUS_OVERRIDE", "web=working,docs=errored")
    statuses = sub.lane_status_all()
    assert statuses["web"].status == "working"
    assert statuses["docs"].status == "errored"
    assert statuses["coord"].status == "idle"
    assert sub.lane_status("docs") == "errored"


def test_lane_status_and_print_target(sub, call_log):
    assert sub.lane_status("web") == "idle"
    assert sub.print_target("web") == "demo:web.1"
    assert call_log() == [
        "loop-lane-status demo web",
        "loop-lane-status --print-target demo web",
    ]


def test_digest_parses_canned_doc(sub):
    doc = sub.digest()
    assert doc["state"]["loops"]["loop-demo"]["status"] == "implement"
    assert len(doc["mailbox"]["pending"]) == 1
    assert doc["mailbox"]["pending"][0]["from"] == "web"
    assert doc["mailbox"]["processed_count"] == 3
    assert doc["adrs"] == []


def test_checkpoint_prompt_default_header(sub):
    prompt = sub.checkpoint_prompt()
    assert prompt.startswith("## Coordinator checkpoint (fake default header)\n")
    assert "loops: loop-demo implement" in prompt
    assert prompt.count("\n") >= 4


def test_checkpoint_prompt_header_file(sub, tmp_path, call_log):
    header = tmp_path / "header.md"
    header.write_text("CUSTOM HEADER LINE\n", encoding="utf-8")
    prompt = sub.checkpoint_prompt(header_file=header)
    assert prompt.startswith("CUSTOM HEADER LINE\n")
    assert "## Coordinator checkpoint" not in prompt
    assert "loops: loop-demo implement" in prompt
    assert f"--header-file {header}" in call_log()[0]


def test_pending_count(sub):
    assert sub.pending_count() == 2


def test_harness_registry(sub, call_log):
    assert sub.oneshot_template("claude") == "claude -p {prompt}"
    assert sub.harness_field("claude", "oneshot_template") == "claude -p {prompt}"
    sub.harness_field("claude", "oneshot_template")  # cached: must not spawn again
    assert call_log() == [
        "harness-registry oneshot claude",
        "harness-registry field claude oneshot_template",
    ]
    with pytest.raises(SubstrateError):
        sub.oneshot_template("pi")


def test_harness_roster_unstubbed_is_empty(sub, call_log):
    assert sub.harness_roster() == {}
    assert call_log() == ["harness-registry roster --json"]


def test_harness_roster_parses_entries(sub, monkeypatch):
    monkeypatch.setenv(
        "FAKE_ROSTER_JSON",
        '{"contract_version": 1, "harnesses": ['
        '{"name": "claude", "present": true, "drift_pins": "low"},'
        '{"name": "amp", "present": false, "drift_pins": "high"}]}',
    )
    roster = sub.harness_roster()
    assert set(roster) == {"claude", "amp"}
    assert roster["claude"]["present"] is True
    assert roster["amp"]["drift_pins"] == "high"


def test_run_gate_runs_make_check_then_pytest(sub, monkeypatch):
    calls: list[tuple[list[str], Path, float]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["cwd"], kwargs["timeout"]))
        return substrate_mod.subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"ok {' '.join(argv)}\n",
            stderr="",
        )

    monkeypatch.setattr(substrate_mod.subprocess, "run", fake_run)

    passed, output = sub.run_gate(timeout=12)

    assert passed is True
    assert [argv for argv, _cwd, _timeout in calls] == [
        ["make", "check"],
        ["uv", "run", "pytest"],
    ]
    assert {cwd for _argv, cwd, _timeout in calls} == {sub.project_root}
    assert {timeout for _argv, _cwd, timeout in calls} == {12}
    assert "ok make check" in output
    assert "ok uv run pytest" in output


def test_run_gate_stops_on_first_failure(sub, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return substrate_mod.subprocess.CompletedProcess(argv, 2, stdout="", stderr="bad\n")

    monkeypatch.setattr(substrate_mod.subprocess, "run", fake_run)

    passed, output = sub.run_gate(timeout=12)

    assert passed is False
    assert calls == [["make", "check"]]
    assert "bad" in output


def test_git_diff_returns_stdout(sub, monkeypatch):
    calls: list[tuple[list[str], Path, float]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["cwd"], kwargs["timeout"]))
        return substrate_mod.subprocess.CompletedProcess(
            argv, 0, stdout="diff --git a b\n", stderr=""
        )

    monkeypatch.setattr(substrate_mod.subprocess, "run", fake_run)

    assert sub.git_diff("base", "tip", timeout=8) == "diff --git a b\n"
    assert calls == [(["git", "diff", "base..tip"], sub.project_root, 8)]


def test_git_diff_failure_raises_substrate_error(sub, monkeypatch):
    def fake_run(argv, **kwargs):
        return substrate_mod.subprocess.CompletedProcess(argv, 128, stdout="", stderr="bad rev\n")

    monkeypatch.setattr(substrate_mod.subprocess, "run", fake_run)

    with pytest.raises(SubstrateError) as exc:
        sub.git_diff("base", "tip")
    assert exc.value.returncode == 128
    assert "bad rev" in exc.value.stderr


def test_branch_head_returns_rev_parse_stdout(sub, monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    calls: list[tuple[list[str], Path, float]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["cwd"], kwargs["timeout"]))
        return substrate_mod.subprocess.CompletedProcess(argv, 0, stdout="abc123\n", stderr="")

    monkeypatch.setattr(substrate_mod.subprocess, "run", fake_run)

    assert sub.branch_head(worktree, "loop/demo/web") == "abc123"
    assert calls == [(["git", "rev-parse", "loop/demo/web"], worktree, 5)]


def test_branch_head_returns_none_on_git_error(sub, monkeypatch, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()

    def fake_run(argv, **kwargs):
        return substrate_mod.subprocess.CompletedProcess(
            argv, 128, stdout="", stderr="unknown revision\n"
        )

    monkeypatch.setattr(substrate_mod.subprocess, "run", fake_run)

    assert sub.branch_head(worktree, "loop/demo/missing") is None


def test_process_command_returns_ps_command(sub, monkeypatch):
    calls: list[tuple[list[str], float]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        return substrate_mod.subprocess.CompletedProcess(
            argv, 0, stdout="uv run loop-verify --out x\n", stderr=""
        )

    monkeypatch.setattr(substrate_mod.subprocess, "run", fake_run)

    assert sub.process_command(123, timeout=3) == "uv run loop-verify --out x"
    assert calls == [(["ps", "-p", "123", "-o", "command="], 3)]


def test_process_command_missing_pid_returns_none(sub, monkeypatch):
    def fake_run(argv, **kwargs):
        return substrate_mod.subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(substrate_mod.subprocess, "run", fake_run)

    assert sub.process_command(123) is None


def test_spawn_verify_exec_failure_raises_immediately(sub, tmp_path, monkeypatch):
    missing = tmp_path / "missing-loop-verify"
    monkeypatch.setattr(sub, "_verify_argv", lambda: [str(missing)])

    with pytest.raises(SubstrateError) as exc:
        sub.spawn_verify(tmp_path, "main", "feature", tmp_path / "verify.json")

    assert exc.value.returncode == 127
    assert str(missing) in exc.value.stderr


def test_spawn_build_exec_failure_raises_immediately(sub, tmp_path, monkeypatch):
    missing = tmp_path / "missing-codex"
    monkeypatch.setattr(sub, "_codex_argv", lambda: [str(missing)])

    with pytest.raises(SubstrateError) as exc:
        sub.spawn_build(tmp_path, "implement and commit")

    assert exc.value.returncode == 127
    assert str(missing) in exc.value.stderr


def test_build_argv_uses_codex_exec_with_worktree_cd(sub, tmp_path, monkeypatch):
    monkeypatch.setattr(sub, "_codex_argv", lambda: ["/bin/codex"])

    argv = sub._build_argv(tmp_path, "implement and commit")

    assert argv == [
        "/bin/codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "--cd",
        str(tmp_path),
        "implement and commit",
    ]


def test_verify_exec_handshake_timeout_raises(sub):
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(SubstrateError) as exc:
            sub._read_exec_error(read_fd, ["loop-verify"], timeout=0.01)
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert "exec handshake timed out" in exc.value.stderr


def test_dispatch_argv_order(sub, call_log):
    sub.dispatch("web", "echo hi", wait_ready=True, interrupt=True)
    sub.dispatch("docs", "hello", mode="command")
    assert call_log() == [
        "loop-dispatch --session demo --mode text --wait-ready --interrupt web echo hi",
        "loop-dispatch --session demo --mode command docs hello",
    ]


def test_dispatch_no_clear_flag(sub, call_log):
    # no_clear=True appends --no-clear (opt out of the auto-/clear, #36); the
    # default omits it so loop-dispatch's claude fresh-dispatch clear stays on.
    sub.dispatch("web", "hello", no_clear=True)
    sub.dispatch("docs", "world")
    assert call_log() == [
        "loop-dispatch --session demo --mode text --no-clear web hello",
        "loop-dispatch --session demo --mode text docs world",
    ]


def test_dispatch_failure_captures_stderr(sub, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_FAIL", "1")
    with pytest.raises(SubstrateError) as exc:
        sub.dispatch("web", "hello")
    assert exc.value.returncode == 1
    assert "pane vanished" in exc.value.stderr
    assert "pane vanished" in str(exc.value)


def test_add_lane_argv(sub, call_log):
    sub.add_lane("helper2", harness="claude", model="opus", role="impl", auto_approve=True)
    assert call_log() == [
        "loop-tmux add-lane --session demo --window helper2 --harness claude "
        "--model opus --role impl --auto-approve --wait-ready"
    ]


def test_add_lane_shared_default_omits_worktree(sub, call_log):
    # T0025: the shared default must NOT pass --worktree (byte-identical to today).
    sub.add_lane("helper3", harness="claude")
    assert "--worktree" not in call_log()[0]


def test_add_lane_worktree_appends_flag(sub, call_log):
    sub.add_lane("helper4", harness="claude", worktree=True)
    assert "--worktree" in call_log()[0]


def test_drop_lane_never_forces(sub, call_log):
    sub.drop_lane("helper")  # fake exits 7 on --force, so success also proves it
    (line,) = call_log()
    assert line == "loop-tmux drop-lane --session demo --window helper"
    assert "--force" not in line


def test_contract_mismatch_raises(tmp_path, monkeypatch):
    bad_bin = tmp_path / "bin-v99"
    bad_bin.mkdir()
    script = bad_bin / "loop-digest"
    script.write_text("#!/usr/bin/env bash\necho '{\"contract_version\": 99}'\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("LOOP_SUBSTRATE_BIN", str(bad_bin))
    with pytest.raises(ContractMismatch):
        Substrate(tmp_path, "demo").digest()


def test_timeout_param_plumbed(sub, monkeypatch):
    seen: dict[str, float] = {}

    def fake_run(argv, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return substrate_mod.subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(substrate_mod.subprocess, "run", fake_run)
    sub.dispatch("web", "hello", timeout=7)
    assert seen["timeout"] == 7
    sub.lane_status("web")
    assert seen["timeout"] == 15
