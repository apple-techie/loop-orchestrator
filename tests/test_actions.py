"""actions.execute — drop_lane handoff-breadcrumb flush (T0023).

The flush leaves an append-only `## Handoff state` breadcrumb on the lane page,
but ONLY for a verified-idle agent lane; non-agent / unknown-harness /
not-verified-idle lanes SKIP. The teardown always proceeds.
"""

from __future__ import annotations

from loop_orchestrator.engine import actions
from loop_orchestrator.engine.config import EngineConfig
from loop_orchestrator.engine.events import EventLog
from loop_orchestrator.paths import SessionPaths
from loop_orchestrator.substrate import LaneInfo, SubstrateError

_KNOWN = {"claude", "codex", "pi", "shell", "mprocs"}


class FakeSub:
    def __init__(self, infos, statuses, pane="line one\nworking on step 3\n"):
        self._infos = infos
        self._statuses = statuses
        self._pane = pane
        self.dropped: list[str] = []

    def lanes(self):
        return self._infos

    def harness_field(self, name, field):
        if name in _KNOWN:
            return ""
        raise SubstrateError([name], 1, f"unknown harness {name}")

    def lane_status(self, lane):
        return self._statuses.get(lane, "unknown")

    def capture_pane(self, lane, lines=40):
        return self._pane

    def drop_lane(self, window):
        self.dropped.append(window)


def _info(window, harness):
    return LaneInfo(
        window=window, harness=harness, model=None, role="impl", cmd=None, base=False, kind="worker"
    )


def _env(tmp_path):
    paths = SessionPaths(tmp_path, "demo")
    paths.ensure()
    return paths, EventLog(paths.events_path)


def _drop(window):
    return {"kind": "drop_lane", "window": window, "rationale": "r"}


def _events(events):
    return [e["event"] for e in events.tail(20)]


def test_flush_writes_breadcrumb_on_idle_agent(tmp_path):
    paths, events = _env(tmp_path)
    sub = FakeSub([_info("helper", "claude")], {"helper": "idle"})
    actions.execute(_drop("helper"), sub, events, EngineConfig(), paths=paths)
    page = paths.lane_page("helper").read_text(encoding="utf-8")
    assert "## Handoff state" in page
    assert "harness: claude" in page
    assert "working on step 3" in page  # the captured pane tail
    assert sub.dropped == ["helper"]  # teardown still happened
    assert "handoff-flush" in _events(events)


def test_flush_skips_non_agent_lane(tmp_path):
    paths, events = _env(tmp_path)
    sub = FakeSub([_info("ops1", "shell")], {"ops1": "idle"})
    actions.execute(_drop("ops1"), sub, events, EngineConfig(), paths=paths)
    assert not paths.lane_page("ops1").exists()
    assert sub.dropped == ["ops1"]
    evs = _events(events)
    assert "handoff-skip" in evs and "handoff-flush" not in evs


def test_flush_skips_unknown_harness(tmp_path):
    paths, events = _env(tmp_path)
    sub = FakeSub([_info("weird", "nosuch")], {"weird": "idle"})
    actions.execute(_drop("weird"), sub, events, EngineConfig(), paths=paths)
    assert not paths.lane_page("weird").exists()
    assert sub.dropped == ["weird"]
    assert "handoff-skip" in _events(events)


def test_flush_skips_when_not_verified_idle(tmp_path):
    paths, events = _env(tmp_path)
    sub = FakeSub([_info("helper", "claude")], {"helper": "working"})
    actions.execute(_drop("helper"), sub, events, EngineConfig(), paths=paths)
    assert not paths.lane_page("helper").exists()
    assert sub.dropped == ["helper"]
    evs = _events(events)
    assert "handoff-skip" in evs and "handoff-flush" not in evs


def test_handoff_breadcrumb_is_append_only(tmp_path):
    paths, events = _env(tmp_path)
    page = paths.lane_page("helper")
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# lane: helper\n\n## Role\nworker lane\n", encoding="utf-8")
    sub = FakeSub([_info("helper", "claude")], {"helper": "idle"})
    actions.execute(_drop("helper"), sub, events, EngineConfig(), paths=paths)
    text = page.read_text(encoding="utf-8")
    assert text.startswith("# lane: helper\n\n## Role\nworker lane\n")  # original preserved
    assert "## Handoff state" in text


# ── T0026 conditional worktree provisioning on add_lane ─────────────────────


class AddLaneSub:
    def __init__(self):
        self.calls: list[dict] = []
        self.dispatched: list[tuple] = []

    def add_lane(self, window, **kwargs):
        self.calls.append({"window": window, **kwargs})

    def dispatch(self, lane, payload, **k):
        self.dispatched.append((lane, payload))


def _add(harness="claude", cmd=None, window="w", brief="b"):
    a = {"kind": "add_lane", "window": window, "harness": harness, "brief": brief, "rationale": "r"}
    if cmd:
        a["cmd"] = cmd
    return a


def test_add_lane_shared_when_sole_code_writer(tmp_path):
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    actions.execute(_add(), sub, events, EngineConfig(), paths=paths, code_writers=0)
    assert sub.calls[0]["worktree"] is False  # DORMANT at concurrency=1


def test_add_lane_worktree_when_a_peer_is_concurrent(tmp_path):
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    actions.execute(_add(), sub, events, EngineConfig(), paths=paths, code_writers=1)
    assert sub.calls[0]["worktree"] is True  # second concurrent code-writer


def test_add_lane_shell_lane_never_worktree(tmp_path):
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    actions.execute(_add(harness="shell"), sub, events, EngineConfig(), paths=paths, code_writers=5)
    assert sub.calls[0]["worktree"] is False  # not a code-writer lane


def test_add_lane_dormant_without_concurrency_signal(tmp_path):
    # cli path / no policy: code_writers=None => shared, byte-identical to today.
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    actions.execute(_add(), sub, events, EngineConfig(), paths=paths, code_writers=None)
    assert sub.calls[0]["worktree"] is False


def test_add_lane_explicit_worktree_field_still_honored(tmp_path):
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    action = {**_add(), "worktree": True}
    actions.execute(action, sub, events, EngineConfig(), paths=paths, code_writers=0)
    assert sub.calls[0]["worktree"] is True  # explicit T0025 opt-in preserved


# ── T0028 full lane-handoff (recovery brief + ack + worktree carry) ──────────

import json  # noqa: E402

from loop_orchestrator.engine import wiki  # noqa: E402


def _seed_handoff(paths, window="w"):
    page = paths.lane_page(window)
    page.parent.mkdir(parents=True, exist_ok=True)
    wiki.append_handoff(page, window, "claude", "last step: wiring the gate\n", "2026-06-14T00:00Z")


def test_handoff_ack_subject_format():
    assert actions.handoff_ack_subject("worker-3") == "re:handoff:worker-3"


def test_cold_add_brief_unchanged(tmp_path):
    # No predecessor handoff state => the dispatched brief is the original, verbatim.
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    actions.execute(_add(brief="do the thing"), sub, events, EngineConfig(), paths=paths)
    assert sub.dispatched == [("w", "do the thing")]


def test_successor_gets_recovery_brief(tmp_path):
    # A window with a `## Handoff state` page => the successor's brief is augmented.
    paths, events = _env(tmp_path)
    _seed_handoff(paths, "w")
    sub = AddLaneSub()
    actions.execute(_add(brief="do the thing"), sub, events, EngineConfig(), paths=paths)
    _, payload = sub.dispatched[0]
    assert "Handoff recovery" in payload
    assert "ops-wiki/lanes/w.md" in payload
    assert "subject: re:handoff:w" in payload  # ack instruction
    assert payload.endswith("do the thing")  # original brief preserved
    assert any(e["event"] == "handoff-recovery" for e in events.tail(10))


def test_successor_carries_predecessor_worktree(tmp_path):
    # Predecessor was worktree-isolated (branch in the ledger) => carry it.
    paths, events = _env(tmp_path)
    _seed_handoff(paths, "w")
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(
        json.dumps({"loops": {"w": {"branch": "loop/demo/w"}}}), encoding="utf-8"
    )
    sub = AddLaneSub()
    actions.execute(_add(brief="resume"), sub, events, EngineConfig(), paths=paths, code_writers=0)
    assert sub.calls[0]["worktree"] is True  # carried despite code_writers=0
    _, payload = sub.dispatched[0]
    assert "loop/demo/w" in payload  # the carried branch is named in the brief


def test_successor_without_predecessor_branch_no_carry(tmp_path):
    # Handoff state but no recorded branch (shared predecessor) => no worktree carry.
    paths, events = _env(tmp_path)
    _seed_handoff(paths, "w")
    sub = AddLaneSub()
    actions.execute(_add(brief="resume"), sub, events, EngineConfig(), paths=paths, code_writers=0)
    assert sub.calls[0]["worktree"] is False
