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
