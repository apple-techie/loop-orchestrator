"""One full engine cycle: observe -> ingest nudge -> brain -> gate -> act.

Exit codes: 0 completed cycle (stdout says whether approval is pending),
1 brain invocation failure, 3 a decision is already in flight (single
in-flight invariant), 4 the brain would not produce a usable decision even
after one corrective re-prompt (needs-human doc filed), 5 paused.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from ..locking import atomic_write_json, file_lock
from ..paths import SessionPaths
from ..substrate import Substrate, SubstrateError
from . import actions as actions_mod
from . import decision as decision_mod
from . import decisions, gate, wiki
from .brain import Brain, BrainError
from .config import EngineConfig
from .decision import DecisionError
from .events import EventLog
from .observe import EngineSnapshot, Observer

_HEADER_RESOURCE = ("contracts", "checkpoint-header.md")

_CORRECTIVE_SUFFIX = (
    "\n\nYour previous reply could not be used: {error}. Reply with ONLY the fenced decision block."
)

# approval mode -> classifications that execute without a human (blocked never runs).
_AUTO_CLASSES: dict[str, frozenset[str]] = {
    "manual": frozenset(),
    "auto": frozenset({"safe"}),
    "full": frozenset({"safe", "destructive"}),
}


def _assemble_prompt(substrate: Substrate, snap: EngineSnapshot) -> str:
    """checkpoint_prompt(packaged header) + live lane status + restarts tail."""
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
    return "\n".join(lines) + "\n"


def action_line(action: dict) -> str:
    target = action.get("lane") or action.get("window") or "-"
    return (
        f"{action.get('idx')}. {action.get('kind')} {target} "
        f"[{action.get('classification')}/{action.get('status')}]"
    )


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

    if snap.mailbox_pending and config.ingest.mode == "lane":
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

    prompt = _assemble_prompt(substrate, snap)

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
    classifications = gate.classify_batch(parsed.actions, len(snap.lanes), config)
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
        doc = actions_mod.execute_batch(doc, substrate, events, config)
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

    wiki.file_decision(paths.checkpoint_page, wiki.render_decision_entry(doc))
    events.append("cycle-end", outcome="pending" if awaiting else "resolved")
    return 0
