"""One full engine cycle: observe -> ingest nudge -> pm pull -> brain -> gate
-> act -> pm push.

Exit codes: 0 completed cycle (stdout says whether approval is pending),
1 brain invocation failure, 3 a decision is already in flight (single
in-flight invariant), 4 the brain would not produce a usable decision even
after one corrective re-prompt (needs-human doc filed), 5 paused.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shlex
import signal
import sys
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from ..locking import atomic_write_json, file_lock, read_json
from ..paths import SessionPaths
from ..pm import registry as pm_registry
from ..pm.base import PMAdapter
from ..substrate import Substrate, SubstrateError
from ..verify import DEFAULT_GATE_TIMEOUT_S, DEFAULT_LENS_TIMEOUT_S
from . import actions as actions_mod
from . import decision as decision_mod
from . import decisions, gate, wiki
from .brain import Brain, BrainError, oneshot_argv, run_oneshot
from .config import EngineConfig, HarnessPolicy, lane_config_harnesses
from .decision import DecisionError
from .events import EventLog, parse_ts
from .observe import EngineSnapshot, Observer, adaptive_timeout, sync_loops_registry

_HEADER_RESOURCE = ("contracts", "checkpoint-header.md")

_CORRECTIVE_SUFFIX = (
    "\n\nYour previous reply could not be used: {error}. Reply with ONLY the fenced decision block."
)

_INGEST_HEADING = "### Ingest protocol"

# Timed-out asks stay visible in the checkpoint addendum this long.
_ASK_TIMEOUT_RECENCY_S = 3600
_BUILD_DRIVE_OUTCOME_EVENTS = frozenset({"build-done", "build-failed", "build-timeout"})
_BUILD_DRIVE_EVENT_TAIL = 100
_BUILD_DRIVE_OUTCOME_LIMIT = 5
_BUILD_TIMEOUT_S = 1500
# A lane an interactive agent is actively occupying — never spawn a headless
# build/verify on its worktree while it may be editing. idle/unknown/errored mean
# the worktree is quiescent (no live agent) and safe to drive headlessly.
_BUSY_LANE_STATES = frozenset({"working", "awaiting-approval"})
# A finding at or above this severity blocks the merge gate; low/medium ride
# along as residual caveats in the escalate summary. The adversarial lens nearly
# always raises SOME low/medium nitpick, so gating escalate on overall==pass means
# the merge gate is never reached — severity is the real block signal.
_BLOCKING_SEVERITIES = frozenset({"high", "critical"})
_SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_VERIFY_DRIVE_OUTCOME_EVENTS = frozenset(
    {"verify-passed", "verify-failed", "verify-timeout", "verify-stale"}
)
_VERIFY_DRIVE_EVENT_TAIL = 100
_VERIFY_DRIVE_OUTCOME_LIMIT = 5
_VERIFY_DIFF_TIMEOUT_S = 60
_VERIFY_TIMEOUT_BUFFER_S = 1140
_VERIFY_TIMEOUT_S = (
    DEFAULT_GATE_TIMEOUT_S
    + _VERIFY_DIFF_TIMEOUT_S
    + DEFAULT_LENS_TIMEOUT_S
    + _VERIFY_TIMEOUT_BUFFER_S
)

# approval mode -> classifications that execute without a human (blocked never runs).
_AUTO_CLASSES: dict[str, frozenset[str]] = {
    "manual": frozenset(),
    "auto": frozenset({"safe"}),
    "full": frozenset({"safe", "destructive"}),
}


def _max_finding_severity(findings_raw: object) -> str | None:
    """The highest-ranked severity among a verify result's findings (or None).
    Used to decide escalate-eligibility: a high/critical finding blocks the merge
    gate, low/medium ride along as residual caveats."""
    if not isinstance(findings_raw, list):
        return None
    blocking_rank = _SEVERITY_RANK["critical"]
    best_rank, best_label = 0, None
    for finding in findings_raw:
        if not isinstance(finding, dict):
            continue
        raw = finding.get("severity")
        label = str(raw).strip().lower() if raw is not None else ""
        if not label:
            continue
        # Fail CLOSED on an out-of-vocabulary severity ('blocker', 'sev1', …): treat
        # it as blocking so a future/third-party producer can't slip past the
        # escalate gate. (Empty/missing severity stays non-blocking — it may be a
        # benign note, and the gate/overall clauses still apply.)
        if label in _SEVERITY_RANK:
            rank = _SEVERITY_RANK[label]
        else:
            rank, label = blocking_rank, "critical"
        if rank > best_rank:
            best_rank, best_label = rank, label
    return best_label


def surface_verify_results(substrate: Substrate, paths: SessionPaths, events: EventLog) -> None:
    with file_lock(paths.lock_path):
        markers = actions_mod.load_verify_markers(paths)
        if not markers:
            return
        remaining: list[dict] = []
        changed = False
        for idx, marker in enumerate(markers):
            out_path = marker.get("out_path")
            if not isinstance(out_path, str) or not out_path:
                remaining.append(marker)
                continue
            try:
                result = substrate.read_verify_result(out_path)
                overall = result.get("overall") if result is not None else None
                if overall not in ("pass", "concerns", "fail"):
                    try:
                        started_at = parse_ts(str(marker.get("started_at") or ""))
                        age_s = (datetime.now(timezone.utc) - started_at).total_seconds()
                    except (TypeError, ValueError):
                        age_s = _VERIFY_TIMEOUT_S + 1
                    if age_s > _VERIFY_TIMEOUT_S:
                        _terminate_verify_runner(substrate, marker.get("pid"), events)
                        # Record verified_tip on timeout too (as pass/fail do) so the
                        # ready-to-verify gate re-arms when the branch ADVANCES past the
                        # timed-out SHA; otherwise verified_tip stays None and the
                        # None-fallback suppresses the lane head-blind, stalling a
                        # timed-out-then-fixed build forever.
                        _maybe_record_verified_tip(paths, marker)
                        events.append(
                            "verify-timeout",
                            window=marker.get("window"),
                            pid=marker.get("pid"),
                            out_path=out_path,
                            started_at=marker.get("started_at"),
                            timeout_s=_VERIFY_TIMEOUT_S,
                        )
                        changed = True
                        _save_verify_markers_best_effort(
                            paths, remaining + markers[idx + 1 :], events
                        )
                        continue
                    remaining.append(marker)
                    continue
                findings_raw = result.get("findings")
                findings = len(findings_raw) if isinstance(findings_raw, list) else 0
                max_severity = _max_finding_severity(findings_raw)
                gate = result.get("gate")
                gate_passed = (
                    bool(gate.get("passed")) if isinstance(gate, dict) else (overall == "pass")
                )
                window = marker.get("window")
                branch = marker.get("branch")
                # The lane branch must be CURRENT to act on its verdict: main must be
                # an ancestor of the branch (a clean fast-forward). When main has
                # advanced past the branch's fork point, the `main..branch` diff the
                # lenses reviewed is polluted by main's newer work showing up as
                # reversions, so the verdict — pass OR fail — is UNRELIABLE.
                mergeable = (
                    isinstance(window, str)
                    and bool(window)
                    and isinstance(branch, str)
                    and bool(branch)
                    and substrate.is_ancestor(
                        actions_mod._lane_worktree(paths, window), "main", branch
                    )
                )
                has_blocking = max_severity in _BLOCKING_SEVERITIES
                if not gate_passed:
                    # A broken gate (tests fail) is real and currency-independent.
                    event = "verify-failed"
                elif not mergeable:
                    # Gate ok but the branch forked from a stale main: route to
                    # verify-stale (a rebase signal) regardless of the unreliable lens
                    # verdict — never escalate-merge an unmergeable branch (the false
                    # PASS) and never churn a futile build-fix on a revert-shaped diff
                    # (the false FAIL). Rebase onto main, then re-verify.
                    event = "verify-stale"
                elif overall != "fail" and not has_blocking:
                    # Current branch, gate ok, no hard fail, no high/critical finding
                    # -> escalate-eligible (low/medium ride along as caveats).
                    event = "verify-passed"
                else:
                    event = "verify-failed"
                _maybe_record_verified_tip(paths, marker)
                events.append(
                    event,
                    window=window,
                    overall=overall,
                    findings=findings,
                    max_severity=max_severity,
                )
                changed = True
                _save_verify_markers_best_effort(paths, remaining + markers[idx + 1 :], events)
            except Exception as exc:
                events.append(
                    "verify-surface-error",
                    window=marker.get("window"),
                    out_path=out_path,
                    error=f"{type(exc).__name__}: {exc}",
                )
                remaining.append(marker)
        if changed:
            _save_verify_markers_best_effort(paths, remaining, events)


def _save_verify_markers_best_effort(
    paths: SessionPaths, markers: list[dict], events: EventLog
) -> bool:
    try:
        actions_mod.save_verify_markers(paths, markers)
    except OSError as exc:
        events.append("verify-marker-save-failed", error=str(exc))
        return False
    return True


def surface_build_results(substrate: Substrate, paths: SessionPaths, events: EventLog) -> None:
    with file_lock(paths.lock_path):
        markers = actions_mod.load_build_markers(paths)
        if not markers:
            return
        remaining: list[dict] = []
        changed = False
        for idx, marker in enumerate(markers):
            try:
                window = marker.get("window")
                branch = marker.get("branch")
                pre_build_sha = marker.get("pre_build_sha")
                worktree = (
                    actions_mod._lane_worktree(paths, window)
                    if isinstance(window, str) and window
                    else None
                )
                branch_head = None
                if worktree is not None and isinstance(branch, str) and branch:
                    branch_head = substrate.branch_head(worktree, branch)
                try:
                    started_at = parse_ts(str(marker.get("started_at") or ""))
                    age_s = (datetime.now(timezone.utc) - started_at).total_seconds()
                except (TypeError, ValueError):
                    age_s = _BUILD_TIMEOUT_S + 1
                # A branch advance only counts as a COMPLETED build once the codex
                # runner has exited: a still-running codex's incremental/WIP commit
                # would otherwise false-fire build-done, clear the marker (orphaning
                # the live runner so it can never be timed-out/killed) and arm a
                # verify against a branch codex is still mutating. pre_build_sha must
                # be present (a None baseline never establishes an advance).
                advanced = (
                    pre_build_sha is not None
                    and branch_head is not None
                    and branch_head != pre_build_sha
                )
                runner_alive = _build_runner_alive(substrate, marker.get("pid"), worktree)
                if advanced and not runner_alive:
                    events.append(
                        "build-done",
                        window=window,
                        branch=branch,
                        pre_build_sha=pre_build_sha,
                        branch_head=branch_head,
                    )
                    changed = True
                    _save_build_markers_best_effort(paths, remaining + markers[idx + 1 :], events)
                    continue
                if pre_build_sha is not None and branch_head == pre_build_sha and not runner_alive:
                    # TOCTOU guard: branch_head was read BEFORE the runner-alive check,
                    # so a build that committed THEN exited in that window looks
                    # unchanged here. Re-read now that the runner is confirmed gone —
                    # an actual advance is a build-done, not a failure (pre-change this
                    # state fell through to timeout and self-corrected next cycle; the
                    # build-failed terminal would otherwise mislabel it permanently).
                    fresh_head = (
                        substrate.branch_head(worktree, branch)
                        if isinstance(branch, str) and branch
                        else None
                    )
                    if fresh_head is not None and fresh_head != pre_build_sha:
                        events.append(
                            "build-done",
                            window=window,
                            branch=branch,
                            pre_build_sha=pre_build_sha,
                            branch_head=fresh_head,
                        )
                    else:
                        events.append(
                            "build-failed",
                            window=window,
                            branch=branch,
                            pre_build_sha=pre_build_sha,
                            branch_head=fresh_head or branch_head,
                        )
                    changed = True
                    _save_build_markers_best_effort(paths, remaining + markers[idx + 1 :], events)
                    continue
                if age_s > _BUILD_TIMEOUT_S:
                    _terminate_build_runner(substrate, marker.get("pid"), worktree, events)
                    events.append(
                        "build-timeout",
                        window=window,
                        branch=branch,
                        pid=marker.get("pid"),
                        started_at=marker.get("started_at"),
                        timeout_s=_BUILD_TIMEOUT_S,
                        pre_build_sha=pre_build_sha,
                        branch_head=branch_head,
                    )
                    changed = True
                    _save_build_markers_best_effort(paths, remaining + markers[idx + 1 :], events)
                    continue
                remaining.append(marker)
            except Exception as exc:
                # One bad marker must never abort the whole surface pass (and the
                # cycle): re-append it and record a diagnostic, mirroring
                # surface_verify_results' per-marker isolation.
                events.append(
                    "build-surface-error",
                    window=marker.get("window"),
                    error=f"{type(exc).__name__}: {exc}",
                )
                remaining.append(marker)
        if changed:
            _save_build_markers_best_effort(paths, remaining, events)


def _save_build_markers_best_effort(
    paths: SessionPaths, markers: list[dict], events: EventLog
) -> bool:
    try:
        actions_mod.save_build_markers(paths, markers)
    except OSError as exc:
        events.append("build-marker-save-failed", error=str(exc))
        return False
    return True


def _record_verified_tip(paths: SessionPaths, window: str, verified_tip: str) -> None:
    ledger = read_json(paths.state_file, {})
    if not isinstance(ledger, dict):
        ledger = {}
    loops = ledger.get("loops")
    if not isinstance(loops, dict):
        loops = {}
        ledger["loops"] = loops
    entry = loops.get(window)
    if not isinstance(entry, dict):
        entry = {}
        loops[window] = entry
    entry["verified_tip"] = verified_tip
    atomic_write_json(paths.state_file, ledger)


def _maybe_record_verified_tip(paths: SessionPaths, marker: dict) -> None:
    """Record loops.<window>.verified_tip = the SHA this verify ran against
    (marker['tip_sha'], snapshotted at spawn) when both are present. Called for
    pass, fail, AND timeout so the ready-to-verify gate re-arms only when the
    branch advances past the verified SHA."""
    window = marker.get("window")
    tip_sha = marker.get("tip_sha")
    if isinstance(window, str) and window and isinstance(tip_sha, str) and tip_sha:
        _record_verified_tip(paths, window, tip_sha)


def _terminate_verify_runner(
    substrate: Substrate, pid_value: object, events: EventLog | None = None
) -> None:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return
    if pid <= 1:
        return
    try:
        command = substrate.process_command(pid, timeout=2)
    except SubstrateError as exc:
        if events is not None:
            events.append("verify-kill-skip", pid=pid, reason="ps-failed", error=str(exc))
        return
    if command is None or "loop-verify" not in command:
        if events is not None:
            events.append("verify-kill-skip", pid=pid, reason="identity-mismatch")
        return
    try:
        if os.getpgid(pid) != pid:
            return
        os.killpg(pid, signal.SIGTERM)
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        return


def _build_runner_alive(substrate: Substrate, pid_value: object, worktree: object) -> bool:
    """True while the build's codex-exec runner is still alive, identified by the
    per-window worktree --cd path so a recycled PID running something else (or
    another lane's build) reads as gone. On a ps failure, treat as ALIVE
    (conservative: defer to the timeout path rather than declare a false
    build-done on an unconfirmed PID)."""
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        command = substrate.process_command(pid, timeout=2)
    except SubstrateError:
        return True
    if command is None:
        return False
    # Anchor on the `--cd <worktree> ` token (the brief always follows, so the
    # trailing space is present), NOT a bare substring: lane `web`'s path is a
    # substring of lane `web2`'s `--cd .../web2 <brief>`, which a recycled PID on
    # the sibling could otherwise defeat.
    return (
        isinstance(worktree, (str, Path))
        and "codex exec" in command
        and f"--cd {worktree} " in command
    )


def _terminate_build_runner(
    substrate: Substrate,
    pid_value: object,
    worktree: object,
    events: EventLog | None = None,
) -> None:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return
    if pid <= 1:
        return
    try:
        command = substrate.process_command(pid, timeout=2)
    except SubstrateError as exc:
        if events is not None:
            events.append("build-kill-skip", pid=pid, reason="ps-failed", error=str(exc))
        return
    wt = str(worktree) if isinstance(worktree, (str, Path)) else ""
    # Identity-guard on the `--cd <worktree> ` token (per-window), not just
    # "codex exec" or a bare substring: a recycled PID landing on a DIFFERENT
    # lane's codex-exec build is its own pgrp leader too, and a sibling-prefix
    # lane (web vs web2) would defeat a bare substring — either lets us SIGKILL
    # the wrong lane's build.
    if command is None or "codex exec" not in command or not wt or f"--cd {wt} " not in command:
        if events is not None:
            events.append("build-kill-skip", pid=pid, reason="identity-mismatch")
        return
    try:
        if os.getpgid(pid) != pid:
            return
        os.killpg(pid, signal.SIGTERM)
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError as exc:
        # A real kill failure (e.g. EPERM, unkillable group) leaves a runaway
        # --dangerously-bypass codex committing into a branch the engine now
        # believes is dead — surface it so an operator can spot the orphan.
        if events is not None:
            events.append("build-kill-failed", pid=pid, error=str(exc))
        return


def _ask_lines(asks: list[dict], now: datetime) -> list[str]:
    """Checkpoint-addendum lines: outstanding asks + recently timed-out ones."""
    lines: list[str] = []
    for ask in asks:
        status = ask.get("status")
        if status == "outstanding":
            lines.append(
                f"{ask.get('id')} lane={ask.get('lane')} status=outstanding "
                f"created_at={ask.get('created_at')} reply_timeout_s={ask.get('reply_timeout_s')}"
            )
        elif status == "timed-out":
            try:
                age_s = (now - parse_ts(ask.get("timed_out_at") or "")).total_seconds()
            except (TypeError, ValueError):
                continue
            if age_s <= _ASK_TIMEOUT_RECENCY_S:
                lines.append(
                    f"{ask.get('id')} lane={ask.get('lane')} status=timed-out "
                    f"timed_out_at={ask.get('timed_out_at')}"
                )
    return lines or ["(none)"]


def _latest_verify_outcomes(paths: SessionPaths) -> list[dict]:
    latest_by_window: dict[str, dict] = {}
    for event in EventLog(paths.events_path).tail(_VERIFY_DRIVE_EVENT_TAIL):
        if event.get("event") not in _VERIFY_DRIVE_OUTCOME_EVENTS:
            continue
        window = event.get("window")
        if not isinstance(window, str) or not window:
            continue
        latest_by_window.pop(window, None)
        latest_by_window[window] = event
    return list(latest_by_window.values())


def _latest_build_outcomes(paths: SessionPaths) -> list[dict]:
    latest_by_window: dict[str, dict] = {}
    for event in EventLog(paths.events_path).tail(_BUILD_DRIVE_EVENT_TAIL):
        if event.get("event") not in _BUILD_DRIVE_OUTCOME_EVENTS:
            continue
        window = event.get("window")
        if not isinstance(window, str) or not window:
            continue
        latest_by_window.pop(window, None)
        latest_by_window[window] = event
    return list(latest_by_window.values())


def _verified_tip(paths: SessionPaths, lane: str) -> str | None:
    ledger = read_json(paths.state_file, {})
    loops = ledger.get("loops") if isinstance(ledger, dict) else None
    entry = loops.get(lane) if isinstance(loops, dict) else None
    if not isinstance(entry, dict):
        return None
    verified_tip = entry.get("verified_tip")
    return verified_tip if isinstance(verified_tip, str) and verified_tip else None


def _open_backlog_by_loop(paths: SessionPaths) -> dict[str, list[str]]:
    """Map loop-id -> open task ids (status open/in-progress/review) parsed from
    tasks/, so the awaiting-build drive section can point the brain at the backlog
    for an idle lane. Mirrors derive_loops_from_tasks' frontmatter parsing; a
    malformed task file is skipped, never fatal."""
    import yaml

    from ..pm import taskfiles

    backlog: dict[str, list[str]] = {}
    try:
        task_paths = taskfiles.list_tasks(paths.tasks_dir)
    except OSError:
        return backlog
    for path in task_paths:
        try:
            fm = taskfiles.parse_frontmatter(path)
        except (OSError, ValueError, yaml.YAMLError):
            # OSError too: parse_frontmatter read_text()s the file, which can race
            # a lane deleting/renaming a task between list_tasks and the read.
            # Skip the file, never abort the drive cycle ("never fatal").
            continue
        loop = fm.get("loop")
        task_id = fm.get("id")
        status = str(fm.get("status") or "")
        if (
            isinstance(loop, str)
            and loop
            and isinstance(task_id, str)
            and task_id
            and status in ("open", "in-progress", "review")
        ):
            backlog.setdefault(loop, []).append(task_id)
    return backlog


def _verify_drive_lines(
    substrate: Substrate, snap: EngineSnapshot, paths: SessionPaths
) -> list[str]:
    build_markers = actions_mod.load_build_markers(paths)
    build_in_flight_windows = {
        window
        for marker in build_markers
        if isinstance((window := marker.get("window")), str) and window
    }
    build_outcomes = _latest_build_outcomes(paths)[-_BUILD_DRIVE_OUTCOME_LIMIT:]
    markers = actions_mod.load_verify_markers(paths)
    in_flight_windows = {
        window for marker in markers if isinstance((window := marker.get("window")), str) and window
    }
    latest_outcomes = _latest_verify_outcomes(paths)
    outcomes = latest_outcomes[-_VERIFY_DRIVE_OUTCOME_LIMIT:]
    latest_outcome = {
        str(outcome["window"]): outcome
        for outcome in latest_outcomes
        if isinstance(outcome.get("window"), str)
    }

    # Shared base (main HEAD): a lane whose branch is AT base has nothing built
    # yet (awaiting-build), and a lane ahead of base has a non-empty diff to
    # review (ready-to-verify). Resolving main from the project root is
    # worktree-independent (linked worktrees share .git).
    base_head = substrate.branch_head(paths.project_root, "main")
    open_backlog = _open_backlog_by_loop(paths)

    # Candidate lanes = the worktree lanes (ledger loops with a branch, T0025) UNION
    # the live tmux lanes. build/verify are HEADLESS (codex exec / loop-verify run
    # detached, never touching the lane's pane), so a worktree lane is drivable even
    # with no booted AI harness — it need not be a tmux window at all, and one that
    # is reads status "unknown" rather than "idle". Eligibility therefore keys off
    # the registered branch + a quiescent worktree, NOT a live idle pane.
    ledger = read_json(paths.state_file, {})
    loops_doc = ledger.get("loops") if isinstance(ledger, dict) else None
    branch_lanes = (
        {
            window
            for window, entry in loops_doc.items()
            if isinstance(entry, dict)
            and isinstance(entry.get("branch"), str)
            and entry.get("branch")
        }
        if isinstance(loops_doc, dict)
        else set()
    )

    ready: list[str] = []
    ready_lanes: set[str] = set()
    awaiting: list[str] = []
    for lane in sorted(set(snap.lanes) | branch_lanes):
        info = snap.lanes.get(lane) or {}
        status = info.get("status")
        if (
            status in _BUSY_LANE_STATES
            or lane in in_flight_windows
            or lane in build_in_flight_windows
        ):
            continue
        branch = actions_mod._predecessor_branch(paths, lane)
        if branch is None:
            continue
        head = substrate.branch_head(actions_mod._lane_worktree(paths, lane), branch)
        verified_tip = _verified_tip(paths, lane)
        status_label = status or "unknown"
        # Awaiting-build: the branch is AT base (nothing built) and the lane has
        # open backlog -> the brain should propose a `build`, not a `verify`. This
        # closes the cold-start gap: without it a fresh lane at main false-read as
        # ready-to-verify (head != verified_tip=None) and there was no signal that
        # an idle lane was waiting for its first build.
        if head is not None and base_head is not None and head == base_head:
            tasks = open_backlog.get(lane)
            if tasks:
                awaiting.append(
                    f"- lane={lane} branch={branch} status={status_label} at-base "
                    f"open-tasks={','.join(tasks)}"
                )
            continue
        # Ready-to-verify requires the branch to be AHEAD of base (the at-base
        # case is handled above), so a lane sitting at main never false-reads ready.
        if head is None:
            if lane in latest_outcome:
                continue
        elif verified_tip is None and lane in latest_outcome:
            continue
        elif head == verified_tip:
            continue
        ready_lanes.add(lane)
        ready.append(f"- lane={lane} branch={branch} status={status_label}")

    outcomes = [outcome for outcome in outcomes if str(outcome["window"]) not in ready_lanes]

    lines: list[str] = []
    if awaiting:
        lines.append("awaiting-build:")
        lines.extend(awaiting)
    if ready:
        lines.append("ready-to-verify:")
        lines.extend(ready)
    if build_markers:
        lines.append("build in flight:")
        for marker in build_markers:
            window = marker.get("window")
            if not isinstance(window, str) or not window:
                continue
            branch = marker.get("branch") or actions_mod._predecessor_branch(paths, window)
            lines.append(
                f"- window={window} branch={branch or '(unknown)'} "
                f"started_at={marker.get('started_at')} pid={marker.get('pid')} "
                f"pre_build_sha={marker.get('pre_build_sha') or '(unknown)'}"
            )
    if markers:
        lines.append("verify in flight:")
        for marker in markers:
            window = marker.get("window")
            if not isinstance(window, str) or not window:
                continue
            branch = marker.get("branch") or actions_mod._predecessor_branch(paths, window)
            lines.append(
                f"- window={window} branch={branch or '(unknown)'} "
                f"started_at={marker.get('started_at')} pid={marker.get('pid')}"
            )
    if build_outcomes:
        lines.append("recent build outcomes:")
        for outcome in build_outcomes:
            window = str(outcome["window"])
            branch = outcome.get("branch") or actions_mod._predecessor_branch(paths, window)
            lines.append(
                f"- window={window} event={outcome.get('event')} "
                f"branch={branch or '(unknown)'} "
                f"pre_build_sha={outcome.get('pre_build_sha') or '(unknown)'} "
                f"branch_head={outcome.get('branch_head') or '(unknown)'}"
            )
    if outcomes:
        lines.append("recent verify outcomes:")
        for outcome in outcomes:
            window = str(outcome["window"])
            branch = actions_mod._predecessor_branch(paths, window)
            overall = outcome.get("overall")
            if outcome.get("event") == "verify-timeout" and not overall:
                overall = "timeout"
            lines.append(
                f"- window={window} event={outcome.get('event')} "
                f"overall={overall or '(unknown)'} findings={outcome.get('findings', '?')} "
                f"branch={branch or '(unknown)'}"
            )
    if not lines:
        return []
    return ["--- verify drive ---", *lines]


# Condensed plan-A.4 selection rubric, appended to the brain prompt alongside
# the roster (only once a harness_policy is written — the empty policy keeps
# the prompt byte-identical to today). ~700 chars, far under the 24000-token
# checkpoint warn threshold.
_HARNESS_RUBRIC = """\
--- harness selection rubric (first match wins) ---
brain / headless ingest: claude (codex fallback, brain_allow-gated)
high-risk infra: claude interactive (codex pinned fallback)
product reasoning / spec / UX: pi (claude fallback)
synthesis / docs / wiki: pi (claude fallback)
agentic codebase search: amp (claude when model pinning matters)
cheap bulk / parallel grunt edits: opencode (forge fallback)
fast one-shot burst, latency-sensitive: forge (droid exec fallback)
headless autonomous coding burst: droid (codex exec fallback)
cursor-model-specific edits: cursor-agent (skip if loop skills needed)
gateway-mediated / fleet task: openclaw (hermes fallback)
agent-platform experiment: hermes (claude fallback)
watcher / probe / log tail: shell; process dashboard: mprocs
tie-breakers: reproducibility required -> exclude amp (cannot pin a model);
unattended-destructive -> only claude/codex/hermes/amp; high drift +
unattended + high-risk role -> the gate forces human approval."""


_DRIVE_RUBRIC = """\
--- verify drive rubric ---
Act on each window's latest listed outcome only.
awaiting-build lane -> propose a `build` for that lane with a brief drawn from a named open task.
build in flight -> wait; do not propose another `build` for that window.
latest build-done -> lane is ready-to-verify.
latest build-failed -> propose a `build` fix for that lane.
latest build-timeout -> propose a narrower `build` for that lane.
latest verify-passed -> propose `escalate` summary: merge <branch> — verified, N findings.
verify-passed = gate passed + NO high/critical finding (low/medium may remain) = mergeable.
Escalate a verify-passed lane (note residual low/medium in the summary); do NOT keep fixing it.
latest verify-failed/verify-timeout -> propose a `build` for that lane with a fix brief.
latest verify-stale -> the branch is behind main and cannot clean-merge.
propose `escalate`: rebase <branch> onto main, then re-verify — do NOT merge a stale branch.
ready-to-verify lane -> propose `verify` for that lane.
These are headless worktree lanes: drive them with `build`/`verify`, NEVER `dispatch`/`steer`.
Include the named findings in the build fix brief.
Never merge directly; the escalate action is the single human gate.
Never escalate-merge a failed or timed-out build."""


_UTILIZATION_RUBRIC = """\
--- lane utilization rubric ---
Route work to the idle lanes above before proposing `stop`: dispatch/steer for a
live agent lane, `build` for a worktree lane — highest open-backlog first.
Fill AT MOST max_dispatches_per_cycle idle lanes per cycle (the fan-out cap); the
rest next cycle. Never leave a lane idle while it has open backlog.
`stop` is valid only when no idle lane has routable backlog. Never target coord."""


def _lane_utilization_lines(
    snap: EngineSnapshot, paths: SessionPaths, config: EngineConfig | None
) -> list[str]:
    """Idle-lane inventory, config-gated by target_lane_utilization > 0: every
    non-coord lane that is NOT busy AND has open backlog, so the brain routes work
    there instead of sitting idle. Returns [] (byte-identical prompt) when the knob
    is off or no idle lane has routable backlog — mirrors _verify_drive_lines."""
    if config is None or getattr(config, "target_lane_utilization", 0.0) <= 0:
        return []
    open_backlog = _open_backlog_by_loop(paths)
    if not open_backlog:
        return []
    lines: list[str] = []
    for name in sorted(snap.lanes):
        if name == "coord":
            continue
        info = snap.lanes.get(name) or {}
        if info.get("status") in _BUSY_LANE_STATES:
            continue
        tasks = open_backlog.get(name)
        if not tasks:
            continue
        lines.append(
            f"- lane={name} status={info.get('status') or 'unknown'} "
            f"kind={info.get('kind') or '?'} open-backlog={','.join(tasks)}"
        )
    if not lines:
        return []
    return ["--- idle lanes (route work here before stop) ---", *lines]


def _roster_lines(roster: dict[str, dict], config: EngineConfig) -> list[str]:
    """Brain-prompt roster block: allowed + present + healthy harnesses only,
    so the brain physically cannot propose a bad one (the gate stays the
    backstop, not the primary funnel)."""
    policy = config.harness_policy
    lines = ["--- harness roster (allowed + present + healthy) ---"]
    for name, entry in roster.items():
        if name in policy.deny:
            continue
        if policy.allow and name not in policy.allow:
            continue
        if entry.get("present") is False:
            continue
        if str(entry.get("health", "")) in ("missing", "unauthenticated", "unhealthy"):
            continue
        lines.append(
            f"{name} tags={entry.get('capability_tags', '')} "
            f"cost={entry.get('cost_tier', '')} autonomy={entry.get('autonomy_class', '')} "
            f"drift={entry.get('drift_pins', '')}"
        )
    if len(lines) == 1:
        lines.append("(none)")
    lines.append(_HARNESS_RUBRIC)
    return lines


def _degraded_checkpoint_body() -> str:
    """F16: a header-only checkpoint body for when the assembled checkpoint prompt
    is over the token ceiling (loop-checkpoint exit 3). The contract header (small,
    bounded) carries the brain's operating instructions; ops-wiki/checkpoint.md +
    index.md are OMITTED to keep the cycle alive, with an explicit directive to
    self-trim them this cycle so the next cycle sees full state again."""
    resource = resources.files("loop_orchestrator.engine").joinpath(*_HEADER_RESOURCE)
    header = resource.read_text(encoding="utf-8")
    return (
        header.rstrip("\n") + "\n\n--- checkpoint OVERFLOW (F16) ---\n"
        "The assembled checkpoint exceeded the token ceiling, so ops-wiki/checkpoint.md\n"
        "and ops-wiki/index.md were OMITTED from this prompt to keep the cycle alive.\n"
        "Your HIGHEST-priority action this cycle: trim/rotate ops-wiki/checkpoint.md and\n"
        "ops-wiki/index.md back under the ceiling so the next cycle sees full state.\n"
    )


def _assemble_prompt(
    substrate: Substrate,
    snap: EngineSnapshot,
    paths: SessionPaths,
    config: EngineConfig | None = None,
    roster: dict[str, dict] | None = None,
    checkpoint_body: str | None = None,
) -> str:
    """checkpoint_prompt(packaged header) + lane status + restarts tail + asks
    (+ governance roster and selection rubric when a roster was resolved).

    `checkpoint_body` overrides the substrate.checkpoint_prompt call (the F16
    degrade path passes a header-only body so an over-ceiling checkpoint does not
    re-raise); None = today's behavior, fetch it from the substrate."""
    if checkpoint_body is None:
        resource = resources.files("loop_orchestrator.engine").joinpath(*_HEADER_RESOURCE)
        with resources.as_file(resource) as header:
            checkpoint_body = substrate.checkpoint_prompt(header_file=header)
    lines = [checkpoint_body.rstrip("\n"), "", "--- live lane status ---"]
    for name in sorted(snap.lanes):
        info = snap.lanes[name]
        lines.append(f"{name} {info['status']} {info['kind']}")
    # Also list ledger worktree lanes that have no live tmux pane, so the verify /
    # awaiting-build drive never references a lane absent from this roster — that
    # mismatch made the brain propose add_lane ("make the window live") instead of
    # a headless `build`. These are driven HEADLESSLY (build/verify), no pane needed.
    _ledger = read_json(paths.state_file, {})
    _loops = _ledger.get("loops") if isinstance(_ledger, dict) else None
    if isinstance(_loops, dict):
        for name in sorted(_loops):
            if name in snap.lanes or name == "coord":
                continue
            entry = _loops[name]
            branch = entry.get("branch") if isinstance(entry, dict) else None
            if isinstance(branch, str) and branch:
                lines.append(f"{name} headless-worktree branch={branch} (build/verify only)")
    lines.append("--- lane restarts (tail) ---")
    if snap.restarts_tail:
        lines.extend(json.dumps(entry, sort_keys=True) for entry in snap.restarts_tail)
    else:
        lines.append("(none)")
    lines.append("--- outstanding asks ---")
    lines.extend(_ask_lines(actions_mod.load_asks(paths), datetime.now(timezone.utc)))
    verify_drive = _verify_drive_lines(substrate, snap, paths)
    if verify_drive:
        lines.extend(verify_drive)
        lines.append(_DRIVE_RUBRIC)
    utilization = _lane_utilization_lines(snap, paths, config)
    if utilization:
        lines.extend(utilization)
        lines.append(_UTILIZATION_RUBRIC)
    if roster is not None and config is not None:
        lines.extend(_roster_lines(roster, config))
    return "\n".join(lines) + "\n"


def validate_boot_config(config: EngineConfig, substrate: Substrate) -> list[str]:
    """Fail-fast boot checks (plan A.2): the brain harness — and the ingest
    harness when ingest runs headless — must be allowed by
    harness_policy.brain_allow (empty list = unrestricted) and must have a
    non-empty registry oneshot_template. Returns human-readable failure
    messages; empty list = boot OK. An env override (LOOP_ENGINE_BRAIN_CMD /
    LOOP_ENGINE_INGEST_CMD) replaces the registry one-shot, so the template
    check is skipped for that role."""
    failures: list[str] = []
    allow = config.harness_policy.brain_allow
    checks = [("brain", config.brain.harness, "LOOP_ENGINE_BRAIN_CMD")]
    if config.ingest.mode == "headless":
        checks.append(
            ("ingest", config.ingest.harness or config.brain.harness, "LOOP_ENGINE_INGEST_CMD")
        )
    for label, harness, override_var in checks:
        if allow and harness not in allow:
            failures.append(
                f"{label}.harness {harness!r} is not in harness_policy.brain_allow "
                f"{allow} (lane-config.yaml)"
            )
        if os.environ.get(override_var):
            continue
        try:
            template = substrate.harness_field(harness, "oneshot_template")
        except SubstrateError:
            failures.append(f"{label}.harness {harness!r} is not a registered harness")
            continue
        if not template:
            failures.append(
                f"{label}.harness {harness!r} has no one-shot mode (empty "
                f"oneshot_template) — it cannot run as the {label}"
            )
    return failures


def _ingest_protocol(project_root: Path) -> str:
    """The AGENTS.md '### Ingest protocol' section, verbatim; '' when absent."""
    try:
        text = (project_root / "AGENTS.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    lines: list[str] = []
    capture = False
    for line in text.splitlines():
        if line.strip() == _INGEST_HEADING:
            capture = True
        elif capture and (line.startswith("## ") or line.startswith("### ")):
            break
        if capture:
            lines.append(line)
    return "\n".join(lines).strip()


def _ingest_argv(substrate: Substrate, config: EngineConfig, prompt: str) -> list[str]:
    """LOOP_ENGINE_INGEST_CMD overrides the registry one-shot (mirrors brain)."""
    override = os.environ.get("LOOP_ENGINE_INGEST_CMD")
    if override:
        return shlex.split(override) + [prompt]
    harness = config.ingest.harness or config.brain.harness
    argv = oneshot_argv(substrate.oneshot_template(harness), prompt)
    if config.ingest.auto_approve:
        try:
            flag = substrate.harness_field(harness, "auto_approve_flag")
        except SubstrateError:
            flag = ""
        if flag:
            argv.append(flag)
    return argv


def _quarantine_failed_ingest(
    paths: SessionPaths,
    events: EventLog,
    pending: list[str],
    reason: str,
) -> None:
    """F17: move the message a timed-out/failed headless ingest hung on OUT of
    `.loop/messages/` so the NEXT cycle does not re-ingest (and re-hang on) it.

    Quarantine rule (the simplest correct one): the headless ingest works the
    pending queue oldest-first, moving each processed file to processed/ as it
    goes, so the OLDEST message still in `.loop/messages/` is exactly the one it
    was stuck on. Move that single message to `.loop/messages/failed/` (NOT a
    delete — mailbox single-writer/add-only rules; a human can re-queue it),
    drop a sibling `<name>.ingest-failed.txt` with the reason + UTC timestamp,
    and emit one `ingest-quarantined` event (the run_oneshot `ingest-timeout`/
    `ingest-failed` event is kept). Idempotent: a message already moved (not on
    disk) is skipped, so a re-run never duplicates or crashes."""
    failed_dir = paths.mailbox_dir / "failed"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for name in pending:  # oldest first
        src = paths.mailbox_dir / name
        if not src.is_file():
            continue  # already processed/moved — keep scanning for the stuck one
        failed_dir.mkdir(parents=True, exist_ok=True)
        try:
            src.replace(failed_dir / name)  # atomic move within the mailbox tree
            (failed_dir / f"{name}.ingest-failed.txt").write_text(
                f"{ts} ingest quarantined: {reason}\n", encoding="utf-8"
            )
        except OSError as exc:
            events.append("error", kind="ingest-quarantine-failed", file=name, error=str(exc))
            return
        events.append("ingest-quarantined", file=name, reason=reason)
        return  # only the stuck (oldest-on-disk) message; the rest retry next cycle


def _headless_ingest(
    substrate: Substrate,
    paths: SessionPaths,
    events: EventLog,
    config: EngineConfig,
    pending: list[str],
) -> None:
    """One-shot harness performs the docs-lane ingest itself (no lane nudge).

    Emits ingest-done (with the re-checked pending count) on success;
    run_oneshot already emits ingest-timeout/ingest-failed on failure. On
    failure the offending message is quarantined (F17) so the cycle degrades
    instead of re-hanging on it every subsequent cycle; the caller still
    proceeds to the brain.
    """
    prompt = (
        f"You are the docs lane for the project at {paths.project_root}. "
        f"{len(pending)} mailbox message(s) are pending. PERFORM the ingest now — "
        "read each pending message under .loop/messages/, write the ops-wiki "
        "updates yourself, and move each processed file to "
        ".loop/messages/processed/ — exactly per the protocol below.\n\n"
        f"{_ingest_protocol(paths.project_root)}\n\n"
        "--- pending messages (oldest first) ---\n" + "\n".join(pending) + "\n"
    )
    try:
        argv = _ingest_argv(substrate, config, prompt)
    except SubstrateError as exc:
        events.append("error", kind="ingest-headless-failed", error=str(exc))
        return
    try:
        run_oneshot(
            argv,
            prompt,
            paths.engine_dir / "ingest",
            config.ingest.timeout_s,
            events,
            "ingest",
            cwd=paths.project_root,
            harness=config.ingest.harness or config.brain.harness,
        )
    except BrainError as exc:
        # run_oneshot appended ingest-timeout / ingest-failed already. F17:
        # quarantine the stuck message so the next cycle does not re-hang on it,
        # then degrade — the caller proceeds to the brain regardless.
        _quarantine_failed_ingest(paths, events, pending, str(exc))
        return
    try:
        remaining = substrate.pending_count()
    except SubstrateError:
        remaining = -1
    events.append("ingest-done", pending=remaining)


def _pm_adapters(config: EngineConfig, root: Path, events: EventLog) -> list[tuple[str, PMAdapter]]:
    """Instantiate the adapters named in config.pm.adapters (default [] = no
    PM layer at all: no discovery scan, no events)."""
    entries = config.pm.adapters or []
    if not entries:
        return []
    known = pm_registry.discover()
    adapters: list[tuple[str, PMAdapter]] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else str(entry)
        if not name:
            continue
        cls = known.get(name)
        if cls is None:
            events.append("pm-skip", adapter=name, reason="unknown-adapter")
            continue
        try:
            adapters.append((name, cls(project_root=root)))
        except Exception as exc:
            events.append("pm-error", adapter=name, op="init", error=str(exc))
    return adapters


def _pm_sync(
    adapters: list[tuple[str, PMAdapter]],
    op: str,
    tasks_dir: Path,
    events: EventLog,
    dry_run: bool = False,
) -> None:
    """pull/push every available adapter; failures are events, never aborts."""
    for name, adapter in adapters:
        try:
            if not adapter.available():
                events.append("pm-skip", adapter=name, op=op, reason="unavailable")
                continue
            result = getattr(adapter, op)(tasks_dir, dry_run=dry_run)
        except Exception as exc:  # adapter trouble must NEVER abort a cycle
            events.append("pm-error", adapter=name, op=op, error=str(exc))
            continue
        events.append(
            f"pm-{op}",
            adapter=name,
            created=len(result.created),
            updated=len(result.updated),
            conflicts=len(result.conflicts),
            errors=len(result.errors),
        )


def _truncate(value: str, limit: int = 200) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def action_line(action: dict) -> str:
    target = action.get("lane") or action.get("window") or "-"
    # FIRST line byte-format is asserted by tests — never change it.
    first = (
        f"{action.get('idx')}. {action.get('kind')} {target} "
        f"[{action.get('classification')}/{action.get('status')}]"
    )
    # SECOND line surfaces the executable fields a human is actually approving
    # (command mode, a raw cmd, a model id, the payload/brief) so FIX 1/2
    # approval is not blind. Absent these, no second line is emitted.
    parts: list[str] = []
    if action.get("mode") == "command":
        parts.append("mode=command")
    cmd = action.get("cmd")
    if cmd:
        parts.append(f"cmd={_truncate(str(cmd))}")
    model = action.get("model")
    if model:
        parts.append(f"model={model}")
    body = action.get("payload") or action.get("brief")
    if body:
        parts.append(f"payload={_truncate(str(body))}")
    if parts:
        return first + "\n     " + " ".join(parts)
    return first


def _persist(paths: SessionPaths, doc: dict) -> None:
    with file_lock(paths.lock_path):
        atomic_write_json(paths.pending_decision_path, doc)


def _file_needs_human(
    paths: SessionPaths,
    events: EventLog,
    approval: str,
    error: DecisionError,
    raw_text: str,
    keep: int = wiki.DEFAULT_KEEP_DECISIONS,
) -> int:
    summary = f"brain reply unusable after corrective re-prompt: {error}"
    stub = decision_mod.Decision(
        id=datetime.now(timezone.utc).strftime("d-%Y%m%d-%H%M%S"),
        critique=summary,
        actions=[],
        raw_text=raw_text,
    )
    doc = decisions.create(stub, [], approval, paths)
    doc["status"] = "needs-human"
    doc["reason"] = summary
    _persist(paths, doc)
    events.append("decision-parse-error", id=doc["id"], error=str(error))
    events.append("escalate", summary=summary)
    wiki.file_decision(paths.checkpoint_page, wiki.render_decision_entry(doc), keep=keep)
    print(f"decision {doc['id']} needs a human: {error}")
    events.append("cycle-end", outcome="needs-human")
    return 4


def run_once(
    project_root: str | Path,
    session: str,
    config: EngineConfig,
    approval_mode_override: str | None = None,
    dry_run: bool = False,
) -> int:
    root = Path(project_root)
    paths = SessionPaths(root, session)
    paths.ensure()
    events = EventLog(paths.events_path)
    events.append("cycle-start", session=session)
    approval = approval_mode_override or config.approval_mode

    substrate = Substrate(root, session)
    surface_verify_results(substrate, paths, events)
    surface_build_results(substrate, paths, events)

    pending = decisions.get(paths)
    if pending is not None:
        print(
            f"decision {pending.get('id')} is still {pending.get('status')}; "
            f"resolve it first: loop-engine approve|reject {pending.get('id')}"
        )
        events.append("error", kind="pending-exists", id=pending.get("id"))
        return 3

    if paths.paused_path.exists():
        events.append("paused")
        print(f"engine is paused ({paths.paused_path}); run loop-engine resume")
        return 5

    # B5 (T0035): refresh the ledger loops registry from the task loop: fields (the
    # source of truth) so loop-digest / the deck show every active loop. Non-
    # destructive (F5/T0024): derived status only, hand-authored fields preserved;
    # writes only when the registry changes. Runs before observe so the snapshot's
    # digest reads the freshened ledger.
    sync_loops_registry(paths, events)
    # F7 (T0029): observe degrades gracefully. A fresh snapshot is the happy path
    # (unchanged — same observe event). When the all-lanes fan-out fails under
    # load, reuse the last good snapshot (observe-stale, with its age + the
    # adaptive timeout the fan-out should scale to) so the cycle proceeds on known
    # state; with no prior snapshot to fall back on, skip the cycle (observe-failed)
    # instead of letting the SubstrateError abort it.
    try:
        snap, stale, age_s = Observer(substrate, paths).snapshot_or_stale()
    except SubstrateError as exc:
        events.append("observe-failed", error=str(exc))
        events.append("cycle-end", outcome="observe-failed")
        return 6
    if stale:
        events.append(
            "observe-stale",
            lanes=len(snap.lanes),
            mailbox_pending=len(snap.mailbox_pending),
            age_s=round(age_s) if age_s is not None else None,
            adaptive_timeout_s=round(adaptive_timeout(len(snap.lanes))),
        )
    else:
        events.append("observe", lanes=len(snap.lanes), mailbox_pending=len(snap.mailbox_pending))

    if snap.mailbox_pending and config.ingest.mode == "headless":
        _headless_ingest(substrate, paths, events, config, snap.mailbox_pending)
    elif snap.mailbox_pending and config.ingest.mode == "lane":
        nudge = (
            f"{len(snap.mailbox_pending)} mailbox message(s) pending. Run the ingest loop "
            "now, exactly as specified in AGENTS.md '### Ingest protocol': ingest "
            "oldest-first and move each processed file to .loop/messages/processed/."
        )
        try:
            # The ingest lane (e.g. coord) is a long-lived lane whose
            # accumulated session context (fleet state, prior mailbox handling)
            # the nudge relies on — never auto-/clear it (#36).
            substrate.dispatch(config.ingest.lane, nudge, wait_ready=True, no_clear=True)
        except SubstrateError as exc:
            events.append("error", kind="ingest-nudge-failed", error=str(exc))
        else:
            events.append(
                "ingest-nudge", lane=config.ingest.lane, pending=len(snap.mailbox_pending)
            )

    pm_adapters = _pm_adapters(config, root, events)
    if pm_adapters:
        _pm_sync(pm_adapters, "pull", paths.tasks_dir, events, dry_run=dry_run)

    # Harness governance (plan A.2): one roster snapshot per cycle, shared by
    # the brain prompt and the gate. Only when a policy is actually written —
    # the empty policy is a pass-through, so skip the subprocess and keep both
    # the prompt and the call profile identical to today. A roster failure
    # degrades to None (pass-through) with an event; it never aborts the cycle.
    roster = None
    lane_harnesses = None
    lane_kinds = None
    role_workers = None
    code_writers = None
    if config.harness_policy != HarnessPolicy():
        try:
            roster = substrate.harness_roster()
        except SubstrateError as exc:
            events.append("error", kind="roster-failed", error=str(exc))
        # Per-cycle lane snapshot, resolved only under a non-empty policy so the
        # empty policy keeps today's call profile. One substrate.lanes() call
        # feeds the F1 dispatch-target map (lane->harness), the T0019 standing-
        # lane drop guard (lane->kind), and the T0020 reuse-before-spawn rule
        # (per-role idle/live worker counts, correlated with this cycle's
        # observed statuses in snap.lanes). A failure degrades all to None (the
        # gate passes go inert) with an event; it never aborts the cycle.
        try:
            lane_infos = substrate.lanes()
            lane_harnesses = {info.window: info.harness for info in lane_infos if info.harness}
            # F6 (T0027): the tmux tag is a per-window fast-path; the lane-config
            # is the authoritative per-lane source. Fill any lane the tag map
            # lacks (untagged pre-existing sessions; multi-pane windows whose
            # per-lane names — e.g. validate-left/right — are never window keys)
            # from config, so harness_policy is safe on ANY session and mixed
            # windows resolve per lane. setdefault = the tag wins where present,
            # so correctly-tagged single-pane sessions stay byte-identical; no
            # lane-config => {} => unchanged (dormant).
            for lane, harness in lane_config_harnesses(root).items():
                lane_harnesses.setdefault(lane, harness)
            lane_kinds = {info.window: info.kind for info in lane_infos if info.kind}
            role_workers = {}
            # T0026: count live CODE-WRITER lanes (worker kind + an agent
            # harness — never shell/mprocs). Drives conditional worktree
            # provisioning; DORMANT at concurrency=1 (the rule resolves shared).
            code_writers = 0
            for info in lane_infos:
                if info.kind != "worker":
                    continue
                bucket = role_workers.setdefault(info.role or "", {"idle": 0, "live": 0})
                bucket["live"] += 1
                if (snap.lanes.get(info.window) or {}).get("status") == "idle":
                    bucket["idle"] += 1
                if info.harness and info.harness not in ("shell", "mprocs"):
                    code_writers += 1
            # Surface the N>=3 integration-lane recommendation (plan C.3) — an
            # observable signal the brain/operator acts on; never engine-spawned.
            if gate.needs_integration_lane(code_writers):
                events.append("integration-lane-recommended", code_writers=code_writers)
        except SubstrateError as exc:
            events.append("error", kind="lanes-failed", error=str(exc))

    # F16: the checkpoint_prompt substrate call is the ONLY one in run_once that
    # could abort the cycle before the brain runs — an over-ceiling prompt makes
    # loop-checkpoint exit 3 -> SubstrateError. Degrade like observe/ingest (F7/F11):
    # emit checkpoint-overflow and fall back to a header-only prompt so the brain
    # STILL runs and can self-trim ops-wiki/checkpoint.md + index.md this cycle.
    try:
        prompt = _assemble_prompt(substrate, snap, paths, config=config, roster=roster)
    except SubstrateError as exc:
        events.append("checkpoint-overflow", error=str(exc))
        prompt = _assemble_prompt(
            substrate,
            snap,
            paths,
            config=config,
            roster=roster,
            checkpoint_body=_degraded_checkpoint_body(),
        )

    if dry_run:
        print(f"dry-run: prompt {len(prompt)} bytes (~{len(prompt) // 4} tokens)")
        print(
            f"dry-run: would invoke brain '{config.brain.harness}', gate with "
            f"approval={approval}, execute/queue actions, and file the decision"
        )
        events.append("cycle-end", outcome="dry-run")
        return 0

    brain = Brain(config, substrate, paths, events)
    live_lanes = set(snap.lanes)
    # Headless ledger worktree lanes (a branch, no tmux pane): build/verify may
    # target these even though they are not live lanes (the eligibility-decouple).
    _ledger = read_json(paths.state_file, {})
    _loops = _ledger.get("loops") if isinstance(_ledger, dict) else None
    worktree_lanes = (
        {
            w
            for w, e in _loops.items()
            if isinstance(e, dict) and isinstance(e.get("branch"), str) and e.get("branch")
        }
        if isinstance(_loops, dict)
        else set()
    )
    try:
        reply = brain.invoke(prompt)
    except BrainError as exc:
        print(f"error: {exc}", file=sys.stderr)
        events.append("cycle-end", outcome="brain-failed")
        return 1

    try:
        parsed = decision_mod.parse_and_validate(reply, live_lanes, worktree_lanes)
    except DecisionError as first_error:
        try:
            reply = brain.invoke(prompt + _CORRECTIVE_SUFFIX.format(error=first_error))
        except BrainError as exc:
            print(f"error: {exc}", file=sys.stderr)
            events.append("cycle-end", outcome="brain-failed")
            return 1
        try:
            parsed = decision_mod.parse_and_validate(reply, live_lanes, worktree_lanes)
        except DecisionError as second_error:
            return _file_needs_human(
                paths, events, approval, second_error, reply, keep=config.checkpoint.keep_decisions
            )

    events.append("decision", id=parsed.id, actions=[a.kind for a in parsed.actions])
    # T0032 (B2) + T0045 defensive stop: a `stop` is honored only when the fleet
    # is genuinely idle AND no new mailbox work arrived after observe.
    if any(action.kind == "stop" for action in parsed.actions):
        working_lanes = {
            lane for lane, info in snap.lanes.items() if info.get("status") == "working"
        }
        if actions_mod.stop_suspected_idle_stall(substrate, working_lanes, events):
            events.append("cycle-end", outcome="stop-suspected-idle-stall")
            return 0
        # Skip the mailbox-race guard on a STALE snapshot: its baseline
        # (snap.mailbox_pending) is then old/unreliable, so diffing the fresh
        # mailbox against it would read every currently-pending message as "new"
        # and spuriously suppress the stop every cycle. A stale observe already
        # signalled degraded substrate; honor the stop (a no-op) and let a fresh
        # cycle re-evaluate.
        if not stale and actions_mod.stop_suspected_mailbox_race(
            substrate, snap.mailbox_pending, events
        ):
            events.append("cycle-end", outcome="stop-suspected-mailbox-race")
            return 0
    governed, governance_events = gate.govern_add_lanes(parsed.actions, config, roster)
    for governance_event in governance_events:
        events.append("governance", **governance_event)
    if governance_events:
        parsed = dataclasses.replace(parsed, actions=governed)
    classifications = gate.classify_batch(
        parsed.actions, len(snap.lanes), config, roster, lane_harnesses, lane_kinds, role_workers
    )
    events.append("gate", id=parsed.id, classifications=classifications)

    doc = decisions.create(parsed, classifications, approval, paths)
    autos = [
        action
        for action in doc["actions"]
        if action["status"] == "awaiting-approval"
        and action["classification"] in _AUTO_CLASSES.get(approval, frozenset())
        # An escalate is the loop's explicit request for human judgment — it must
        # reach an operator as a pending decision in EVERY mode, including `full`
        # (where the destructive class otherwise self-executes). Never auto-promote
        # it, regardless of classification. (gate.py also classes it DESTRUCTIVE so
        # auto/manual gate it; this is the belt-and-suspenders for full mode.)
        and action["kind"] != "escalate"
    ]
    if autos:
        for action in autos:
            action["status"] = "auto"
        _persist(paths, doc)
        doc = actions_mod.execute_batch(
            doc, substrate, events, config, paths=paths, code_writers=code_writers
        )
        _persist(paths, doc)

    awaiting = [a for a in doc["actions"] if a["status"] == "awaiting-approval"]
    if awaiting:
        events.append("decision-pending", id=doc["id"], awaiting=[a["idx"] for a in awaiting])
        print(f"decision {doc['id']} awaits approval (mode={approval}):")
        for action in doc["actions"]:
            print(f"  {action_line(action)}")
        print(f"approve with: loop-engine approve {doc['id']}")
    else:
        doc = decisions.resolve(
            paths,
            doc["id"],
            approve=True,
            decided_by="engine",
            reason="auto-resolved: nothing awaiting approval",
        )
        decisions.archive(paths, doc)
        events.append("decision-approved", id=doc["id"], decided_by="engine")
        print(f"decision {doc['id']} completed; nothing awaits approval")
        for action in doc["actions"]:
            print(f"  {action_line(action)}")

    if pm_adapters:  # after action execution (and in the no-actions path)
        _pm_sync(pm_adapters, "push", paths.tasks_dir, events)

    wiki.file_decision(
        paths.checkpoint_page,
        wiki.render_decision_entry(doc),
        keep=config.checkpoint.keep_decisions,
    )
    events.append("cycle-end", outcome="pending" if awaiting else "resolved")
    return 0
