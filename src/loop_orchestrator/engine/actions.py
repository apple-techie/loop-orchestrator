"""Execute approved decision actions against the substrate.

Side effects only — classification and approval live in gate.py/decisions.py.
Actions arrive as pending-decision dicts (decisions.py shapes them); per-action
status updates here are in-memory, callers persist under the engine lock.
"""

from __future__ import annotations

import time

from ..substrate import Substrate, SubstrateError
from .config import EngineConfig
from .decisions import mark_action
from .events import EventLog

IDLE_POLL_INTERVAL_S = 3.0
IDLE_POLL_TIMEOUT_S = 120.0

_REPLY_FOOTER = (
    "\n\nWhen done, write a mailbox message "
    ".loop/messages/<UTC ts YYYYMMDD-HHMMSS>-{lane}-to-coord.md "
    "with frontmatter subject: re:{request_id}"
)


def _wait_for_idle(substrate: Substrate, lane: str) -> bool:
    """Poll lane_status every 3s for up to 120s; False = still not idle."""
    deadline = time.monotonic() + IDLE_POLL_TIMEOUT_S
    while True:
        if substrate.lane_status(lane) == "idle":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(IDLE_POLL_INTERVAL_S)


def execute(
    action: dict,
    substrate: Substrate,
    events: EventLog,
    config: EngineConfig,
    request_id: str = "",
) -> None:
    """One action's side effects; raises SubstrateError on delivery failure."""
    kind = action["kind"]
    if kind == "dispatch":
        substrate.dispatch(
            action["lane"],
            action["payload"],
            mode=action.get("mode", "text"),
            wait_ready=bool(action.get("wait_ready", False)),
        )
    elif kind == "add_lane":
        substrate.add_lane(
            action["window"],
            harness=action.get("harness"),
            cmd=action.get("cmd"),
            model=action.get("model"),
            role=action.get("role"),
            auto_approve=bool(action.get("auto_approve", False)),
        )
        substrate.dispatch(action["window"], action["brief"], wait_ready=True)
    elif kind == "drop_lane":
        substrate.drop_lane(action["window"])
    elif kind == "steer":
        if action.get("wait_for_idle"):
            _wait_for_idle(substrate, action["lane"])
        payload = action["payload"]
        if action.get("expects_reply"):
            payload += _REPLY_FOOTER.format(lane=action["lane"], request_id=request_id)
        substrate.dispatch(action["lane"], payload, interrupt=bool(action.get("interrupt", False)))
    elif kind == "stop":
        pass
    elif kind == "escalate":
        events.append(
            "escalate",
            summary=action.get("summary", ""),
            rationale=action.get("rationale", ""),
        )
    else:
        raise SubstrateError([kind], None, f"unknown action kind {kind!r}")


def execute_batch(
    doc: dict,
    substrate: Substrate,
    events: EventLog,
    config: EngineConfig,
) -> dict:
    """Run every 'approved'/'auto' action in the doc; continue past failures.

    Success marks the action 'executed' (+ action event); SubstrateError marks
    it 'failed' (+ action-failed event). Returns the updated doc — persistence
    is the caller's job.
    """
    for action in doc.get("actions") or []:
        if action.get("status") not in ("approved", "auto"):
            continue
        idx = action["idx"]
        try:
            execute(action, substrate, events, config, request_id=doc.get("id", ""))
        except SubstrateError as exc:
            mark_action(doc, idx, "failed")
            events.append(
                "action-failed",
                decision=doc.get("id"),
                idx=idx,
                kind=action.get("kind"),
                error=str(exc),
            )
        else:
            mark_action(doc, idx, "executed")
            events.append("action", decision=doc.get("id"), idx=idx, kind=action.get("kind"))
    return doc
