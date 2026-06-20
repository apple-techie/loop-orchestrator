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

import re
import time
import uuid
from pathlib import Path

from ..locking import atomic_write_json, file_lock, read_json
from ..paths import SessionPaths
from ..substrate import Substrate, SubstrateError
from . import gate, wiki
from .config import EngineConfig
from .decisions import mark_action
from .events import EventLog, utc_now
from .observe import current_mailbox_pending

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


# ── verify marker ledger ───────────────────────────────────────────────────


def load_verify_markers(paths: SessionPaths) -> list[dict]:
    doc = read_json(paths.verify_markers_path, {"verifies": []})
    verifies = doc.get("verifies") if isinstance(doc, dict) else None
    if not isinstance(verifies, list):
        return []
    return [marker for marker in verifies if isinstance(marker, dict)]


def save_verify_markers(paths: SessionPaths, markers: list[dict]) -> None:
    atomic_write_json(paths.verify_markers_path, {"verifies": markers})


def record_verify_marker(paths: SessionPaths, marker: dict) -> dict:
    window = marker.get("window")
    with file_lock(paths.lock_path):
        markers = [m for m in load_verify_markers(paths) if m.get("window") != window]
        markers.append(marker)
        save_verify_markers(paths, markers)
    return marker


# ── build marker ledger ────────────────────────────────────────────────────


def load_build_markers(paths: SessionPaths) -> list[dict]:
    doc = read_json(paths.build_markers_path, {"builds": []})
    builds = doc.get("builds") if isinstance(doc, dict) else None
    if not isinstance(builds, list):
        return []
    return [marker for marker in builds if isinstance(marker, dict)]


def save_build_markers(paths: SessionPaths, markers: list[dict]) -> None:
    atomic_write_json(paths.build_markers_path, {"builds": markers})


def record_build_marker(paths: SessionPaths, marker: dict) -> dict:
    window = marker.get("window")
    with file_lock(paths.lock_path):
        markers = [m for m in load_build_markers(paths) if m.get("window") != window]
        markers.append(marker)
        save_build_markers(paths, markers)
    return marker


def _build_brief(brief: str) -> str:
    return (
        "You are running an engine BUILD for this lane.\n\n"
        "Guardrails:\n"
        "- Implement the task ON THE CURRENT BRANCH in this worktree.\n"
        "- Run the repository's required gate/verification before committing.\n"
        "- Commit the completed changes on this worktree branch when green.\n"
        "- DO NOT merge, push, reinstall, switch branches, or edit main.\n\n"
        "--- build brief ---\n\n"
        f"{brief}"
    )


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


def _lane_worktree(paths: SessionPaths, window: str) -> Path:
    return paths.project_root / ".loop" / "worktrees" / paths.session / window


def _is_worktree_lane(paths: SessionPaths | None, window: str) -> bool:
    """True when window runs in an isolated git worktree — signalled by a recorded
    `loops.<window>.branch` in the ledger (T0025), the same marker the handoff
    carry uses. The engine uses this to gate F14 inline-spec embedding so a
    shared (non-worktree) lane's dispatch stays byte-identical."""
    return paths is not None and _predecessor_branch(paths, window) is not None


# F14: a worktree lane is cut from main HEAD, so a `tasks/Txxxx-….md` spec that is
# uncommitted in main or seeded after the cut is ABSENT from its working tree.
_TASK_REF_RE = re.compile(r"tasks/(T\d{4})[\w.-]*\.md")
_MAX_EMBED_CHARS = 16000


def _embed_task_spec(payload: str, paths: SessionPaths | None) -> str:
    """F14: make a worktree-lane dispatch SELF-CONTAINED. When the payload points
    a lane at a `tasks/Txxxx-….md` spec, resolve that file from the ENGINE's
    main-checkout tasks/ (paths.tasks_dir — which the engine CAN see, mirroring
    F15's main-root resolution) and append the spec INLINE, so a worktree lane
    cut from main HEAD needs no tasks/ access to execute. No reference / no paths
    / file missing / oversized / already-embedded => payload unchanged. The
    caller gates on the lane being worktree-isolated, so non-worktree dispatches
    are byte-identical."""
    if paths is None:
        return payload
    match = _TASK_REF_RE.search(payload)
    if not match:
        return payload
    task_id = match.group(1)
    from ..pm import taskfiles

    try:
        path = next(
            (p for p in taskfiles.list_tasks(paths.tasks_dir) if p.name.startswith(f"{task_id}-")),
            None,
        )
        if path is None:
            return payload
        spec = path.read_text(encoding="utf-8")
    except OSError:
        return payload
    if len(spec) > _MAX_EMBED_CHARS or spec in payload:
        return payload
    return (
        f"{payload}\n\n"
        f"--- task spec {path.name} (embedded inline — your worktree is cut from main "
        f"HEAD and may NOT contain this file; do NOT rely on reading it from tasks/) "
        f"---\n\n{spec}"
    )


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


def stop_suspected_mailbox_race(
    substrate: Substrate, observed_pending: list[str], events: EventLog
) -> bool:
    """A brain `stop` is honored only when the mailbox did not change under it.

    The cycle snapshot can observe an empty mailbox, then a steer/seed can land
    before the stop is honored. Re-read the mailbox once; any file not present in
    the cycle snapshot means the stop raced new work, so suppress it and let the
    next cycle ingest the message.
    """
    try:
        fresh_pending = current_mailbox_pending(substrate)
    except SubstrateError as exc:
        # Fail-open (honor the stop) so a flaky digest can't wedge the loop open,
        # but record it — previously the only silent degrade path in this module.
        events.append("stop-mailbox-recheck-failed", error=str(exc))
        return False
    new_files = sorted(set(fresh_pending) - set(observed_pending))
    if new_files:
        events.append("stop-suspected-mailbox-race", files=new_files)
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
        payload = action["payload"]
        # F14: a worktree lane can't read a tasks/ spec absent from its cut tree —
        # embed it inline. Gated on worktree-isolation => shared lanes unchanged.
        if _is_worktree_lane(paths, action["lane"]):
            payload = _embed_task_spec(payload, paths)
        substrate.dispatch(
            action["lane"],
            payload,
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
        # F14: a worktree lane is cut from main HEAD — embed the task spec inline
        # so it needs no tasks/ access. Gated on worktree => shared adds unchanged.
        if worktree:
            recovered_brief = _embed_task_spec(recovered_brief, paths)
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
    elif kind == "verify":
        if paths is None:
            raise SubstrateError([kind], None, "verify requires engine session paths")
        window = action.get("lane") or action.get("window")
        if not isinstance(window, str) or not window:
            raise SubstrateError([kind], None, "verify requires a target lane")
        branch = _predecessor_branch(paths, window)
        if branch is None:
            raise SubstrateError([kind, window], None, "target lane has no ledger branch")
        base = "main"
        started_at = utc_now()
        run_token = uuid.uuid4().hex[:12]
        out_path = paths.verify_run_result_path(window, run_token)
        worktree = _lane_worktree(paths, window)
        with file_lock(paths.lock_path):
            existing = next(
                (m for m in load_verify_markers(paths) if m.get("window") == window),
                None,
            )
            if existing is not None:
                events.append(
                    "verify-skip",
                    window=window,
                    reason="in-progress",
                    out_path=existing.get("out_path"),
                    pid=existing.get("pid"),
                )
                return
            tip_sha = substrate.branch_head(worktree, branch)
            # Skip a lane sitting exactly at base: nothing to verify (empty base..tip
            # diff) and nothing to merge. The drive context never surfaces an at-base
            # lane as ready-to-verify, but the brain can still over-propose a verify,
            # so guard at the spawn boundary — else it spuriously escalates an empty
            # merge. (A behind-main branch has a different SHA, so it still flows to
            # the verify -> verify-stale rebase path.)
            base_sha = substrate.branch_head(worktree, base)
            if tip_sha is not None and base_sha is not None and tip_sha == base_sha:
                events.append("verify-skip", window=window, reason="at-base")
                return
            verify_tip = tip_sha or branch
            pid = substrate.spawn_verify(worktree, base, verify_tip, out_path)
            marker = {
                "window": window,
                "branch": branch,
                "base": base,
                "tip": verify_tip,
                "out_path": str(out_path),
                "pid": pid,
                "started_at": started_at,
            }
            if tip_sha is not None:
                marker["tip_sha"] = tip_sha
            markers = load_verify_markers(paths)
            markers.append(marker)
            save_verify_markers(paths, markers)
        events.append(
            "verify-started",
            window=window,
            branch=branch,
            out_path=str(out_path),
            pid=pid,
        )
    elif kind == "build":
        if paths is None:
            raise SubstrateError([kind], None, "build requires engine session paths")
        window = action.get("window") or action.get("lane")
        if not isinstance(window, str) or not window:
            raise SubstrateError([kind], None, "build requires a target window")
        branch = _predecessor_branch(paths, window)
        if branch is None:
            raise SubstrateError([kind, window], None, "target lane has no ledger branch")
        started_at = utc_now()
        worktree = _lane_worktree(paths, window)
        with file_lock(paths.lock_path):
            existing = next(
                (m for m in load_build_markers(paths) if m.get("window") == window),
                None,
            )
            if existing is not None:
                events.append(
                    "build-skip",
                    window=window,
                    reason="in-progress",
                    pid=existing.get("pid"),
                )
                return
            # Never spawn a headless codex-exec build over a dirty worktree: drive
            # eligibility is decoupled from the tmux pane, so a lane an interactive
            # agent is mid-task in (with uncommitted edits) could otherwise be built
            # over and its work buried. A clean tree is the spawn-boundary guard.
            if substrate.worktree_dirty(worktree):
                events.append("build-skip", window=window, reason="worktree-dirty")
                return
            pre_build_sha = substrate.branch_head(worktree, branch)
            pid = substrate.spawn_build(worktree, _build_brief(str(action.get("brief") or "")))
            marker = {
                "window": window,
                "branch": branch,
                "pid": pid,
                "started_at": started_at,
            }
            # Omit pre_build_sha when the baseline could not be resolved (mirrors
            # the verify action's tip_sha handling). A None baseline must NEVER be
            # stored: surface_build_results would then read any later non-None
            # branch_head as `!= None` and emit a false build-done with zero
            # commits. Absent pre_build_sha → build-done can't fire → fail safe to
            # the timeout path.
            if pre_build_sha is not None:
                marker["pre_build_sha"] = pre_build_sha
            markers = load_build_markers(paths)
            markers.append(marker)
            save_build_markers(paths, markers)
        events.append(
            "build-started",
            window=window,
            branch=branch,
            pre_build_sha=pre_build_sha,
            pid=pid,
        )
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
            events.append(
                "action",
                decision=doc.get("id"),
                idx=idx,
                kind=action.get("kind"),
                lane=action.get("lane") or action.get("window"),
            )
    return doc
