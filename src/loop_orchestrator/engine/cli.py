"""loop-engine CLI — once / observe / status / approve / reject / pause /
resume / cycle-now over the engine modules. `watch` lands in the daemon phase.

Session resolution: --session, then $LOOP_SESSION, else exit 2. Approve and
reject perform the single CAS transition on pending-decision.json, execute (on
approve), archive, and append the resolution entry to the checkpoint page.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..paths import SessionPaths
from ..substrate import Substrate
from . import decisions, wiki
from .actions import execute_batch
from .config import load_config
from .decisions import DecisionStateError
from .events import EventLog
from .loop import action_line, run_once
from .observe import Observer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loop-engine",
        description="Deterministic orchestration engine for loop-orchestrator sessions.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="repo root containing .loop/ and ops-wiki/ (default: cwd)",
    )
    parser.add_argument("--session", help="tmux session name (default: $LOOP_SESSION)")

    sub = parser.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="run a single engine cycle")
    once.add_argument(
        "--dry-run",
        action="store_true",
        help="observe and report; never call the brain or dispatch",
    )
    once.add_argument(
        "--approval",
        choices=["manual", "auto", "full"],
        default=None,
        help="override config approval mode for this cycle",
    )

    sub.add_parser("observe", help="write a fresh snapshot + observe event, no cycle")
    sub.add_parser("status", help="print pending decision summary + last events")
    sub.add_parser("watch", help="run the engine daemon (poll + cycle on triggers)")

    approve = sub.add_parser("approve", help="approve the pending decision and execute it")
    approve.add_argument("decision_id")
    approve.add_argument("--actions", help="comma-separated action indices (default: all)")

    reject = sub.add_parser("reject", help="reject the pending decision")
    reject.add_argument("decision_id")
    reject.add_argument("--reason", default="")

    sub.add_parser("pause", help="pause brain calls and action execution")
    sub.add_parser("resume", help="resume after pause")
    sub.add_parser("cycle-now", help="request an immediate cycle from the daemon")

    return parser


def _session(args: argparse.Namespace) -> str:
    session = args.session or os.environ.get("LOOP_SESSION")
    if session:
        return session
    print("error: --session <name> (or $LOOP_SESSION) is required", file=sys.stderr)
    raise SystemExit(2)


def _parse_indices(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    try:
        return [int(part) for part in raw.split(",") if part.strip()]
    except ValueError:
        print(f"error: --actions must be comma-separated integers, got {raw!r}", file=sys.stderr)
        raise SystemExit(2) from None


def cmd_once(args: argparse.Namespace, root: Path) -> int:
    session = _session(args)
    config = load_config(root)
    return run_once(
        root,
        session,
        config,
        approval_mode_override=args.approval,
        dry_run=args.dry_run,
    )


def cmd_observe(args: argparse.Namespace, root: Path) -> int:
    session = _session(args)
    paths = SessionPaths(root, session)
    paths.ensure()
    snap = Observer(Substrate(root, session), paths).snapshot()
    EventLog(paths.events_path).append(
        "observe", lanes=len(snap.lanes), mailbox_pending=len(snap.mailbox_pending)
    )
    print(f"snapshot written: {paths.snapshot_path}")
    for name in sorted(snap.lanes):
        info = snap.lanes[name]
        print(f"  {name:16s} {info['status']:18s} {info['kind']}")
    print(f"  mailbox pending: {len(snap.mailbox_pending)}")
    return 0


def cmd_status(args: argparse.Namespace, root: Path) -> int:
    session = _session(args)
    paths = SessionPaths(root, session)
    doc = decisions.get(paths)
    if doc is None:
        print("no pending decision")
    else:
        print(
            f"pending decision {doc.get('id')} ({doc.get('status')}, "
            f"mode={doc.get('approval_mode')}):"
        )
        for action in doc.get("actions") or []:
            print(f"  {action_line(action)}")
    tail = EventLog(paths.events_path).tail(5)
    if tail:
        print("last events:")
        for event in tail:
            extras = {k: v for k, v in event.items() if k not in ("ts", "seq", "event")}
            suffix = f" {json.dumps(extras, sort_keys=True)}" if extras else ""
            print(f"  {event.get('ts')} #{event.get('seq')} {event.get('event')}{suffix}")
    return 0


def _resolve_and_finish(args: argparse.Namespace, root: Path, approve: bool) -> int:
    session = _session(args)
    paths = SessionPaths(root, session)
    paths.ensure()
    events = EventLog(paths.events_path)
    indices = _parse_indices(getattr(args, "actions", None))
    try:
        doc = decisions.resolve(
            paths,
            args.decision_id,
            approve=approve,
            action_indices=indices,
            decided_by=os.environ.get("USER", "human"),
            reason=getattr(args, "reason", ""),
        )
    except DecisionStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if approve:
        events.append("decision-approved", id=doc["id"], indices=indices)
        config = load_config(root)
        doc = execute_batch(doc, Substrate(root, session), events, config)
    else:
        events.append("decision-rejected", id=doc["id"], reason=doc.get("reason", ""))
    decisions.archive(paths, doc)
    wiki.file_decision(paths.checkpoint_page, wiki.render_decision_entry(doc))
    print(f"decision {doc['id']} {doc['status']} and archived:")
    for action in doc.get("actions") or []:
        print(f"  {action_line(action)}")
    failed = [a["idx"] for a in doc.get("actions") or [] if a.get("status") == "failed"]
    if failed:
        print(f"error: action(s) {failed} failed; see events.jsonl", file=sys.stderr)
        return 1
    return 0


def cmd_approve(args: argparse.Namespace, root: Path) -> int:
    return _resolve_and_finish(args, root, approve=True)


def cmd_reject(args: argparse.Namespace, root: Path) -> int:
    return _resolve_and_finish(args, root, approve=False)


def cmd_pause(args: argparse.Namespace, root: Path) -> int:
    session = _session(args)
    paths = SessionPaths(root, session)
    paths.ensure()
    paths.paused_path.touch()
    EventLog(paths.events_path).append("paused", by=os.environ.get("USER", "human"))
    print(f"paused ({paths.paused_path})")
    return 0


def cmd_resume(args: argparse.Namespace, root: Path) -> int:
    session = _session(args)
    paths = SessionPaths(root, session)
    paths.ensure()
    try:
        paths.paused_path.unlink()
    except FileNotFoundError:
        print("engine was not paused")
        return 0
    EventLog(paths.events_path).append("resumed", by=os.environ.get("USER", "human"))
    print("resumed")
    return 0


def cmd_cycle_now(args: argparse.Namespace, root: Path) -> int:
    session = _session(args)
    paths = SessionPaths(root, session)
    paths.ensure()
    paths.cycle_now_path.touch()
    print(f"cycle requested ({paths.cycle_now_path})")
    return 0


def cmd_watch(args: argparse.Namespace, root: Path) -> int:
    print("loop-engine watch: lands in the daemon phase", file=sys.stderr)
    return 2


_HANDLERS = {
    "once": cmd_once,
    "observe": cmd_observe,
    "status": cmd_status,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "cycle-now": cmd_cycle_now,
    "watch": cmd_watch,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.project_root).resolve()
    return _HANDLERS[args.command](args, root)


if __name__ == "__main__":
    raise SystemExit(main())
