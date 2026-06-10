"""pending-decision.json lifecycle (contract_version 1).

Single outstanding decision at paths.pending_decision_path; every
read-modify-write happens under locking.file_lock(paths.lock_path) +
locking.atomic_write_json (the engine daemon and CLI/deck approvals are
concurrent writers). CAS rule: a doc's status leaves 'pending'/'needs-human'
exactly once; resolved docs are archived to paths.decisions_dir/<id>.json and
the pending file removed by the caller that finishes execution.
"""

from __future__ import annotations

import dataclasses

from ..locking import atomic_write_json, file_lock, read_json
from ..paths import SessionPaths
from .events import utc_now

_UNRESOLVED = ("pending", "needs-human")


class DecisionStateError(RuntimeError):
    """CAS violation: id mismatch, already-resolved, or no pending decision."""


def _action_fields(action: object) -> dict:
    if dataclasses.is_dataclass(action) and not isinstance(action, type):
        fields = dataclasses.asdict(action)
        # 'kind' is a ClassVar on the decision Action dataclasses, which
        # asdict() omits — the doc contract requires it on every action.
        if "kind" not in fields and getattr(action, "kind", None) is not None:
            fields = {"kind": action.kind, **fields}
        return fields
    if isinstance(action, dict):
        return dict(action)
    return dict(vars(action))


def create(decision, classifications: list[str], approval_mode: str, paths: SessionPaths) -> dict:
    """Write a new pending-decision doc; one per-action classification required.

    'blocked' actions start 'rejected' (never approvable); everything else
    starts 'awaiting-approval'. Raises DecisionStateError if an unresolved
    decision is already pending; a resolved leftover is overwritten.
    """
    actions = []
    for idx, (action, classification) in enumerate(
        zip(decision.actions, classifications, strict=True)
    ):
        fields = _action_fields(action)
        for reserved in ("idx", "classification", "status"):
            fields.pop(reserved, None)
        actions.append(
            {
                "idx": idx,
                **fields,
                "classification": classification,
                "status": "rejected" if classification == "blocked" else "awaiting-approval",
            }
        )
    doc = {
        "contract_version": 1,
        "id": decision.id,
        "created_at": utc_now(),
        "approval_mode": approval_mode,
        "status": "pending",
        "critique": decision.critique,
        "actions": actions,
        "decided_by": None,
        "decided_at": None,
        "reason": "",
    }
    with file_lock(paths.lock_path):
        existing = read_json(paths.pending_decision_path)
        if isinstance(existing, dict) and existing.get("status") in _UNRESOLVED:
            raise DecisionStateError(
                f"decision {existing.get('id')} is still {existing.get('status')}; "
                "resolve and archive it before creating another"
            )
        atomic_write_json(paths.pending_decision_path, doc)
    return doc


def get(paths: SessionPaths) -> dict | None:
    """The current pending-decision doc, or None. Lock-free: writes are atomic."""
    doc = read_json(paths.pending_decision_path)
    return doc if isinstance(doc, dict) else None


def resolve(
    paths: SessionPaths,
    decision_id: str,
    approve: bool,
    action_indices: list[int] | None = None,
    decided_by: str = "human",
    reason: str = "",
) -> dict:
    """Approve or reject the pending decision (the one CAS transition).

    On approve: listed awaiting actions become 'approved' and unlisted awaiting
    actions 'rejected'; no/empty indices approves every awaiting action. On
    reject: every awaiting action becomes 'rejected'. Actions already past
    'awaiting-approval' (executed/rejected-at-create) are untouched.
    """
    with file_lock(paths.lock_path):
        doc = read_json(paths.pending_decision_path)
        if not isinstance(doc, dict):
            raise DecisionStateError(f"no pending decision (looking for {decision_id})")
        if doc.get("id") != decision_id:
            raise DecisionStateError(f"pending decision is {doc.get('id')}, not {decision_id}")
        if doc.get("status") not in _UNRESOLVED:
            raise DecisionStateError(
                f"decision {decision_id} already resolved ({doc.get('status')})"
            )
        wanted = set(action_indices) if action_indices else None
        for action in doc.get("actions") or []:
            if action.get("status") != "awaiting-approval":
                continue
            if approve and (wanted is None or action.get("idx") in wanted):
                action["status"] = "approved"
            else:
                action["status"] = "rejected"
        doc["status"] = "approved" if approve else "rejected"
        doc["decided_by"] = decided_by
        doc["decided_at"] = utc_now()
        doc["reason"] = reason
        atomic_write_json(paths.pending_decision_path, doc)
    return doc


def archive(paths: SessionPaths, doc: dict) -> None:
    """File the doc under decisions/<id>.json and clear the pending slot.

    The pending file is only removed if it still holds this doc's id, so a
    decision created concurrently after resolution is never clobbered.
    """
    with file_lock(paths.lock_path):
        atomic_write_json(paths.decisions_dir / f"{doc['id']}.json", doc)
        pending = read_json(paths.pending_decision_path)
        if isinstance(pending, dict) and pending.get("id") == doc["id"]:
            try:
                paths.pending_decision_path.unlink()
            except FileNotFoundError:
                pass


def mark_action(doc: dict, idx: int, status: str) -> dict:
    """In-memory per-action status update; callers persist under the lock."""
    for action in doc.get("actions") or []:
        if action.get("idx") == idx:
            action["status"] = status
            return doc
    raise DecisionStateError(f"decision {doc.get('id')} has no action idx {idx}")
