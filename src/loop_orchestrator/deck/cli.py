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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        try:
            import textual  # noqa: F401
        except ImportError:
            print("loop-deck --check: textual is not installed", file=sys.stderr)
            return 1
        root = Path(args.project_root).resolve()
        print(f"loop-deck --check: ok (textual importable, project={root})")
        return 0
    session = args.session or os.environ.get("LOOP_SESSION")
    if not session:
        print("error: --session <name> (or $LOOP_SESSION) is required", file=sys.stderr)
        return 2
    root = Path(args.project_root).resolve()

    from ..substrate import Substrate
    from .app import LoopDeckApp

    app = LoopDeckApp(root, session, substrate=Substrate(root, session))
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
