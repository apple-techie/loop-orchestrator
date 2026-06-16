"""deck.model: pure state assembly from a stub substrate + real files."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from loop_orchestrator.deck import model
from loop_orchestrator.paths import SessionPaths
from loop_orchestrator.substrate import LaneInfo, LaneStatus

LANES = [
    LaneInfo(window="coord", harness="claude", model=None, role="coordinator", cmd=None, base=True),
    LaneInfo(window="web", harness="claude", model="opus", role="impl", cmd=None, base=True),
    LaneInfo(window="helper", harness="claude", model=None, role="impl", cmd="claude", base=False),
]

STATUSES = {
    "coord": LaneStatus(lane="coord", status="idle", target="demo:coord.1", kind="fixed"),
    "web": LaneStatus(lane="web", status="working", target="demo:web.1", kind="fixed"),
    "helper": LaneStatus(lane="helper", status="errored", target="%7", kind="dynamic"),
}

DIGEST = {
    "contract_version": 1,
    "state": {
        "schema_version": 2,
        "loops": {"loop-a": {"status": "implement", "branch": "loop/a", "name": "demo loop"}},
    },
    "mailbox": {
        "pending": [{"file": "m.md", "from": "web", "to": "coord", "subject": "hello", "mtime": 1}],
        "processed_count": 4,
    },
    "unpushed": [],
    "adrs": [{"id": "0001", "status": "proposed", "title": "t", "path": "docs/adr/0001-t.md"}],
}

PENDING = {
    "contract_version": 1,
    "id": "d-20260610-120000",
    "status": "pending",
    "approval_mode": "manual",
    "critique": "web idle",
    "actions": [],
}


class StubSubstrate:
    def __init__(self):
        self.calls: list[str] = []

    def lanes(self):
        self.calls.append("lanes")
        return list(LANES)

    def lane_status_all(self):
        self.calls.append("lane_status_all")
        return dict(STATUSES)

    def digest(self):
        self.calls.append("digest")
        return json.loads(json.dumps(DIGEST))


@pytest.fixture
def paths(tmp_path):
    p = SessionPaths(tmp_path, "demo")
    p.ensure()
    return p


def _by_window(state):
    return {lane.window: lane for lane in state.lanes}


def test_load_state_without_snapshot_uses_substrate_statuses(paths):
    stub = StubSubstrate()
    state = model.load_state(stub, paths)
    assert "lane_status_all" in stub.calls
    lanes = _by_window(state)
    assert lanes["web"].status == "working"
    assert lanes["helper"].status == "errored"
    assert lanes["helper"].kind == "dynamic" and not lanes["helper"].base
    assert lanes["coord"].base


def test_load_state_fresh_snapshot_wins(paths):
    snap = {
        "contract_version": 1,
        "lanes": {
            "coord": {"status": "idle", "target": "demo:coord.1", "kind": "fixed"},
            "web": {"status": "awaiting-approval", "target": "demo:web.1", "kind": "fixed"},
        },
    }
    paths.snapshot_path.write_text(json.dumps(snap), encoding="utf-8")
    stub = StubSubstrate()
    state = model.load_state(stub, paths, prefer_snapshot_age_s=10)
    assert "lane_status_all" not in stub.calls  # snapshot is fresh -> no status spawn
    lanes = _by_window(state)
    assert lanes["web"].status == "awaiting-approval"
    assert lanes["helper"].status == "unknown"  # not in snapshot -> unknown


def test_load_state_stale_snapshot_falls_back(paths):
    snap = {"contract_version": 1, "lanes": {"web": {"status": "idle", "kind": "fixed"}}}
    paths.snapshot_path.write_text(json.dumps(snap), encoding="utf-8")
    old = time.time() - 60
    os.utime(paths.snapshot_path, (old, old))
    stub = StubSubstrate()
    state = model.load_state(stub, paths, prefer_snapshot_age_s=10)
    assert "lane_status_all" in stub.calls
    assert _by_window(state)["web"].status == "working"


def test_engine_off_without_files(paths):
    assert model.engine_state(paths) == "off"


def test_engine_paused_beats_running(paths):
    paths.pid_path.write_text(str(os.getpid()), encoding="utf-8")
    paths.paused_path.touch()
    assert model.engine_state(paths) == "paused"


def test_engine_running_with_live_pid(paths):
    paths.pid_path.write_text(str(os.getpid()), encoding="utf-8")
    assert model.engine_state(paths) == "running"


def test_engine_off_with_dead_pid(paths):
    proc = subprocess.Popen(["true"])
    proc.wait()
    paths.pid_path.write_text(str(proc.pid), encoding="utf-8")
    assert model.engine_state(paths) == "off"


def test_engine_off_with_garbage_pid_file(paths):
    paths.pid_path.write_text("not-a-pid", encoding="utf-8")
    assert model.engine_state(paths) == "off"


def test_restart_counts(paths):
    lines = [
        # restart_pane writes no "event" field; lifecycle events carry one.
        json.dumps({"timestamp": "t", "session": "demo", "lane": "web", "cmd": "claude"}),
        json.dumps({"timestamp": "t", "session": "demo", "lane": "web", "cmd": "claude"}),
        json.dumps({"timestamp": "t", "event": "restart", "lane": "coord"}),
        json.dumps({"timestamp": "t", "event": "giving-up", "lane": "web"}),
        "{corrupt",
    ]
    paths.lane_restarts.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert model.restart_counts(paths) == {"web": 2, "coord": 1}
    stub = StubSubstrate()
    state = model.load_state(stub, paths)
    lanes = _by_window(state)
    assert lanes["web"].restarts == 2
    assert lanes["helper"].restarts == 0


def test_pending_decision_loaded(paths):
    paths.pending_decision_path.write_text(json.dumps(PENDING), encoding="utf-8")
    state = model.load_state(StubSubstrate(), paths)
    assert state.pending is not None and state.pending["id"] == PENDING["id"]
    assert state.pending_unresolved is not None


def test_resolved_decision_is_not_unresolved(paths):
    doc = dict(PENDING, status="approved")
    paths.pending_decision_path.write_text(json.dumps(doc), encoding="utf-8")
    state = model.load_state(StubSubstrate(), paths)
    assert state.pending is not None
    assert state.pending_unresolved is None


def _write_task(paths, name: str, **frontmatter) -> None:
    lines = ["---"]
    lines += [f"{key}: {value}" for key, value in frontmatter.items()]
    lines += ["---", "", "body"]
    path = paths.tasks_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_review_items_only_review_status(paths):
    _write_task(
        paths, "T0052-add-panel.md", id="T0052", title="add panel", status="review", jira="SCRUM-52"
    )
    _write_task(paths, "T0040-earlier.md", id="T0040", title="earlier review", status="review")
    _write_task(paths, "T0051-wip.md", id="T0051", title="in flight", status="in-progress")
    _write_task(paths, "T0001-open.md", id="T0001", title="todo", status="open")
    _write_task(paths, "T0009-done.md", id="T0009", title="finished", status="done")
    paths.tasks_dir.joinpath("README.md").write_text("not a task\n", encoding="utf-8")
    # An archived review task must NOT surface — review items live in tasks/ only.
    (paths.tasks_dir / "archive").mkdir(parents=True, exist_ok=True)
    _write_task(paths, "archive/T0030-archived.md", id="T0030", title="archived", status="review")

    items = model.review_items(paths)

    assert [r.id for r in items] == ["T0040", "T0052"]  # sorted by id, review-only
    by_id = {r.id: r for r in items}
    assert by_id["T0052"].title == "add panel"
    assert by_id["T0052"].jira == "SCRUM-52"
    assert by_id["T0040"].jira == ""  # missing jira -> empty string


def test_review_items_empty_without_tasks_dir(paths):
    assert model.review_items(paths) == []


def test_review_items_in_load_state(paths):
    _write_task(
        paths, "T0052-add-panel.md", id="T0052", title="add panel", status="review", jira="SCRUM-52"
    )
    state = model.load_state(StubSubstrate(), paths)
    assert [r.id for r in state.review_items] == ["T0052"]


def test_digest_join_and_events_tail(paths):
    events = [
        {"ts": "2026-06-10T00:00:00Z", "seq": i, "event": "observe", "lanes": 3} for i in range(25)
    ]
    paths.events_path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    state = model.load_state(StubSubstrate(), paths)
    assert [loop.id for loop in state.loops] == ["loop-a"]
    assert state.loops[0].branch == "loop/a" and state.loops[0].name == "demo loop"
    assert state.mailbox_pending[0]["subject"] == "hello"
    assert state.processed_count == 4
    assert state.adrs[0]["id"] == "0001"
    assert len(state.events_tail) == 20  # tail(20)
    assert state.events_tail[-1]["seq"] == 24
    assert state.session == "demo"
