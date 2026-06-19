from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from loop_orchestrator.engine.decisions import (
    DecisionStateError,
    archive,
    create,
    get,
    mark_action,
    resolve,
)
from loop_orchestrator.engine.decision import VerifyAction, parse_and_validate
from loop_orchestrator.paths import SessionPaths


@dataclass
class StubAction:
    kind: str
    rationale: str
    lane: str | None = None
    window: str | None = None
    payload: str = ""


@dataclass
class StubDecision:
    id: str = "d-20260610-120000"
    critique: str = "two lanes idle; ship the web fix"
    actions: list = field(
        default_factory=lambda: [
            StubAction("dispatch", "unblock review", lane="web", payload="run tests"),
            StubAction("drop_lane", "lane finished", window="scratch"),
            StubAction("dispatch", "forbidden", lane="docs", payload="loop-adr accept 0007"),
        ]
    )


CLASSIFICATIONS = ["safe", "destructive", "blocked"]


@pytest.fixture
def paths(tmp_path):
    p = SessionPaths(tmp_path, "demo")
    p.ensure()
    return p


def test_create_and_get(paths):
    doc = create(StubDecision(), CLASSIFICATIONS, "manual", paths)
    assert doc["contract_version"] == 1
    assert doc["status"] == "pending"
    assert doc["approval_mode"] == "manual"
    assert doc["decided_by"] is None and doc["decided_at"] is None
    assert [a["idx"] for a in doc["actions"]] == [0, 1, 2]
    assert doc["actions"][0]["kind"] == "dispatch"
    assert doc["actions"][0]["lane"] == "web"
    assert doc["actions"][0]["classification"] == "safe"
    assert doc["actions"][0]["status"] == "awaiting-approval"
    assert doc["actions"][1]["status"] == "awaiting-approval"
    assert doc["actions"][2]["status"] == "rejected"  # blocked is never approvable
    assert paths.pending_decision_path.is_file()
    assert get(paths) == doc


def test_create_refuses_second_outstanding(paths):
    create(StubDecision(), CLASSIFICATIONS, "manual", paths)
    with pytest.raises(DecisionStateError):
        create(StubDecision(id="d-20260610-130000"), CLASSIFICATIONS, "manual", paths)


def test_get_without_pending_file(paths):
    assert get(paths) is None


def test_resolve_approve_all_and_cas(paths):
    doc = create(StubDecision(), CLASSIFICATIONS, "manual", paths)
    resolved = resolve(paths, doc["id"], approve=True, decided_by="andrew", reason="lgtm")
    assert resolved["status"] == "approved"
    assert resolved["decided_by"] == "andrew"
    assert resolved["reason"] == "lgtm"
    assert resolved["decided_at"]
    assert resolved["actions"][0]["status"] == "approved"
    assert resolved["actions"][1]["status"] == "approved"
    assert resolved["actions"][2]["status"] == "rejected"  # blocked stays rejected
    assert get(paths)["status"] == "approved"  # persisted

    with pytest.raises(DecisionStateError):  # status left pending exactly once
        resolve(paths, doc["id"], approve=True)
    with pytest.raises(DecisionStateError):
        resolve(paths, doc["id"], approve=False)


def test_resolve_partial_indices(paths):
    doc = create(StubDecision(), CLASSIFICATIONS, "manual", paths)
    resolved = resolve(paths, doc["id"], approve=True, action_indices=[0], decided_by="andrew")
    assert resolved["status"] == "approved"
    assert resolved["actions"][0]["status"] == "approved"
    assert resolved["actions"][1]["status"] == "rejected"  # awaiting but unlisted
    assert resolved["actions"][2]["status"] == "rejected"


def test_resolve_reject(paths):
    doc = create(StubDecision(), CLASSIFICATIONS, "manual", paths)
    resolved = resolve(paths, doc["id"], approve=False, decided_by="andrew", reason="nope")
    assert resolved["status"] == "rejected"
    assert all(a["status"] == "rejected" for a in resolved["actions"])


def test_resolve_id_mismatch(paths):
    create(StubDecision(), CLASSIFICATIONS, "manual", paths)
    with pytest.raises(DecisionStateError):
        resolve(paths, "d-wrong", approve=True)


def test_resolve_without_pending(paths):
    with pytest.raises(DecisionStateError):
        resolve(paths, "d-20260610-120000", approve=True)


def test_archive_removes_pending(paths):
    doc = create(StubDecision(), CLASSIFICATIONS, "manual", paths)
    resolved = resolve(paths, doc["id"], approve=True)
    archive(paths, resolved)
    assert not paths.pending_decision_path.exists()
    archived = paths.decisions_dir / f"{doc['id']}.json"
    assert json.loads(archived.read_text(encoding="utf-8")) == resolved
    # slot is free again
    create(StubDecision(id="d-20260610-130000"), CLASSIFICATIONS, "manual", paths)


def test_mark_action(paths):
    doc = create(StubDecision(), CLASSIFICATIONS, "manual", paths)
    mark_action(doc, 0, "executed")
    assert doc["actions"][0]["status"] == "executed"
    with pytest.raises(DecisionStateError):
        mark_action(doc, 99, "executed")


def test_verify_action_contract_is_minimal_lane_and_rationale():
    decision = parse_and_validate(
        """```decision
version: 1
critique: committed work is ready for read-only review
actions:
  - kind: verify
    lane: web
    rationale: ready for loop-verify
```""",
        {"web"},
    )
    assert decision.actions == [VerifyAction(lane="web", rationale="ready for loop-verify")]
