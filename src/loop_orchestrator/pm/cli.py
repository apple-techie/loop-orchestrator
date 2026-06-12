"""loop-pm CLI — list PM adapters, sync tasks/ files with them, and drive the
Jira scrum verbs (epics, sprints, retrospectives).

Exit codes: 0 success (conflicts/warnings are reported on stderr but are NOT
failures — under the file-wins rule a conflict is a correctly-handled
divergence), 1 adapter/API errors, 64 unknown or unavailable adapter / missing
environment (matches the bash stub's "implementation unavailable" contract in
CONTRACT.md).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .base import PMAdapter, PMSyncResult
from .jira import ENV_BOARD, ENV_PROJECT, JiraAdapter, JiraError
from .registry import discover


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loop-pm",
        description="PM adapter sync against tasks/ files (file-wins conflict rule).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-adapters", help="list installed PM adapters and their availability")
    sync = sub.add_parser("sync", help="sync tasks/ with a PM adapter")
    sync.add_argument("--adapter", required=True)
    sync.add_argument("direction", nargs="?", default="both", choices=["pull", "push", "both"])
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--tasks-dir", default=None, help="default: <project-root>/tasks")
    sync.add_argument(
        "--project-root", default=".", help="repo root holding ops-wiki/ (default: cwd)"
    )
    sync.add_argument("--project", default=None, help=f"push: project key (default ${ENV_PROJECT})")
    sync.add_argument("--epic", default=None, help="push: link created issues under this epic key")
    sync.add_argument(
        "--sprint", default=None, help="push: move created issues to this sprint id (or 'active')"
    )
    sync.add_argument(
        "--board", default=None, help=f"push: board for --sprint active (default ${ENV_BOARD})"
    )

    jira = sub.add_parser("jira", help="Jira scrum verbs (epics, sprints, retrospectives)")
    jira_sub = jira.add_subparsers(dest="jira_command", required=True)

    ensure = jira_sub.add_parser("ensure-epic", help="find or create an epic; prints its key")
    ensure.add_argument("--name", required=True)
    ensure.add_argument("--project", default=None, help=f"default ${ENV_PROJECT}")

    status = jira_sub.add_parser("sprint-status", help="print the board's active sprint")
    status.add_argument("--board", default=None, help=f"default ${ENV_BOARD}")

    move = jira_sub.add_parser("move-to-sprint", help="move issues into a sprint")
    which = move.add_mutually_exclusive_group(required=True)
    which.add_argument("--sprint", default=None, help="sprint id")
    which.add_argument("--active", action="store_true", help="resolve the board's active sprint")
    move.add_argument("--board", default=None, help=f"for --active (default ${ENV_BOARD})")
    move.add_argument("keys", nargs="+", metavar="KEY")

    start = jira_sub.add_parser(
        "start-sprint", help="activate a sprint (sets start/end dates from now)"
    )
    which = start.add_mutually_exclusive_group(required=True)
    which.add_argument("--sprint", default=None, help="sprint id")
    which.add_argument("--next", action="store_true", help="earliest future sprint on the board")
    which.add_argument(
        "--create", default=None, metavar="NAME", help="create a sprint, then start it"
    )
    start.add_argument("--board", default=None, help=f"for --next/--create (default ${ENV_BOARD})")
    start.add_argument("--duration-days", type=int, default=14, help="sprint length (default 14)")
    start.add_argument("--goal", default=None, help="sprint goal")

    complete = jira_sub.add_parser(
        "complete-sprint", help="close a sprint (Jira moves incomplete issues to the backlog)"
    )
    which = complete.add_mutually_exclusive_group(required=True)
    which.add_argument("--sprint", default=None, help="sprint id")
    which.add_argument("--active", action="store_true", help="resolve the board's active sprint")
    complete.add_argument("--board", default=None, help=f"for --active (default ${ENV_BOARD})")

    complete_epic = jira_sub.add_parser(
        "complete-epic", help="transition an epic to Done (sprint/issue completion doesn't roll up)"
    )
    complete_epic.add_argument("key", help="epic issue key, e.g. SCRUM-117")

    retro = jira_sub.add_parser("retro", help="post a retrospective onto an epic")
    retro.add_argument("--epic", required=True, help="epic issue key")
    retro.add_argument("--title", default=None, help="default: 'Retrospective <YYYY-MM-DD>'")
    body = retro.add_mutually_exclusive_group(required=True)
    body.add_argument("--body-file", default=None)
    body.add_argument("--body", default=None)
    retro.add_argument(
        "--as-issue",
        action="store_true",
        help="create a Task labeled 'retrospective' under the epic instead of a comment",
    )
    retro.add_argument(
        "--project", default=None, help="for --as-issue (default: the epic key's project)"
    )
    return parser


def _print_result(direction: str, result: PMSyncResult) -> None:
    suffix = " [dry-run]" if result.dry_run else ""
    print(
        f"{direction}: {len(result.created)} created, {len(result.updated)} updated, "
        f"{len(result.conflicts)} conflict(s), {len(result.errors)} error(s){suffix}"
    )
    for path in result.created:
        print(f"  created {path}")
    for item in result.updated:
        print(f"  updated {item}")
    for conflict in result.conflicts:
        print(f"conflict (file wins): {conflict}", file=sys.stderr)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in result.errors:
        print(f"error: {error}", file=sys.stderr)


def cmd_list_adapters(adapters: dict[str, type[PMAdapter]]) -> int:
    if not adapters:
        print("(no PM adapters installed)")
        return 0
    for name in sorted(adapters):
        try:
            adapter = adapters[name]()
        except Exception as exc:
            print(f"{name}  unavailable (failed to construct: {exc})")
            continue
        if adapter.available():
            print(f"{name}  available")
        else:
            missing = ", ".join(adapter.validate_env()) or "unknown reason"
            print(f"{name}  unavailable (missing: {missing})")
    return 0


def cmd_sync(args: argparse.Namespace, adapters: dict[str, type[PMAdapter]]) -> int:
    if args.adapter not in adapters:
        known = ", ".join(sorted(adapters)) or "none installed"
        print(f"loop-pm: unknown adapter '{args.adapter}' (known: {known})", file=sys.stderr)
        return 64
    root = Path(args.project_root).resolve()
    adapter = adapters[args.adapter](project_root=root)
    if not adapter.available():
        missing = ", ".join(adapter.validate_env()) or "unknown reason"
        print(
            f"loop-pm: adapter '{args.adapter}' is not available — missing environment "
            f"variable(s): {missing}",
            file=sys.stderr,
        )
        return 64
    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else root / "tasks"
    directions = ["pull", "push"] if args.direction == "both" else [args.direction]
    push_options = {
        name: getattr(args, name)
        for name in ("project", "epic", "sprint", "board")
        if getattr(args, name) is not None
    }
    failed = False
    for direction in directions:
        if direction == "push" and push_options:
            try:
                result = adapter.push(tasks_dir, dry_run=args.dry_run, **push_options)
            except TypeError:
                flags = ", ".join(f"--{name}" for name in sorted(push_options))
                print(
                    f"loop-pm: adapter '{args.adapter}' does not support {flags}",
                    file=sys.stderr,
                )
                return 1
        else:
            run = adapter.pull if direction == "pull" else adapter.push
            result = run(tasks_dir, dry_run=args.dry_run)
        _print_result(direction, result)
        failed = failed or bool(result.errors)
    return 1 if failed else 0


# ── jira scrum verbs ─────────────────────────────────────────────────────────


def _require(value: str | None, env_var: str, flag: str) -> str:
    """Flag value, else the env var — exit 64 when neither is set (the verb
    needs it; creds-style 'environment not configured' contract)."""
    resolved = value or os.environ.get(env_var)
    if not resolved:
        print(f"loop-pm: {env_var} is not set and {flag} was not given", file=sys.stderr)
        raise SystemExit(64)
    return resolved


def _resolve_sprint_id(adapter: JiraAdapter, args: argparse.Namespace) -> str | int:
    if not args.active:
        return args.sprint
    board = _require(args.board, ENV_BOARD, "--board")
    sprint = adapter.active_sprint(board)
    if sprint is None:
        print(f"loop-pm: board {board} has no active sprint", file=sys.stderr)
        raise SystemExit(1)
    return sprint["id"]


def cmd_jira(args: argparse.Namespace, adapter: JiraAdapter | None = None) -> int:
    adapter = adapter or JiraAdapter()
    missing = adapter.validate_env()
    if missing:
        print(
            f"loop-pm: jira is not available — missing environment variable(s): "
            f"{', '.join(missing)}",
            file=sys.stderr,
        )
        return 64
    try:
        if args.jira_command == "ensure-epic":
            project = _require(args.project, ENV_PROJECT, "--project")
            key = adapter.find_epic(args.name, project) or adapter.create_epic(args.name, project)
            print(key)
        elif args.jira_command == "sprint-status":
            board = _require(args.board, ENV_BOARD, "--board")
            sprint = adapter.active_sprint(board)
            if sprint is None:
                print("no active sprint")
            else:
                print(f"{sprint['id']} {sprint['name']}")
        elif args.jira_command == "move-to-sprint":
            sprint_id = _resolve_sprint_id(adapter, args)
            adapter.move_to_sprint(sprint_id, args.keys)
            print(f"moved {len(args.keys)} issue(s) to sprint {sprint_id}")
        elif args.jira_command == "start-sprint":
            return _cmd_jira_start_sprint(args, adapter)
        elif args.jira_command == "complete-sprint":
            return _cmd_jira_complete_sprint(args, adapter)
        elif args.jira_command == "complete-epic":
            new_status = adapter.complete_epic(args.key)
            print(f"{args.key} -> {new_status}")
        elif args.jira_command == "retro":
            return _cmd_jira_retro(args, adapter)
    except JiraError as exc:
        print(f"loop-pm: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_jira_start_sprint(args: argparse.Namespace, adapter: JiraAdapter) -> int:
    if args.create is not None:
        board = _require(args.board, ENV_BOARD, "--board")
        sprint = adapter.create_sprint(board, args.create)
        print(f"created sprint {sprint['id']} {sprint['name']}")
        sprint_id = sprint["id"]
    elif args.next:
        board = _require(args.board, ENV_BOARD, "--board")
        future = adapter.future_sprints(board)
        if not future:
            print(
                f"loop-pm: board {board} has no future sprint (create one with --create NAME)",
                file=sys.stderr,
            )
            return 1
        sprint_id = min(future, key=lambda sprint: sprint["id"])["id"]
    else:
        sprint_id = args.sprint
    adapter.start_sprint(sprint_id, duration_days=args.duration_days, goal=args.goal)
    goal_suffix = f" — goal: {args.goal}" if args.goal else ""
    print(f"started sprint {sprint_id} ({args.duration_days} days){goal_suffix}")
    return 0


def _cmd_jira_complete_sprint(args: argparse.Namespace, adapter: JiraAdapter) -> int:
    sprint_id = _resolve_sprint_id(adapter, args)
    closed = adapter.complete_sprint(sprint_id)
    name = closed.get("name")
    print(f"closed sprint {sprint_id}" + (f" {name}" if name else ""))
    print("note: Jira moves a closed sprint's incomplete issues back to the backlog")
    return 0


def _cmd_jira_retro(args: argparse.Namespace, adapter: JiraAdapter) -> int:
    if args.body_file is not None:
        try:
            text = Path(args.body_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"loop-pm: cannot read --body-file: {exc}", file=sys.stderr)
            return 1
    else:
        text = args.body
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = args.title or f"Retrospective {today}"
    if args.as_issue:
        project = args.project or args.epic.split("-")[0]
        key, warning = adapter.create_issue(
            title, text, project, epic_key=args.epic, labels=["retrospective"]
        )
        if warning:
            print(f"warning: {warning}", file=sys.stderr)
        print(key)
    else:
        adapter.add_comment(args.epic, f"{title}\n{text}")
        print(f"retro comment added to {args.epic}")
    return 0


def main(
    argv: list[str] | None = None,
    registry: dict[str, type[PMAdapter]] | None = None,
    jira_adapter: JiraAdapter | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    adapters = registry if registry is not None else discover()
    if args.command == "list-adapters":
        return cmd_list_adapters(adapters)
    if args.command == "sync":
        return cmd_sync(args, adapters)
    if args.command == "jira":
        return cmd_jira(args, jira_adapter)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
