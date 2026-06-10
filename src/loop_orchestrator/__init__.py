"""Optional Python layer over the loop-orchestrator bash substrate.

The bash scripts remain the substrate and the project's identity; this package
adds the deterministic engine (loop-engine), the Textual flight deck
(loop-deck), and the PM adapter framework (loop-pm). Every interaction with
tmux or the repo's state files goes through the surfaces enumerated in
CONTRACT.md — `substrate.py` is the only module allowed to spawn the bash
CLIs, and `brain.py` the only other module allowed to spawn subprocesses.
"""

__version__ = "0.1.0"
