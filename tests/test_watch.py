"""Watch daemon: pure trigger policy, tick behavior, asks, headless ingest."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from loop_orchestrator.engine import actions, cli
from loop_orchestrator.engine import loop as loop_mod
from loop_orchestrator.engine import watch as watch_mod
from loop_orchestrator.engine.config import EngineConfig, IngestConfig, LintConfig, MetricsConfig
from loop_orchestrator.engine.events import EventLog, utc_now
from loop_orchestrator.engine.loop import run_once
from loop_orchestrator.engine.watch import TriggerState, Watch, evaluate_triggers
from loop_orchestrator.engine.wiki import MARKER
from loop_orchestrator.paths import SessionPaths
from loop_orchestrator.substrate import Substrate

FAKES_BIN = Path(__file__).resolve().parent / "fakes" / "bin"
COMPILED = "# Checkpoint\n\ncompiled state, docs-owned\n\n" + MARKER + "\n"
NOW = 1_750_000_000.0

AGENTS_STUB = """# AGENTS.md

### Ingest
General ingest words.

### Ingest protocol
Move each processed file to processed/ and append to log.md.

### Coordinator contract
not part of the protocol section.
"""


@pytest.fixture
def project(tmp_path: Path, fakes_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".loop" / "messages" / "processed").mkdir(parents=True)
    (root / "ops-wiki").mkdir()
    (root / "ops-wiki" / "checkpoint.md").write_text(COMPILED, encoding="utf-8")
    (root / "AGENTS.md").write_text(AGENTS_STUB, encoding="utf-8")
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(FAKES_BIN / "fake-brain"))
    monkeypatch.delenv("LOOP_ENGINE_INGEST_CMD", raising=False)
    return root


@pytest.fixture
def cycle_recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace loop.run_once with a recorder so ticks never run a real cycle."""
    calls: list[tuple[str, str]] = []

    def fake_run_once(project_root, session, config, **kwargs):
        calls.append((str(project_root), session))
        return 0

    monkeypatch.setattr(loop_mod, "run_once", fake_run_once)
    return calls


def _events(project: Path) -> list[dict]:
    path = SessionPaths(project, "demo").events_path
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _watch(project: Path, **overrides) -> Watch:
    values = {"poll_interval_s": 0, "min_cycle_interval_s": 0}
    values.update(overrides)
    config = EngineConfig(**values)
    return Watch(project, "demo", config)


def _settled(w: Watch) -> Watch:
    """Seed the previous snapshot + a recent cycle so nothing triggers."""
    w._prev_snapshot = w.observer.snapshot().to_dict()
    w._last_cycle_start = time.time()
    return w


def _cycle_triggers(project: Path) -> list[dict]:
    return [e for e in _events(project) if e["event"] == "cycle-trigger"]


def _seed_loop_task(
    paths: SessionPaths, task_id: str, loop: str, status: str = "open", depends_on=None
) -> None:
    paths.tasks_dir.mkdir(parents=True, exist_ok=True)
    depends_on = [] if depends_on is None else depends_on
    deps = ", ".join(depends_on)
    (paths.tasks_dir / f"{task_id}-x.md").write_text(
        f"---\nid: {task_id}\ntitle: x\nstatus: {status}\n"
        f"loop: {loop}\ndepends_on: [{deps}]\nscope: src\n---\n",
        encoding="utf-8",
    )


def test_cycle_routed_candidate_non_routing_pending_decision_is_not_progress(project: Path):
    """HIGH regression: a pending decision that does NOT route the candidate idle
    lane must NOT count as lane-utilization progress — otherwise the no-progress
    latch never engages and the brain is woken every cycle (was a wrong `return
    True`). A routing action still counts; a `stop` or a different-lane action does
    not."""
    w = _watch(project)
    lanes = {"web"}

    # A routing action targeting the candidate lane -> progress.
    w.paths.pending_decision_path.write_text(
        json.dumps(
            {"id": "d-1", "status": "pending", "actions": [{"kind": "dispatch", "lane": "web"}]}
        ),
        encoding="utf-8",
    )
    assert w._cycle_routed_candidate(0, lanes) is True

    # A non-routing pending decision (stop) -> NOT progress (the fixed branch).
    w.paths.pending_decision_path.write_text(
        json.dumps({"id": "d-2", "status": "pending", "actions": [{"kind": "stop"}]}),
        encoding="utf-8",
    )
    assert w._cycle_routed_candidate(0, lanes) is False

    # A routing KIND but a different lane -> NOT progress.
    w.paths.pending_decision_path.write_text(
        json.dumps(
            {"id": "d-3", "status": "pending", "actions": [{"kind": "dispatch", "lane": "infra"}]}
        ),
        encoding="utf-8",
    )
    assert w._cycle_routed_candidate(0, lanes) is False


# ── evaluate_triggers (pure, injected clock) ────────────────────────────────


def _state(**kw) -> TriggerState:
    base = dict(last_cycle_start=NOW - 10, checkpoint_interval_s=900, min_cycle_interval_s=0)
    base.update(kw)
    return TriggerState(**base)


def test_interval_trigger():
    assert evaluate_triggers(_state(last_cycle_start=None), NOW) == ["interval"]
    assert evaluate_triggers(_state(last_cycle_start=NOW - 900), NOW) == ["interval"]
    assert evaluate_triggers(_state(last_cycle_start=NOW - 899), NOW) == []


def test_flag_triggers():
    assert evaluate_triggers(_state(mailbox_new=True), NOW) == ["mailbox-new"]
    assert evaluate_triggers(_state(state_file_changed=True), NOW) == ["state-changed"]
    assert evaluate_triggers(_state(cycle_now=True), NOW) == ["cycle-now"]
    assert evaluate_triggers(_state(reply_received=True), NOW) == ["reply-received"]


def test_drive_pending_trigger():
    # In-flight build/verify work advances the pipeline without an external
    # trigger — but stays subject to the min_cycle_interval debounce.
    assert evaluate_triggers(_state(drive_pending=True), NOW) == ["drive-pending"]
    debounced = _state(last_cycle_start=NOW - 30, min_cycle_interval_s=120, drive_pending=True)
    assert evaluate_triggers(debounced, NOW) == []


def test_lane_utilization_pending_trigger_and_debounce():
    assert evaluate_triggers(_state(lane_utilization_pending=True), NOW) == [
        "lane-utilization-pending"
    ]
    debounced = _state(
        last_cycle_start=NOW - 119,
        min_cycle_interval_s=120,
        lane_utilization_pending=True,
    )
    assert evaluate_triggers(debounced, NOW) == []
    past_debounce = _state(
        last_cycle_start=NOW - 121,
        min_cycle_interval_s=120,
        lane_utilization_pending=True,
    )
    assert evaluate_triggers(past_debounce, NOW) == ["lane-utilization-pending"]


def test_lane_utilization_pending_absent_by_default():
    assert evaluate_triggers(_state(), NOW) == []


def test_lane_transition_trigger():
    for status in ("idle", "errored", "awaiting-approval"):
        st = _state(lane_transitions=[("web", "working", status)])
        assert evaluate_triggers(st, NOW) == [f"lane-transition:web:{status}"]
    boring = [("web", "idle", "working"), ("web", None, "idle"), ("web", "working", "unknown")]
    assert evaluate_triggers(_state(lane_transitions=boring), NOW) == []


def test_debounce_blocks_all_triggers():
    st = _state(
        last_cycle_start=NOW - 30,
        min_cycle_interval_s=120,
        mailbox_new=True,
        cycle_now=True,
        reply_received=True,
    )
    assert evaluate_triggers(st, NOW) == []
    past_debounce = _state(last_cycle_start=NOW - 120, min_cycle_interval_s=120, mailbox_new=True)
    assert evaluate_triggers(past_debounce, NOW) == ["mailbox-new"]


def test_reasons_accumulate():
    st = _state(last_cycle_start=None, mailbox_new=True, cycle_now=True)
    assert evaluate_triggers(st, NOW) == ["interval", "mailbox-new", "cycle-now"]


# ── singleton + lifecycle ───────────────────────────────────────────────────


def test_singleton_pid_refusal(project, capsys):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    paths.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    assert _watch(project).run() == 1

    assert "already running" in capsys.readouterr().err
    assert paths.pid_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert "watch-start" not in [e["event"] for e in _events(project)]


def test_run_takes_over_stale_pid_and_stops_cleanly(project, monkeypatch):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    paths.pid_path.write_text("99999999\n", encoding="utf-8")
    monkeypatch.setattr(watch_mod, "pid_alive", lambda pid: False)
    w = _watch(project)

    def fake_tick(now):
        assert paths.pid_path.read_text(encoding="utf-8").strip() == str(os.getpid())
        w._stop = True

    monkeypatch.setattr(w, "tick", fake_tick)
    assert w.run() == 0
    assert not paths.pid_path.exists()  # clean exit removed the pid file
    kinds = [e["event"] for e in _events(project)]
    assert "watch-start" in kinds and "watch-stop" in kinds


# ── tick: triggers and suppression ──────────────────────────────────────────


def test_tick_mailbox_new_triggers_run_once(project, cycle_recorder):
    w = _watch(project)
    w._last_cycle_start = time.time()  # suppress the interval trigger

    w.tick(time.time())  # first delta sees the digest's pending mailbox file

    assert cycle_recorder == [(str(project), "demo")]
    kinds = [e["event"] for e in _events(project)]
    assert "mailbox-new" in kinds and "cycle-trigger" in kinds and "cycle-result" in kinds

    w.tick(time.time())  # nothing new -> no second cycle
    assert len(cycle_recorder) == 1


def test_cycle_now_consumed(project, cycle_recorder):
    w = _settled(_watch(project))
    w.tick(time.time())
    assert cycle_recorder == []  # settled: nothing triggers

    w.paths.cycle_now_path.touch()
    w.tick(time.time())

    assert len(cycle_recorder) == 1
    assert not w.paths.cycle_now_path.exists()  # consumed
    triggers = [e for e in _events(project) if e["event"] == "cycle-trigger"]
    assert triggers[-1]["reasons"] == ["cycle-now"]


def test_lane_transition_tick_triggers(project, cycle_recorder, monkeypatch):
    monkeypatch.setenv("FAKE_LANE_STATUS_OVERRIDE", "web=working")
    w = _settled(_watch(project))
    monkeypatch.setenv("FAKE_LANE_STATUS_OVERRIDE", "web=errored")

    w.tick(time.time())

    assert len(cycle_recorder) == 1
    triggers = [e for e in _events(project) if e["event"] == "cycle-trigger"]
    assert triggers[-1]["reasons"] == ["lane-transition:web:errored"]


def test_idle_routable_lane_no_progress_suppresses_retrigger(project, cycle_recorder, monkeypatch):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    _seed_loop_task(paths, "T7001", "web")
    actions.record_ask(paths, "d-20260611-000000-0", "web", 1800)
    monkeypatch.setenv("FAKE_LANE_STATUS_OVERRIDE", "web=working")
    w = _watch(project, target_lane_utilization=1.0, min_cycle_interval_s=120)
    w._prev_snapshot = w.observer.snapshot().to_dict()
    w._last_cycle_start = NOW - 200

    monkeypatch.setenv("FAKE_LANE_STATUS_OVERRIDE", "web=idle")
    w.tick(NOW)

    assert len(cycle_recorder) == 1
    first = _cycle_triggers(project)[-1]["reasons"]
    assert "lane-transition:web:idle" in first
    assert "lane-utilization-pending" in first

    w.tick(NOW + 119)
    assert len(cycle_recorder) == 1

    w.tick(NOW + 121)

    assert len(cycle_recorder) == 1
    kinds = [event["event"] for event in _events(project)]
    assert "lane-utilization-no-progress" in kinds
    skips = [event for event in _events(project) if event["event"] == "cycle-skip"]
    assert skips[-1]["reason"] == "lane-utilization-no-progress"


def test_unroutable_idle_backlog_does_not_trigger_utilization(project, cycle_recorder, monkeypatch):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    _seed_loop_task(paths, "T7001", "web", depends_on=["T7000"])
    _seed_loop_task(paths, "T7002", "ops", status="in-progress")
    monkeypatch.setenv("FAKE_LANE_STATUS_OVERRIDE", "web=idle,ops=idle")
    w = _watch(project, target_lane_utilization=1.0, min_cycle_interval_s=120)
    w._prev_snapshot = w.observer.snapshot().to_dict()
    w._last_cycle_start = NOW - 200

    w.tick(NOW)

    assert cycle_recorder == []
    assert _cycle_triggers(project) == []


def test_headless_worktree_backlog_triggers_utilization(project, cycle_recorder):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    paths.state_file.write_text(
        json.dumps({"loops": {"code2": {"branch": "loop/demo/code2"}}}), encoding="utf-8"
    )
    _seed_loop_task(paths, "T7001", "code2")
    w = _watch(project, target_lane_utilization=1.0, min_cycle_interval_s=120)
    w._prev_snapshot = w.observer.snapshot().to_dict()
    w._last_cycle_start = NOW - 200

    w.tick(NOW)

    assert len(cycle_recorder) == 1
    assert _cycle_triggers(project)[-1]["reasons"] == ["lane-utilization-pending"]


def test_state_file_mtime_change_triggers(project, cycle_recorder):
    w = _settled(_watch(project))
    state_file = w.paths.state_file
    state_file.write_text("{}\n", encoding="utf-8")

    w.tick(time.time())

    assert len(cycle_recorder) == 1
    triggers = [e for e in _events(project) if e["event"] == "cycle-trigger"]
    assert triggers[-1]["reasons"] == ["state-changed"]

    w.tick(time.time())  # mtime unchanged -> no retrigger
    assert len(cycle_recorder) == 1


def test_drive_pending_build_live_runner_with_advanced_branch_does_not_trigger(
    project, cycle_recorder, monkeypatch
):
    w = _settled(_watch(project))
    worktree = actions._lane_worktree(w.paths, "web")
    actions.record_build_marker(
        w.paths,
        {
            "window": "web",
            "branch": "loop/demo/web",
            "pre_build_sha": "sha-a",
            "pid": 123,
            "started_at": utc_now(),
        },
    )
    monkeypatch.setattr(Substrate, "branch_head", lambda self, _worktree, _branch: "sha-b")
    monkeypatch.setattr(
        Substrate,
        "process_command",
        lambda self, pid, timeout=2: f"codex exec --cd {worktree} <brief>",
    )

    w.tick(time.time())

    assert cycle_recorder == []
    assert _cycle_triggers(project) == []


def test_drive_pending_build_gone_runner_with_pre_build_sha_triggers(
    project, cycle_recorder, monkeypatch
):
    w = _settled(_watch(project))
    actions.record_build_marker(
        w.paths,
        {
            "window": "web",
            "branch": "loop/demo/web",
            "pre_build_sha": "sha-a",
            "pid": 123,
            "started_at": utc_now(),
        },
    )
    monkeypatch.setattr(Substrate, "process_command", lambda self, pid, timeout=2: None)

    w.tick(time.time())

    assert len(cycle_recorder) == 1
    assert _cycle_triggers(project)[-1]["reasons"] == ["drive-pending"]


def test_drive_pending_build_gone_runner_without_pre_build_sha_young_does_not_trigger(
    project, cycle_recorder, monkeypatch
):
    w = _settled(_watch(project))
    actions.record_build_marker(
        w.paths,
        {
            "window": "web",
            "branch": "loop/demo/web",
            "pid": 123,
            "started_at": utc_now(),
        },
    )
    monkeypatch.setattr(Substrate, "process_command", lambda self, pid, timeout=2: None)

    w.tick(time.time())

    assert cycle_recorder == []
    assert _cycle_triggers(project) == []


def test_drive_pending_build_aged_past_timeout_triggers_without_baseline(project, cycle_recorder):
    w = _settled(_watch(project))
    actions.record_build_marker(
        w.paths,
        {
            "window": "web",
            "branch": "loop/demo/web",
            "pid": 123,
            "started_at": "2000-01-01T00:00:00Z",
        },
    )

    w.tick(time.time())

    assert len(cycle_recorder) == 1
    assert _cycle_triggers(project)[-1]["reasons"] == ["drive-pending"]


def test_drive_pending_verify_result_with_concerns_triggers(project, cycle_recorder):
    w = _settled(_watch(project))
    out_path = w.paths.verify_dir / "web-result.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"overall": "concerns"}), encoding="utf-8")
    actions.record_verify_marker(
        w.paths,
        {
            "window": "web",
            "branch": "loop/demo/web",
            "out_path": str(out_path),
            "pid": 123,
            "started_at": utc_now(),
        },
    )

    w.tick(time.time())

    assert len(cycle_recorder) == 1
    assert _cycle_triggers(project)[-1]["reasons"] == ["drive-pending"]


def test_drive_pending_verify_young_without_result_does_not_trigger(project, cycle_recorder):
    w = _settled(_watch(project))
    actions.record_verify_marker(
        w.paths,
        {
            "window": "web",
            "branch": "loop/demo/web",
            "out_path": str(w.paths.verify_dir / "missing.json"),
            "pid": 123,
            "started_at": utc_now(),
        },
    )

    w.tick(time.time())

    assert cycle_recorder == []
    assert _cycle_triggers(project) == []


def test_paused_skip_once_per_pause(project, cycle_recorder):
    w = _settled(_watch(project))
    w.paths.paused_path.touch()

    w.paths.cycle_now_path.touch()
    w.tick(time.time())
    w.paths.cycle_now_path.touch()
    w.tick(time.time())

    assert cycle_recorder == []
    skips = [e for e in _events(project) if e["event"] == "cycle-skip"]
    assert len(skips) == 1 and skips[0]["reason"] == "paused"

    w.paths.paused_path.unlink()
    w.paths.cycle_now_path.touch()
    w.tick(time.time())
    assert len(cycle_recorder) == 1


def test_pending_decision_suppresses(project, cycle_recorder):
    w = _settled(_watch(project))
    w.paths.pending_decision_path.write_text('{"id": "d-x", "status": "pending"}', "utf-8")

    w.paths.cycle_now_path.touch()
    w.tick(time.time())

    assert cycle_recorder == []
    skips = [e for e in _events(project) if e["event"] == "cycle-skip"]
    assert skips and skips[-1]["reason"] == "pending-decision"


def test_budget_guard_suppresses(project, cycle_recorder):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    log = EventLog(paths.events_path)
    for _ in range(12):  # default brain.max_calls_per_hour
        log.append("brain-call")
    w = _settled(_watch(project))

    w.paths.cycle_now_path.touch()
    w.tick(time.time())

    assert cycle_recorder == []
    skips = [e for e in _events(project) if e["event"] == "cycle-skip"]
    assert skips and skips[-1]["reason"] == "budget"


# ── asks: record, reply, timeout ────────────────────────────────────────────


def _steer_doc() -> dict:
    return {
        "id": "d-20260611-000000",
        "actions": [
            {
                "idx": 0,
                "kind": "steer",
                "lane": "web",
                "payload": "report status",
                "interrupt": False,
                "wait_for_idle": False,
                "expects_reply": True,
                "reply_timeout_s": 1800,
                "rationale": "r",
                "classification": "safe",
                "status": "approved",
            }
        ],
    }


def test_execute_batch_records_ask_then_reply_received(project, cycle_recorder):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    events = EventLog(paths.events_path)

    actions.execute_batch(_steer_doc(), Substrate(project, "demo"), events, EngineConfig(), paths)

    asks = actions.load_asks(paths)
    assert len(asks) == 1
    ask = asks[0]
    assert ask["id"] == "d-20260611-000000-0"
    assert ask["lane"] == "web"
    assert ask["status"] == "outstanding"
    assert ask["reply_timeout_s"] == 1800 and ask["created_at"]
    ask_events = [e for e in _events(project) if e["event"] == "ask"]
    assert ask_events and ask_events[0]["id"] == "d-20260611-000000-0"

    w = _settled(_watch(project))
    reply = paths.mailbox_dir / "20260611-000100-web-to-coord.md"
    reply.write_text(
        "---\nsubject: re:d-20260611-000000-0\nfrom: web\nto: coord\n---\n\ndone.\n",
        encoding="utf-8",
    )
    w.tick(time.time())

    assert len(cycle_recorder) == 1  # matched reply counts as a trigger
    triggers = [e for e in _events(project) if e["event"] == "cycle-trigger"]
    assert triggers[-1]["reasons"] == ["reply-received"]
    assert actions.load_asks(paths)[0]["status"] == "replied"
    received = [e for e in _events(project) if e["event"] == "reply-received"]
    assert received and received[0]["ask"] == "d-20260611-000000-0"
    assert reply.exists()  # peek only — the docs lane owns the ack

    w.tick(time.time())  # same file again -> no duplicate reply-received
    assert len([e for e in _events(project) if e["event"] == "reply-received"]) == 1


def test_unmatched_reply_subject_is_ignored(project, cycle_recorder):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    actions.record_ask(paths, "d-1-0", "web", 1800)
    w = _settled(_watch(project))
    reply = paths.mailbox_dir / "20260611-000200-web-to-coord.md"
    reply.write_text("---\nsubject: unrelated update\n---\n\nbody\n", encoding="utf-8")

    w.tick(time.time())

    assert cycle_recorder == []
    assert actions.load_asks(paths)[0]["status"] == "outstanding"


def test_ask_timeout_marks_once(project, cycle_recorder):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    actions.save_asks(
        paths,
        [
            {
                "id": "d-1-0",
                "lane": "web",
                "created_at": "2026-06-10T00:00:00Z",  # injected old timestamp
                "reply_timeout_s": 60,
                "status": "outstanding",
            }
        ],
    )
    w = _settled(_watch(project))

    w.tick(time.time())
    w.tick(time.time())

    assert cycle_recorder == []  # timeout is not a cycle trigger
    asks = actions.load_asks(paths)
    assert asks[0]["status"] == "timed-out" and asks[0]["timed_out_at"]
    timeouts = [e for e in _events(project) if e["event"] == "reply-timeout"]
    assert len(timeouts) == 1 and timeouts[0]["ask"] == "d-1-0"


def test_checkpoint_prompt_lists_outstanding_asks(project):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    actions.record_ask(paths, "d-2-0", "web", 1800)

    assert run_once(project, "demo", EngineConfig()) == 0

    prompts = sorted(paths.brain_dir.glob("*.prompt.md"))
    text = prompts[0].read_text(encoding="utf-8")
    assert "--- outstanding asks ---" in text
    assert "d-2-0 lane=web status=outstanding" in text


# ── scheduled triggers: metrics after cycle, stale-lint dispatch ────────────


def _lint_calls(call_log) -> list[str]:
    return [line for line in call_log() if line.startswith("loop-wiki-lint")]


def test_metrics_logged_after_each_cycle(project, cycle_recorder, call_log):
    w = _watch(project, metrics=MetricsConfig(log_after_cycle=True))
    w._last_cycle_start = time.time()

    w.tick(time.time())  # first delta sees the pending mailbox file -> cycle

    assert len(cycle_recorder) == 1
    metrics_calls = [line for line in call_log() if line.startswith("loop-metrics")]
    assert metrics_calls == [f"loop-metrics --session demo --project-root {project} --log"]
    metrics_events = [e for e in _events(project) if e["event"] == "metrics"]
    assert metrics_events and metrics_events[0]["after_cycle"] is True
    assert metrics_events[0]["autonomy_ratio"] == 0.75
    assert metrics_events[0]["interventions_per_shipped_unit"] == 1.5
    assert metrics_events[0]["escalations_7d"] == 2
    seq = {e["event"]: e["seq"] for e in _events(project)}
    assert seq["cycle-result"] < seq["metrics"]


def test_metrics_disabled_by_default_and_failure_is_an_event(project, cycle_recorder, monkeypatch):
    w = _watch(project)
    w._last_cycle_start = time.time()
    w.tick(time.time())
    assert "metrics" not in [e["event"] for e in _events(project)]

    monkeypatch.setenv("FAKE_METRICS_FAIL", "1")
    w2 = _watch(project, metrics=MetricsConfig(log_after_cycle=True))
    w2.paths.cycle_now_path.touch()
    w2.tick(time.time())

    assert len(cycle_recorder) == 2  # the failed metrics run never kills the daemon
    errors = [e for e in _events(project) if e["event"] == "error"]
    assert errors and errors[-1]["kind"] == "metrics-failed"


def test_lint_dispatched_when_stale_at_most_once_per_interval(project, cycle_recorder, call_log):
    w = _settled(_watch(project, lint=LintConfig(enabled=True)))

    w.tick(time.time())  # no log.md at all -> the lint run is overdue

    assert _lint_calls(call_log) == [
        f"loop-wiki-lint --dispatch --session demo --project-root {project}"
    ]
    dispatches = [e for e in _events(project) if e["event"] == "lint-dispatch"]
    assert len(dispatches) == 1 and dispatches[0]["ok"] is True

    w.tick(time.time())  # inside the interval -> no second dispatch
    assert len(_lint_calls(call_log)) == 1


def test_lint_not_dispatched_when_recent_disabled_or_paused(project, call_log):
    log_md = project / "ops-wiki" / "log.md"
    today = time.strftime("%Y-%m-%d", time.gmtime())
    log_md.write_text(f"## [{today}] lint | 5 pages, 0 findings\n", encoding="utf-8")
    w = _settled(_watch(project, lint=LintConfig(enabled=True)))
    w.tick(time.time())
    assert _lint_calls(call_log) == []  # fresh lint entry in log.md

    log_md.unlink()
    w2 = _settled(_watch(project))  # lint disabled by default
    w2.tick(time.time())
    assert _lint_calls(call_log) == []

    w3 = _settled(_watch(project, lint=LintConfig(enabled=True)))
    w3.paths.paused_path.touch()
    w3.tick(time.time())
    assert _lint_calls(call_log) == []  # paused engines do not start lint lanes


def test_lint_dispatch_failure_latches_for_the_interval(project, monkeypatch, call_log):
    monkeypatch.setenv("FAKE_LINT_FAIL", "1")
    w = _settled(_watch(project, lint=LintConfig(enabled=True)))

    w.tick(time.time())
    w.tick(time.time())

    assert len(_lint_calls(call_log)) == 1  # the failed attempt is the per-interval latch
    dispatches = [e for e in _events(project) if e["event"] == "lint-dispatch"]
    assert len(dispatches) == 1 and dispatches[0]["ok"] is False


# ── headless ingest ─────────────────────────────────────────────────────────


def test_headless_ingest_done(project, monkeypatch, tmp_path, call_log):
    marker = tmp_path / "ingested"
    script = tmp_path / "fake-ingest"
    script.write_text(f'#!/bin/sh\n: > "{marker}"\necho done\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("LOOP_ENGINE_INGEST_CMD", str(script))
    config = EngineConfig(ingest=IngestConfig(mode="headless"))

    assert run_once(project, "demo", config) == 0

    assert marker.exists()
    done = [e for e in _events(project) if e["event"] == "ingest-done"]
    assert done and done[0]["pending"] == 2  # fake loop-wiki-pending prints 2
    # the one-shot got the protocol section + pending list (and only that section)
    paths = SessionPaths(project, "demo")
    prompts = sorted((paths.engine_dir / "ingest").glob("*.prompt.md"))
    text = prompts[0].read_text(encoding="utf-8")
    assert "### Ingest protocol" in text
    assert "Move each processed file" in text
    assert "not part of the protocol section" not in text
    assert "20260610-000000-web-to-coord.md" in text
    # headless mode never nudges the docs lane
    assert not any(" docs " in line for line in call_log() if line.startswith("loop-dispatch"))


def test_headless_ingest_timeout(project, monkeypatch, tmp_path):
    script = tmp_path / "slow-ingest"
    script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("LOOP_ENGINE_INGEST_CMD", str(script))
    config = EngineConfig(ingest=IngestConfig(mode="headless", timeout_s=1))

    assert run_once(project, "demo", config) == 0  # the cycle survives the failure

    kinds = [e["event"] for e in _events(project)]
    assert "ingest-timeout" in kinds
    assert "ingest-done" not in kinds


def test_ingest_argv_appends_auto_approve(monkeypatch):
    monkeypatch.delenv("LOOP_ENGINE_INGEST_CMD", raising=False)

    class StubSub:
        def oneshot_template(self, name):
            assert name == "claude"
            return "claude -p {prompt}"

        def harness_field(self, name, field):
            assert (name, field) == ("claude", "auto_approve_flag")
            return "--dangerously-skip-permissions"

    argv = loop_mod._ingest_argv(StubSub(), EngineConfig(), "P")
    assert argv == ["claude", "-p", "P", "--dangerously-skip-permissions"]

    no_auto = EngineConfig(ingest=IngestConfig(auto_approve=False))
    assert loop_mod._ingest_argv(StubSub(), no_auto, "P") == ["claude", "-p", "P"]

    other = EngineConfig(ingest=IngestConfig(harness="codex", auto_approve=False))

    class CodexStub:
        def oneshot_template(self, name):
            assert name == "codex"  # ingest.harness overrides brain.harness
            return "codex exec {prompt}"

    assert loop_mod._ingest_argv(CodexStub(), other, "P") == ["codex", "exec", "P"]


# ── CLI: status liveness ────────────────────────────────────────────────────


def test_status_reports_watch_liveness(project, monkeypatch, capsys):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    base = ["--project-root", str(project), "--session", "demo", "status"]

    assert cli.main(base) == 0
    assert "watch:" not in capsys.readouterr().out  # no pid file -> no watch line

    paths.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert cli.main(base) == 0
    assert f"watch: alive (pid {os.getpid()}" in capsys.readouterr().out

    monkeypatch.setattr(cli, "pid_alive", lambda pid: False)
    assert cli.main(base) == 0
    assert "watch: not running" in capsys.readouterr().out


def test_fresh_start_does_not_fire_interval_immediately():
    # A daemon that has never cycled must not burn a brain call at boot: the
    # interval baseline is the watch start, not epoch zero.
    fresh = _state(last_cycle_start=None, watch_start=NOW - 10)
    assert evaluate_triggers(fresh, NOW) == []
    due = _state(last_cycle_start=None, watch_start=NOW - 901)
    assert evaluate_triggers(due, NOW) == ["interval"]


def test_cycle_now_survives_suppression_and_fires_after_resolution(project, cycle_recorder):
    w = _settled(_watch(project))
    w.paths.pending_decision_path.write_text('{"id": "d-x", "status": "pending"}', "utf-8")
    w.paths.cycle_now_path.touch()

    w.tick(time.time())
    assert cycle_recorder == []
    assert w.paths.cycle_now_path.exists()  # request survives suppression

    w.paths.pending_decision_path.unlink()
    w.tick(time.time())
    assert len(cycle_recorder) == 1
    assert not w.paths.cycle_now_path.exists()  # consumed by the real cycle


def test_cycle_skip_logged_once_per_episode(project, cycle_recorder):
    w = _settled(_watch(project))
    w.paths.pending_decision_path.write_text('{"id": "d-x", "status": "pending"}', "utf-8")
    w.paths.cycle_now_path.touch()

    w.tick(time.time())
    w.tick(time.time())
    w.tick(time.time())

    skips = [e for e in _events(project) if e["event"] == "cycle-skip"]
    assert len(skips) == 1  # identical episode, one event — not one per poll


# ── OPS GUARD A: restart (confirm-dead-before-start singleton trap) ──────────


def test_restart_stops_then_starts_when_old_daemon_exits(project, monkeypatch):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    paths.pid_path.write_text("4242\n", encoding="utf-8")

    # the old pid is alive until SIGTERM, then exits (poll observes the death)
    alive = {"4242": True}
    monkeypatch.setattr(watch_mod, "pid_alive", lambda pid: alive.get(str(pid), False))

    def fake_kill(pid, sig):
        alive["4242"] = False  # SIGTERM lands -> the daemon exits

    monkeypatch.setattr(watch_mod.os, "kill", fake_kill)
    started: list[bool] = []
    monkeypatch.setattr(watch_mod.Watch, "run", lambda self: started.append(True) or 0)

    rc = watch_mod.restart(project, "demo", EngineConfig(), timeout_s=2.0)

    assert rc == 0
    assert started == [True]  # exactly one new daemon started
    kinds = [e["event"] for e in _events(project)]
    assert "watch-stop-requested" in kinds and "watch-stopped" in kinds


def test_restart_refuses_second_instance_when_old_will_not_die(project, monkeypatch, capsys):
    paths = SessionPaths(project, "demo")
    paths.ensure()
    paths.pid_path.write_text("5555\n", encoding="utf-8")

    # the old daemon never dies, even after SIGTERM
    monkeypatch.setattr(watch_mod, "pid_alive", lambda pid: True)
    monkeypatch.setattr(watch_mod.os, "kill", lambda pid, sig: None)
    started: list[bool] = []
    monkeypatch.setattr(watch_mod.Watch, "run", lambda self: started.append(True) or 0)

    rc = watch_mod.restart(project, "demo", EngineConfig(), timeout_s=0.2)

    assert rc == 1
    assert started == []  # the singleton trap: NEVER start a second instance
    assert "NOT starting a second instance" in capsys.readouterr().err
    kinds = [e["event"] for e in _events(project)]
    assert "watch-stop-timeout" in kinds


def test_restart_starts_directly_when_nothing_running(project, monkeypatch):
    # no pid file at all -> stop_daemon is a no-op, watch starts immediately
    monkeypatch.setattr(watch_mod, "pid_alive", lambda pid: False)
    started: list[bool] = []
    monkeypatch.setattr(watch_mod.Watch, "run", lambda self: started.append(True) or 0)

    assert watch_mod.restart(project, "demo", EngineConfig(), timeout_s=1.0) == 0
    assert started == [True]


def test_restart_cli_wires_timeout(project, monkeypatch):
    monkeypatch.setattr(watch_mod, "pid_alive", lambda pid: False)
    captured: dict = {}

    def fake_restart(root, session, config, timeout_s):
        captured["timeout"] = timeout_s
        return 0

    monkeypatch.setattr(cli, "restart", fake_restart)
    rc = cli.main(
        ["--project-root", str(project), "--session", "demo", "restart", "--timeout", "5"]
    )
    assert rc == 0 and captured["timeout"] == 5.0


# ── OPS GUARD B: quota-aware backoff ────────────────────────────────────────


def test_reset_deadline_parsing():
    now = time.mktime(time.struct_time((2026, 6, 11, 8, 0, 0, 0, 0, -1)))  # 8:00 local
    # "resets 9:30pm" -> a future epoch later the same day
    deadline = watch_mod.parse_reset_deadline("usage limit; resets 9:30pm", now)
    assert deadline is not None and deadline > now
    parsed = time.localtime(deadline)
    assert (parsed.tm_hour, parsed.tm_min) == (21, 30)
    # already past today -> rolls to tomorrow
    past = watch_mod.parse_reset_deadline("resets 7am", now)
    assert past is not None and past > now
    # no hint -> None (caller falls back to config minutes)
    assert watch_mod.parse_reset_deadline("generic error, no reset", now) is None


def test_quota_failure_sets_backoff_and_next_tick_skips(project, monkeypatch):
    # a brain that fails with a quota message -> the cycle ends rc=1 quota
    quota_brain = project / "quota-brain"
    quota_brain.write_text(
        '#!/bin/sh\necho "Claude usage limit reached; resets 9:30pm" >&2\nexit 1\n',
        encoding="utf-8",
    )
    quota_brain.chmod(0o755)
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(quota_brain))

    w = _watch(project)  # real run_once this time (not the cycle_recorder)
    w._last_cycle_start = time.time()

    now = time.time()
    w.tick(now)  # mailbox-new trigger -> a real cycle -> brain quota failure

    assert w._quota_backoff_until is not None and w._quota_backoff_until > now
    set_events = [e for e in _events(project) if e["event"] == "quota-backoff-set"]
    assert set_events and set_events[-1]["source"] == "stderr-reset"

    # the NEXT tick is gated: brain suppressed, cycle-skip reason=quota-backoff
    w.paths.cycle_now_path.touch()
    w.tick(time.time())
    skips = [e for e in _events(project) if e["event"] == "cycle-skip"]
    assert skips and skips[-1]["reason"] == "quota-backoff"


def test_quota_backoff_clears_after_deadline(project, cycle_recorder):
    w = _settled(_watch(project))
    w._quota_backoff_until = time.time() - 1  # already elapsed

    w.paths.cycle_now_path.touch()
    w.tick(time.time())

    assert w._quota_backoff_until is None  # cleared
    cleared = [e for e in _events(project) if e["event"] == "quota-backoff-cleared"]
    assert cleared
    assert len(cycle_recorder) == 1  # the brain runs again once the window passed


def test_quota_backoff_falls_back_to_config_minutes(project, monkeypatch):
    quota_brain = project / "quota-brain2"
    quota_brain.write_text(
        '#!/bin/sh\necho "quota exceeded, no reset hint here" >&2\nexit 1\n', encoding="utf-8"
    )
    quota_brain.chmod(0o755)
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(quota_brain))
    from loop_orchestrator.engine.config import BrainConfig

    w = _watch(project, brain=BrainConfig(quota_backoff_minutes=30))
    w._last_cycle_start = time.time()

    now = time.time()
    w.tick(now)

    assert w._quota_backoff_until is not None
    # ~30 minutes out (config default), give a generous tolerance for clock drift
    assert 25 * 60 < (w._quota_backoff_until - now) < 35 * 60
    set_events = [e for e in _events(project) if e["event"] == "quota-backoff-set"]
    assert set_events and set_events[-1]["source"] == "config-default"


def test_model_unavailable_failure_sets_backoff_and_next_tick_skips(project, monkeypatch):
    # F3 (T0018): the model-unavailable notice prints to STDOUT, then exit 1.
    # The cycle ends rc=1 model-unavailable -> arm the brain backoff (no
    # retry-into-the-wall) and surface a distinct, human-actionable event.
    bad_brain = project / "model-down-brain"
    bad_brain.write_text(
        '#!/bin/sh\necho "Error: model claude-fable-5 is currently unavailable"\nexit 1\n',
        encoding="utf-8",
    )
    bad_brain.chmod(0o755)
    monkeypatch.setenv("LOOP_ENGINE_BRAIN_CMD", str(bad_brain))

    w = _watch(project)  # real run_once
    w._last_cycle_start = time.time()

    now = time.time()
    w.tick(now)  # mailbox-new trigger -> real cycle -> model-unavailable failure

    assert w._quota_backoff_until is not None and w._quota_backoff_until > now
    set_events = [e for e in _events(project) if e["event"] == "model-unavailable-backoff-set"]
    assert set_events  # distinct from quota-backoff-set
    assert "model_failover" in set_events[-1]  # declared failover surfaced (may be "")

    # the NEXT tick is gated with a model-unavailable-specific skip reason.
    w.paths.cycle_now_path.touch()
    w.tick(time.time())
    skips = [e for e in _events(project) if e["event"] == "cycle-skip"]
    assert skips and skips[-1]["reason"] == "model-unavailable-backoff"


# ── OPS GUARD C: stale-daemon warning ───────────────────────────────────────


def test_status_surfaces_stale_daemon_warning(project, monkeypatch, capsys):
    import json as _json

    paths = SessionPaths(project, "demo")
    paths.ensure()
    paths.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)

    # daemon recorded an OLD module mtime; the on-disk module is newer now
    from loop_orchestrator.engine import gate as gate_mod

    module_mtime = os.stat(gate_mod.__file__).st_mtime_ns
    paths.daemon_build_path.write_text(
        _json.dumps({"pid": os.getpid(), "module_mtime_ns": module_mtime - 10_000_000_000}),
        encoding="utf-8",
    )

    rc = cli.main(["--project-root", str(project), "--session", "demo", "status"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "running stale code" in out and "loop-engine restart" in out
    assert "daemon-stale" in [e["event"] for e in _events(project)]


def test_status_no_warning_when_daemon_is_current(project, monkeypatch, capsys):
    import json as _json

    paths = SessionPaths(project, "demo")
    paths.ensure()
    paths.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)

    from loop_orchestrator.engine import gate as gate_mod

    module_mtime = os.stat(gate_mod.__file__).st_mtime_ns
    # recorded mtime EQUAL to on-disk -> not stale
    paths.daemon_build_path.write_text(
        _json.dumps({"pid": os.getpid(), "module_mtime_ns": module_mtime}), encoding="utf-8"
    )

    assert cli.main(["--project-root", str(project), "--session", "demo", "status"]) == 0
    assert "running stale code" not in capsys.readouterr().out


def test_record_daemon_build_stamps_module_mtime(project):
    paths = SessionPaths(project, "demo")
    paths.ensure()

    watch_mod.record_daemon_build(paths, pid=1234)

    import json as _json

    build = _json.loads(paths.daemon_build_path.read_text(encoding="utf-8"))
    assert build["pid"] == 1234 and isinstance(build["module_mtime_ns"], int)
    assert build["started_at"]
    # a freshly-recorded build is, by construction, NOT stale
    assert watch_mod.stale_daemon_warning(paths) is None
