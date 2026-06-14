from __future__ import annotations

import json

import pytest

from loop_orchestrator.engine.observe import (
    FANOUT_BASE_TIMEOUT_S,
    FANOUT_TIMEOUT_CAP_S,
    EngineSnapshot,
    Observer,
    adaptive_timeout,
    delta,
    load_last_snapshot,
)
from loop_orchestrator.paths import SessionPaths
from loop_orchestrator.substrate import LaneStatus, SubstrateError


class StubSubstrate:
    """Canned substrate — no subprocess anywhere near this test."""

    def __init__(self):
        self.statuses = {
            "web": LaneStatus("web", "working", "demo:1.1", "fixed"),
            "docs": LaneStatus("docs", "idle", "demo:2.1", "fixed"),
        }
        self.pending = [{"file": "20260610-120000-web-to-coord.md", "from": "web", "to": "coord"}]
        self.prompt: str | None = "x" * 400
        self.fanout_fails = False  # F7: simulate a slow/failed all-lanes fan-out

    def lane_status_all(self):
        if self.fanout_fails:
            raise SubstrateError(
                ["loop-lane-status", "--json", "--all"], None, "timed out after 30s"
            )
        return dict(self.statuses)

    def digest(self):
        return {
            "contract_version": 1,
            "generated_at": "2026-06-10T12:00:00Z",
            "state": {"loops": {"L1": {"status": "active", "branch": "feat/x"}}},
            "mailbox": {"pending": list(self.pending), "processed_count": 3},
        }

    def checkpoint_prompt(self):
        if self.prompt is None:
            raise SubstrateError(["loop-checkpoint"], 1, "boom")
        return self.prompt


def test_snapshot_shape_and_file(tmp_path):
    paths = SessionPaths(tmp_path, "demo")
    paths.ensure()
    paths.lane_restarts.write_text(
        "".join(json.dumps({"event": "restart", "n": i}) + "\n" for i in range(12)),
        encoding="utf-8",
    )
    snap = Observer(StubSubstrate(), paths).snapshot()

    assert snap.lanes == {
        "web": {"status": "working", "target": "demo:1.1", "kind": "fixed"},
        "docs": {"status": "idle", "target": "demo:2.1", "kind": "fixed"},
    }
    assert snap.loops == {"L1": {"status": "active", "branch": "feat/x"}}
    assert snap.mailbox_pending == ["20260610-120000-web-to-coord.md"]
    assert snap.processed_count == 3
    assert len(snap.restarts_tail) == 10
    assert snap.restarts_tail[-1] == {"event": "restart", "n": 11}
    assert snap.checkpoint_tokens == 100
    assert snap.generated_at

    on_disk = json.loads(paths.snapshot_path.read_text(encoding="utf-8"))
    assert on_disk["contract_version"] == 1
    assert on_disk["lanes"] == snap.lanes
    assert on_disk["mailbox_pending"] == snap.mailbox_pending
    assert on_disk["checkpoint_tokens"] == 100


def test_snapshot_tolerates_missing_pieces(tmp_path):
    paths = SessionPaths(tmp_path, "demo")
    paths.ensure()
    stub = StubSubstrate()
    stub.prompt = None  # checkpoint script unavailable
    snap = Observer(stub, paths).snapshot()
    assert snap.checkpoint_tokens is None
    assert snap.restarts_tail == []  # lane-restarts.jsonl missing


def test_delta_status_change_and_new_mailbox(tmp_path):
    paths = SessionPaths(tmp_path, "demo")
    paths.ensure()
    stub = StubSubstrate()
    prev = Observer(stub, paths).snapshot().to_dict()

    stub.statuses["web"] = LaneStatus("web", "idle", "demo:1.1", "fixed")
    stub.pending.append({"file": "20260610-130000-docs-to-coord.md"})
    cur = Observer(stub, paths).snapshot().to_dict()

    events = delta(prev, cur)
    assert ("lane-status", {"lane": "web", "from": "working", "to": "idle"}) in events
    assert ("mailbox-new", {"file": "20260610-130000-docs-to-coord.md"}) in events
    assert len(events) == 2

    assert delta(cur, cur) == []
    first = delta(None, cur)
    assert ("lane-status", {"lane": "docs", "from": None, "to": "idle"}) in first


# ── F7 (T0029): observe graceful degradation on a slow/failed fan-out ─────────


def test_adaptive_timeout_scales_with_lane_count_and_caps():
    assert adaptive_timeout(0) == FANOUT_BASE_TIMEOUT_S  # no lanes => base
    assert adaptive_timeout(-3) == FANOUT_BASE_TIMEOUT_S  # never below base
    assert adaptive_timeout(6) == FANOUT_BASE_TIMEOUT_S + 5.0 * 6  # scales up
    assert adaptive_timeout(10_000) == FANOUT_TIMEOUT_CAP_S  # bounded


def test_from_dict_roundtrips_via_to_dict(tmp_path):
    paths = SessionPaths(tmp_path, "demo")
    paths.ensure()
    snap = Observer(StubSubstrate(), paths).snapshot()
    rebuilt = EngineSnapshot.from_dict(snap.to_dict())
    assert rebuilt == snap  # contract_version dropped, every field preserved


def test_load_last_snapshot_missing_or_corrupt_returns_none(tmp_path):
    paths = SessionPaths(tmp_path, "demo")
    paths.ensure()
    assert load_last_snapshot(paths.snapshot_path) is None  # never written
    paths.snapshot_path.write_text("{not json", encoding="utf-8")
    assert load_last_snapshot(paths.snapshot_path) is None  # corrupt


def test_snapshot_or_stale_returns_fresh_on_success(tmp_path):
    paths = SessionPaths(tmp_path, "demo")
    paths.ensure()
    snap, stale, age_s = Observer(StubSubstrate(), paths).snapshot_or_stale()
    assert stale is False and age_s is None
    assert snap.lanes["web"]["status"] == "working"
    assert paths.snapshot_path.exists()  # fresh snapshot persisted as today


def test_snapshot_or_stale_falls_back_to_last_on_fanout_failure(tmp_path):
    paths = SessionPaths(tmp_path, "demo")
    paths.ensure()
    stub = StubSubstrate()
    fresh, _, _ = Observer(stub, paths).snapshot_or_stale()  # seed snapshot.json

    stub.fanout_fails = True  # the next fan-out times out under load
    snap, stale, age_s = Observer(stub, paths).snapshot_or_stale()

    assert stale is True  # reused, cycle can proceed — NOT aborted
    assert snap.lanes == fresh.lanes  # the last good state
    assert age_s is not None and age_s >= 0.0


def test_snapshot_or_stale_raises_without_prior_snapshot(tmp_path):
    paths = SessionPaths(tmp_path, "demo")
    paths.ensure()
    stub = StubSubstrate()
    stub.fanout_fails = True  # fails on the very first observe — nothing to reuse
    with pytest.raises(SubstrateError):
        Observer(stub, paths).snapshot_or_stale()
