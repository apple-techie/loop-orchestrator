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
from . import gate, wiki
from .config import EngineConfig
from .decisions import mark_action
from .events import EventLog, utc_now

IDLE_POLL_INTERVAL_S = 3.0
IDLE_POLL_TIMEOUT_S = 120.0


def _mailbox_message_hint(paths: SessionPaths | None, who: str) -> str:
    """The escalation/reply mailbox path a lane MUST write its `<...>-to-coord.md`
    message to (F15). ALWAYS the ENGINE's mailbox at the MAIN-checkout root
    (`paths.mailbox_dir`, absolute) — a lane running inside a git worktree has its
    own cwd-isolated `.loop/messages` (`.loop/` is gitignored, so each worktree
    gets a fresh one) that the engine NEVER ingests, so a cwd-relative path is a
    blind escalation. Resolving from the engine's configured root mirrors the
    substrate's `--project-root` worktree-correctness pattern. `paths is None`
    only on the legacy cli path (no engine root threaded): fall back to the
    relative hint, byte-identical to pre-F15."""
    base = str(paths.mailbox_dir) if paths is not None else ".loop/messages"
    return f"{base}/<UTC ts YYYYMMDD-HHMMSS>-{who}-to-coord.md"


def _reply_footer(paths: SessionPaths | None, lane: str, request_id: str) -> str:
    return (
        "\n\nWhen done, write a mailbox message "
        f"{_mailbox_message_hint(paths, lane)} "
        f"with frontmatter subject: re:{request_id}"
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


def _is_agent_harness(substrate: Substrate, harness: str | None) -> bool:
    """An agent lane holds in-flight work in its pane; shell/mprocs do not, and
    an unknown harness (raises from harness_field) reads as non-agent too."""
    if not harness or harness in ("shell", "mprocs"):
        return False
    try:
        substrate.harness_field(harness, "oneshot_template")  # raises if unknown
    except SubstrateError:
        return False
    return True


def _flush_handoff(
    window: str, substrate: Substrate, events: EventLog, paths: SessionPaths | None
) -> None:
    """T0023: before a drop_lane teardown, leave a `## Handoff state` breadcrumb
    on the lane page — but ONLY for a verified-idle agent lane, so the capture is
    a complete state, never a mid-generation paste/read. Non-agent / unknown /
    not-verified-idle lanes SKIP. Best-effort: any failure logs a skip and never
    blocks the teardown."""
    if paths is None:
        return
    try:
        harness = next((i.harness for i in substrate.lanes() if i.window == window), None)
        if not _is_agent_harness(substrate, harness):
            events.append("handoff-skip", window=window, reason="non-agent")
            return
        if substrate.lane_status(window) != "idle":
            events.append("handoff-skip", window=window, reason="not-verified-idle")
            return
        wiki.append_handoff(
            paths.lane_page(window), window, harness, substrate.capture_pane(window), utc_now()
        )
        events.append("handoff-flush", window=window, harness=harness)
    except SubstrateError as exc:
        events.append("handoff-skip", window=window, reason="error", error=str(exc))


def handoff_ack_subject(window: str) -> str:
    """T0028: the mailbox subject a successor lane uses to ack a handoff, on the
    `re:` reply convention so the ack is observable (confirmed landed)."""
    return f"re:handoff:{window}"


def _predecessor_branch(paths: SessionPaths, window: str) -> str | None:
    """The branch recorded for this window's prior worktree lane
    (loops.<window>.branch in the ledger, T0025), or None — used to carry the
    worktree to the successor on a swap rather than strand its in-flight commits."""
    ledger = read_json(paths.state_file, {})
    loops = ledger.get("loops") if isinstance(ledger, dict) else None
    entry = loops.get(window) if isinstance(loops, dict) else None
    branch = entry.get("branch") if isinstance(entry, dict) else None
    return branch if isinstance(branch, str) and branch else None


_HANDOFF_RECOVERY_TEMPLATE = (
    "## Handoff recovery — you are SUCCEEDING a prior lane in window '{window}'\n\n"
    "A predecessor agent was handed off out of this window. Do NOT cold-start:\n"
    "1. Read its breadcrumb in ops-wiki/lanes/{window}.md (the `## Handoff state`\n"
    "   section — last step, touched files, working tree, blocked-on, assumptions).\n"
    "2. Read the active task file under tasks/ for the in-flight work.\n"
    "3. Resume from where it left off.{worktree}\n"
    "Then ACK so the handoff is observable: write a mailbox message\n"
    "{mailbox} with frontmatter\n"
    "`subject: {ack}` confirming you have the context.\n\n--- original brief ---\n\n"
)


def _handoff_recovery(
    window: str, brief: str, paths: SessionPaths | None, events: EventLog
) -> tuple[str, bool]:
    """T0028 full handoff: if the window already carries a `## Handoff state`
    section (a predecessor was flushed here, T0023), prepend a recovery preamble
    so the successor resumes from the breadcrumb + task file and acks, and report
    whether to carry the predecessor's worktree branch (T0025). No handoff state
    => (brief, False), a normal cold add, byte-identical to today."""
    if paths is None or not wiki.has_handoff_state(paths.lane_page(window)):
        return brief, False
    branch = _predecessor_branch(paths, window)
    worktree_note = (
        f"\n   Your git worktree + branch ({branch}) carry over from the predecessor"
        " — its in-flight commits are intact; continue on that branch."
        if branch
        else ""
    )
    preamble = _HANDOFF_RECOVERY_TEMPLATE.format(
        window=window,
        ack=handoff_ack_subject(window),
        worktree=worktree_note,
        mailbox=_mailbox_message_hint(paths, window),  # F15: engine mailbox, not cwd
    )
    events.append("handoff-recovery", window=window, worktree_carry=bool(branch))
    return preamble + brief, bool(branch)


def stop_suspected_idle_stall(
    substrate: Substrate, working_lanes: set[str], events: EventLog
) -> bool:
    """T0032 (B2) defensive stop: a brain `stop` is honored only when the fleet is
    genuinely idle. The shared lane-status classifier can mis-read an idle lane as
    `working` (stale harness chrome), so the brain sees a busy fleet and stops and
    the loop dies quietly on an actually-idle fleet. Guard: if the cycle observed
    any working lane, RE-PROBE those lanes once (a fresh lane_status); if any is
    STILL working the fleet is not genuinely idle, so the stop is a suspected
    idle-stall — emit `stop-suspected-idle-stall` and return True to SUPPRESS it.
    No observed working lane => a genuine stop (no re-probe), return False. A lane
    that can't be probed is not counted as working."""
    if not working_lanes:
        return False
    still_working: list[str] = []
    for lane in sorted(working_lanes):
        try:
            if substrate.lane_status(lane) == "working":
                still_working.append(lane)
        except SubstrateError:
            continue
    if still_working:
        events.append("stop-suspected-idle-stall", lanes=still_working)
        return True
    return False


def execute(
    action: dict,
    substrate: Substrate,
    events: EventLog,
    config: EngineConfig,
    ask_id: str = "",
    paths: SessionPaths | None = None,
    code_writers: int | None = None,
) -> None:
    """One action's side effects; raises SubstrateError on delivery failure.

    `code_writers` is the per-cycle count of live code-writer lanes the loop
    resolves (None on the pre-T0026 / cli path = no conditional provisioning =
    shared, byte-identical)."""
    kind = action["kind"]
    if kind == "dispatch":
        substrate.dispatch(
            action["lane"],
            action["payload"],
            mode=action.get("mode", "text"),
            wait_ready=bool(action.get("wait_ready", False)),
        )
    elif kind == "add_lane":
        # T0025 gives the explicit opt-in; T0026 adds the conditional rule —
        # DORMANT at concurrency=1. The rule only applies to a code-writer lane
        # (agent harness, no raw cmd) and only when the loop threaded a count;
        # at code_writers in {None, 0} it resolves False -> shared, byte-identical.
        # T0028: a successor to a flushed lane recovers from its handoff
        # breadcrumb (+ carries the predecessor's worktree); a cold add is
        # unchanged (recovered_brief == brief, worktree_carry False).
        recovered_brief, worktree_carry = _handoff_recovery(
            action["window"], action["brief"], paths, events
        )
        new_harness = action.get("harness")
        is_code_writer = (
            bool(new_harness) and new_harness not in ("shell", "mprocs") and not action.get("cmd")
        )
        worktree = bool(action.get("worktree", False))
        if not worktree and is_code_writer and code_writers is not None:
            worktree = gate.should_provision_worktree(code_writers)
        worktree = worktree or worktree_carry  # T0028 continuity: reuse the predecessor's tree
        substrate.add_lane(
            action["window"],
            harness=action.get("harness"),
            cmd=action.get("cmd"),
            model=action.get("model"),
            role=action.get("role"),
            auto_approve=bool(action.get("auto_approve", False)),
            worktree=worktree,
        )
        # A just-provisioned lane has a fresh composer with no accumulated
        # context, so the auto-/clear (loop improvement #36) is unnecessary here
        # and would only race the freshly-booted welcome screen — opt out.
        substrate.dispatch(action["window"], recovered_brief, wait_ready=True, no_clear=True)
    elif kind == "drop_lane":
        _flush_handoff(action["window"], substrate, events, paths)
        substrate.drop_lane(action["window"])
    elif kind == "steer":
        if action.get("wait_for_idle"):
            _wait_for_idle(substrate, action["lane"])
        payload = action["payload"]
        expects_reply = bool(action.get("expects_reply"))
        if expects_reply:
            payload += _reply_footer(paths, action["lane"], ask_id)
        # A steer is mid-conversation guidance — it MUST preserve the lane's
        # context (it often references prior work), so never auto-/clear here,
        # even when the steer waits for an idle lane (loop improvement #36).
        substrate.dispatch(
            action["lane"],
            payload,
            mode=action.get("mode", "text"),
            interrupt=bool(action.get("interrupt", False)),
            no_clear=True,
        )
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
    code_writers: int | None = None,
) -> dict:
    """Run every 'approved'/'auto' action in the doc; continue past failures.

    Success marks the action 'executed' (+ action event); SubstrateError marks
    it 'failed' (+ action-failed event). Steer asks get id '<decision_id>-<idx>'
    and are recorded in asks.json when `paths` is given. `code_writers` (the
    loop's per-cycle live code-writer count) drives T0026 conditional worktree
    provisioning; None (cli path) = shared, byte-identical. Returns the updated
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
                code_writers=code_writers,
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
