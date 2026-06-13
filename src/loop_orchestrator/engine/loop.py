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
import sys
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from ..locking import atomic_write_json, file_lock
from ..paths import SessionPaths
from ..pm import registry as pm_registry
from ..pm.base import PMAdapter
from ..substrate import Substrate, SubstrateError
from . import actions as actions_mod
from . import decision as decision_mod
from . import decisions, gate, wiki
from .brain import Brain, BrainError, oneshot_argv, run_oneshot
from .config import EngineConfig, HarnessPolicy
from .decision import DecisionError
from .events import EventLog, parse_ts
from .observe import EngineSnapshot, Observer

_HEADER_RESOURCE = ("contracts", "checkpoint-header.md")

_CORRECTIVE_SUFFIX = (
    "\n\nYour previous reply could not be used: {error}. Reply with ONLY the fenced decision block."
)

_INGEST_HEADING = "### Ingest protocol"

# Timed-out asks stay visible in the checkpoint addendum this long.
_ASK_TIMEOUT_RECENCY_S = 3600

# approval mode -> classifications that execute without a human (blocked never runs).
_AUTO_CLASSES: dict[str, frozenset[str]] = {
    "manual": frozenset(),
    "auto": frozenset({"safe"}),
    "full": frozenset({"safe", "destructive"}),
}


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


def _assemble_prompt(
    substrate: Substrate,
    snap: EngineSnapshot,
    paths: SessionPaths,
    config: EngineConfig | None = None,
    roster: dict[str, dict] | None = None,
) -> str:
    """checkpoint_prompt(packaged header) + lane status + restarts tail + asks
    (+ governance roster and selection rubric when a roster was resolved)."""
    resource = resources.files("loop_orchestrator.engine").joinpath(*_HEADER_RESOURCE)
    with resources.as_file(resource) as header:
        prompt = substrate.checkpoint_prompt(header_file=header)
    lines = [prompt.rstrip("\n"), "", "--- live lane status ---"]
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


def _headless_ingest(
    substrate: Substrate,
    paths: SessionPaths,
    events: EventLog,
    config: EngineConfig,
    pending: list[str],
) -> None:
    """One-shot harness performs the docs-lane ingest itself (no lane nudge).

    Emits ingest-done (with the re-checked pending count) on success;
    run_oneshot already emits ingest-timeout/ingest-failed on failure.
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
    except BrainError:
        return  # run_oneshot appended ingest-timeout / ingest-failed already
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
    wiki.file_decision(paths.checkpoint_page, wiki.render_decision_entry(doc))
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

    substrate = Substrate(root, session)
    snap = Observer(substrate, paths).snapshot()
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
            substrate.dispatch(config.ingest.lane, nudge, wait_ready=True)
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
    if config.harness_policy != HarnessPolicy():
        try:
            roster = substrate.harness_roster()
        except SubstrateError as exc:
            events.append("error", kind="roster-failed", error=str(exc))
        # F1 dispatch-target governance (Phase 2): the per-cycle lane->harness
        # map, resolved only under a non-empty policy so the empty policy keeps
        # today's call profile. A failure degrades to None (the F1 gate pass
        # goes inert) with an event; it never aborts the cycle.
        try:
            lane_harnesses = {
                info.window: info.harness for info in substrate.lanes() if info.harness
            }
        except SubstrateError as exc:
            events.append("error", kind="lanes-failed", error=str(exc))

    prompt = _assemble_prompt(substrate, snap, paths, config=config, roster=roster)

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
            return _file_needs_human(paths, events, approval, second_error, reply)

    events.append("decision", id=parsed.id, actions=[a.kind for a in parsed.actions])
    governed, governance_events = gate.govern_add_lanes(parsed.actions, config, roster)
    for governance_event in governance_events:
        events.append("governance", **governance_event)
    if governance_events:
        parsed = dataclasses.replace(parsed, actions=governed)
    classifications = gate.classify_batch(
        parsed.actions, len(snap.lanes), config, roster, lane_harnesses
    )
    events.append("gate", id=parsed.id, classifications=classifications)

    doc = decisions.create(parsed, classifications, approval, paths)
    autos = [
        action
        for action in doc["actions"]
        if action["status"] == "awaiting-approval"
        and action["classification"] in _AUTO_CLASSES.get(approval, frozenset())
    ]
    if autos:
        for action in autos:
            action["status"] = "auto"
        _persist(paths, doc)
        doc = actions_mod.execute_batch(doc, substrate, events, config, paths=paths)
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

    wiki.file_decision(paths.checkpoint_page, wiki.render_decision_entry(doc))
    events.append("cycle-end", outcome="pending" if awaiting else "resolved")
    return 0
