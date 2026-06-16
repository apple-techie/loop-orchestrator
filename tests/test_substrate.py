"""Substrate wrappers against tests/fakes/bin — no tmux, no real harnesses."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from loop_orchestrator import substrate as substrate_mod
from loop_orchestrator.contract import ContractMismatch
from loop_orchestrator.substrate import LaneInfo, LaneStatus, Substrate, SubstrateError

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
