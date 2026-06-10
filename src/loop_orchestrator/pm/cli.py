"""loop-pm CLI.

P2 scaffold: adapter discovery works (entry points); sync exits 64 until the
PM adapter phase ships implementations. Exit 64 matches the bash stub's
"implementation unavailable" contract (CONTRACT.md).
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import entry_points


def discovered_adapters() -> dict[str, object]:
    return {ep.name: ep for ep in entry_points(group="loop_orchestrator.pm_adapters")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loop-pm",
        description="PM adapter sync against tasks/ files (file-wins conflict rule).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-adapters", help="list installed PM adapters")
    sync = sub.add_parser("sync", help="sync tasks/ with a PM adapter")
    sync.add_argument("--adapter", required=True)
    sync.add_argument("direction", nargs="?", default="both", choices=["pull", "push", "both"])
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--tasks-dir", default="tasks")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    adapters = discovered_adapters()
    if args.command == "list-adapters":
        if not adapters:
            print("(no PM adapters installed)")
        for name in sorted(adapters):
            print(name)
        return 0
    if args.command == "sync":
        if args.adapter not in adapters:
            known = ", ".join(sorted(adapters)) or "none installed"
            print(f"loop-pm: unknown adapter '{args.adapter}' ({known})", file=sys.stderr)
            return 64
        print("loop-pm sync: not implemented in this build", file=sys.stderr)
        return 64
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
