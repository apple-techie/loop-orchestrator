"""loop-deck CLI.

`--check` is the CI smoke mode (imports + path resolution, no TTY); the
default path launches the Textual app. Session resolution: --session, then
$LOOP_SESSION, else exit 2.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..paths import normalize_project_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loop-deck",
        description="Interactive flight deck for loop-orchestrator sessions.",
    )
    parser.add_argument("--project-root", default=".", help="repo root (default: cwd)")
    parser.add_argument("--session", help="tmux session name (default: $LOOP_SESSION)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="smoke check: imports + path resolution, no TTY needed",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="cross-session fleet view (read-only) of every loop; no TTY/session needed",
    )
    parser.add_argument(
        "--roots",
        help="comma-separated project roots to scan for --all (default: --project-root)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.all:
        # Cross-session fleet view (T0033/B3): read-only aggregation across roots.
        # No TTY, no session required, never writes — the deck non-writer invariant.
        from ..substrate import discover_loops, render_fleet

        if args.roots:
            roots = [Path(part).expanduser() for part in args.roots.split(",") if part.strip()]
        else:
            roots = [Path(args.project_root)]
        print(render_fleet(discover_loops(roots)))
        return 0
    if args.check:
        try:
            import textual  # noqa: F401
        except ImportError:
            print("loop-deck --check: textual is not installed", file=sys.stderr)
            return 1
        root = normalize_project_root(args.project_root)
        print(f"loop-deck --check: ok (textual importable, project={root})")
        return 0
    session = args.session or os.environ.get("LOOP_SESSION")
    if not session:
        print("error: --session <name> (or $LOOP_SESSION) is required", file=sys.stderr)
        return 2
    root = normalize_project_root(args.project_root)

    from ..substrate import Substrate
    from .app import LoopDeckApp

    app = LoopDeckApp(root, session, substrate=Substrate(root, session))
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
