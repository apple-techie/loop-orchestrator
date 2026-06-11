"""Execute approved decision actions against the substrate + the ask ledger.

Side effects only — classification and approval live in gate.py/decisions.py.
Actions arrive as pending-decision dicts (decisions.py shapes them); per-action
status updates here are in-memory, callers persist under the engine lock.

asks.json (engine_dir) tracks steer questions awaiting a mailbox reply:
{asks: [{id, lane, created_at, reply_timeout_s, status:
outstanding|replied|timed-out}]}. Written under the engine lock — the cycle
(here) records asks, the watch daemon marks them replied/timed-out.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..locking import atomic_write_json, file_lock, read_json
from ..paths import SessionPaths
from ..substrate import Substrate, SubstrateError
from .config import EngineConfig
from .decisions import mark_action
from .events import EventLog, utc_now

IDLE_POLL_INTERVAL_S = 3.0
IDLE_POLL_TIMEOUT_S = 120.0

_REPLY_FOOTER = (
    "\n\nWhen done, write a mailbox message "
    ".loop/messages/<UTC ts YYYYMMDD-HHMMSS>-{lane}-to-coord.md "
    "with frontmatter subject: re:{request_id}"
)


# ── ask ledger ──────────────────────────────────────────────────────────────


def asks_file(paths: SessionPaths) -> Path:
    """Engine-owned ask ledger (.loop/sessions/<s>/engine/asks.json)."""
    return paths.engine_dir / "asks.json"


def load_asks(paths: SessionPaths) -> list[dict]:
    """Asks list from asks.json; missing/corrupt file => []. Lock-free read."""
    doc = read_json(asks_file(paths), {"asks": []})
    asks = doc.get("asks") if isinstance(doc, dict) else None
    if not isinstance(asks, list):
        return []
    return [ask for ask in asks if isinstance(ask, dict)]


def save_asks(paths: SessionPaths, asks: list[dict]) -> None:
    """Atomic write; callers doing read-modify-write hold the engine lock."""
    atomic_write_json(asks_file(paths), {"asks": asks})


def record_ask(paths: SessionPaths, ask_id: str, lane: str, reply_timeout_s: int) -> dict:
    """Append an outstanding ask (cycle and CLI approvals both write here)."""
    ask = {
        "id": ask_id,
        "lane": lane,
        "created_at": utc_now(),
        "reply_timeout_s": reply_timeout_s,
        "status": "outstanding",
    }
    with file_lock(paths.lock_path):
        asks = load_asks(paths)
        asks.append(ask)
        save_asks(paths, asks)
    return ask


# ── action execution ────────────────────────────────────────────────────────


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
    ask_id: str = "",
    paths: SessionPaths | None = None,
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
        expects_reply = bool(action.get("expects_reply"))
        if expects_reply:
            payload += _REPLY_FOOTER.format(lane=action["lane"], request_id=ask_id)
        substrate.dispatch(action["lane"], payload, interrupt=bool(action.get("interrupt", False)))
        if expects_reply and paths is not None and ask_id:
            timeout_s = int(action.get("reply_timeout_s") or 1800)
            record_ask(paths, ask_id, action["lane"], timeout_s)
            events.append("ask", id=ask_id, lane=action["lane"], reply_timeout_s=timeout_s)
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
    paths: SessionPaths | None = None,
) -> dict:
    """Run every 'approved'/'auto' action in the doc; continue past failures.

    Success marks the action 'executed' (+ action event); SubstrateError marks
    it 'failed' (+ action-failed event). Steer asks get id '<decision_id>-<idx>'
    and are recorded in asks.json when `paths` is given. Returns the updated
    doc — persistence is the caller's job.
    """
    for action in doc.get("actions") or []:
        if action.get("status") not in ("approved", "auto"):
            continue
        idx = action["idx"]
        try:
            execute(
                action,
                substrate,
                events,
                config,
                ask_id=f"{doc.get('id', '')}-{idx}",
                paths=paths,
            )
        except SubstrateError as exc:
            mark_action(doc, idx, "failed")
            events.append(
                "action-failed",
                decision=doc.get("id"),
                idx=idx,
                kind=action.get("kind"),
                lane=action.get("lane") or action.get("window"),
                error=str(exc),
            )
        else:
            mark_action(doc, idx, "executed")
            events.append("action", decision=doc.get("id"), idx=idx, kind=action.get("kind"))
    return doc
