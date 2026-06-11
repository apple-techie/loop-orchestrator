"""loop-pm CLI — list PM adapters and sync tasks/ files with them.

Exit codes: 0 success (conflicts are reported on stderr but are NOT failures —
under the file-wins rule a conflict is a correctly-handled divergence),
1 adapter errors, 64 unknown or unavailable adapter (matches the bash stub's
"implementation unavailable" contract in CONTRACT.md).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .base import PMAdapter, PMSyncResult
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
    failed = False
    for direction in directions:
        run = adapter.pull if direction == "pull" else adapter.push
        result = run(tasks_dir, dry_run=args.dry_run)
        _print_result(direction, result)
        failed = failed or bool(result.errors)
    return 1 if failed else 0


def main(argv: list[str] | None = None, registry: dict[str, type[PMAdapter]] | None = None) -> int:
    args = build_parser().parse_args(argv)
    adapters = registry if registry is not None else discover()
    if args.command == "list-adapters":
        return cmd_list_adapters(adapters)
    if args.command == "sync":
        return cmd_sync(args, adapters)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
