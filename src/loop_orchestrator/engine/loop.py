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

from ..locking import atomic_write_json, file_lock
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
_VERIFY_DRIVE_OUTCOME_EVENTS = frozenset(
    {"verify-passed", "verify-failed", "verify-timeout"}
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
                event = "verify-passed" if overall == "pass" else "verify-failed"
                events.append(
                    event,
                    window=marker.get("window"),
                    overall=overall,
                    findings=findings,
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


def _recent_verify_outcomes(paths: SessionPaths) -> list[dict]:
    outcomes = [
        event
        for event in EventLog(paths.events_path).tail(_VERIFY_DRIVE_EVENT_TAIL)
        if event.get("event") in _VERIFY_DRIVE_OUTCOME_EVENTS
        and isinstance(event.get("window"), str)
        and event.get("window")
    ]
    return outcomes[-_VERIFY_DRIVE_OUTCOME_LIMIT:]


def _loop_updated_after_outcome(snap: EngineSnapshot, window: str, outcome: dict) -> bool:
    entry = snap.loops.get(window)
    updated_at = entry.get("updated_at") if isinstance(entry, dict) else None
    event_ts = outcome.get("ts")
    if not isinstance(updated_at, str) or not isinstance(event_ts, str):
        return False
    try:
        return parse_ts(updated_at) > parse_ts(event_ts)
    except ValueError:
        return False


def _verify_drive_lines(snap: EngineSnapshot, paths: SessionPaths) -> list[str]:
    markers = actions_mod.load_verify_markers(paths)
    in_flight_windows = {
        window
        for marker in markers
        if isinstance((window := marker.get("window")), str) and window
    }
    outcomes = _recent_verify_outcomes(paths)
    latest_outcome = {
        str(outcome["window"]): outcome
        for outcome in outcomes
        if isinstance(outcome.get("window"), str)
    }

    ready: list[str] = []
    for lane in sorted(snap.lanes):
        info = snap.lanes[lane]
        if info.get("status") != "idle" or lane in in_flight_windows:
            continue
        branch = actions_mod._predecessor_branch(paths, lane)
        if branch is None:
            continue
        outcome = latest_outcome.get(lane)
        if outcome is not None and not _loop_updated_after_outcome(snap, lane, outcome):
            continue
        ready.append(f"- lane={lane} branch={branch} status=idle")

    lines: list[str] = []
    if ready:
        lines.append("ready-to-verify:")
        lines.extend(ready)
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
--- verify drive rubric (first match wins) ---
ready-to-verify lane -> propose `verify` for that lane.
recent verify-passed -> propose `escalate` summary: merge <branch> — verified, N findings.
recent verify-failed/verify-timeout -> propose `dispatch`/`steer` fix to that lane.
Include the named findings in the fix payload.
Never merge directly; the escalate action is the single human gate.
Never escalate-merge a failed or timed-out build."""


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
    lines.append("--- lane restarts (tail) ---")
    if snap.restarts_tail:
        lines.extend(json.dumps(entry, sort_keys=True) for entry in snap.restarts_tail)
    else:
        lines.append("(none)")
    lines.append("--- outstanding asks ---")
    lines.extend(_ask_lines(actions_mod.load_asks(paths), datetime.now(timezone.utc)))
    verify_drive = _verify_drive_lines(snap, paths)
    if verify_drive:
        lines.extend(verify_drive)
        lines.append(_DRIVE_RUBRIC)
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
    try:
        reply = brain.invoke(prompt)
    except BrainError as exc:
        print(f"error: {exc}", file=sys.stderr)
        events.append("cycle-end", outcome="brain-failed")
        return 1

    try:
        parsed = decision_mod.parse_and_validate(reply, live_lanes)
    except DecisionError as first_error:
        try:
            reply = brain.invoke(prompt + _CORRECTIVE_SUFFIX.format(error=first_error))
        except BrainError as exc:
            print(f"error: {exc}", file=sys.stderr)
            events.append("cycle-end", outcome="brain-failed")
            return 1
        try:
            parsed = decision_mod.parse_and_validate(reply, live_lanes)
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
