"""loop-lane-status.sh readiness classifier — heuristics + declared markers (T0015).

Exercises the pure rule chain through the `--classify-stdin` seam (no tmux),
pinning BOTH the FROZEN single-word contract on the heuristic path (harness="")
and the new harness-aware marker preference — and proving the marker path never
regresses any known case.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "loop-lane-status.sh"


def classify(tail: str, harness: str = "") -> str:
    args = ["bash", str(SCRIPT), "--classify-stdin"]
    if harness:
        args.append(harness)
    proc = subprocess.run(args, input=tail, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# Realistic pane captures (the chrome each harness renders).
CLAUDE_WORKING = "user msg\n\n✻ Crunching… (12s · esc to interrupt)\n"
CLAUDE_IDLE = "result here\n\n│ >  │\n  ? for shortcuts · accept edits on\n"
# Codex renders 'esc to interrupt' ABOVE its persistent composer/footer, so the
# live marker is out of the bottom slice — matched across the full tail.
CODEX_WORKING = "• Working (5s • esc to interrupt)\nl2\nl3\nl4\nl5\nmodel: x  cwd: /repo\n"
# Pi renders a braille spinner just above its multi-line footer (out of bottom 5).
PI_WORKING = "⠙ Working…\nfooter\nactive 1\nl4\nl5\nl6\n"
SHELL_IDLE = "$ make check\nok\n➜  repo git:(main) ✗\n"
BARE_PROMPT = "ran something\nfinished\n$ \n"
AWAITING = "Edit file foo?\nDo you want to proceed?\n❯ 1. Yes\n"
ERRORED = "running\nFATAL: kaboom\n"

# (tail, declared harness, expected word). Markers for these harnesses are
# lifted from the heuristics, so the marker path must agree with the heuristic.
KNOWN_CASES = [
    (CLAUDE_WORKING, "claude", "working"),
    (CLAUDE_IDLE, "claude", "idle"),
    (CODEX_WORKING, "codex", "working"),
    (PI_WORKING, "pi", "working"),
    (SHELL_IDLE, "shell", "idle"),
    (BARE_PROMPT, "shell", "idle"),
    (AWAITING, "claude", "awaiting-approval"),
    (ERRORED, "claude", "errored"),
]


def test_known_cases_classify_correctly():
    for tail, harness, expected in KNOWN_CASES:
        assert classify(tail, harness) == expected, (harness, repr(tail))


def test_marker_path_never_regresses_known_cases():
    # FROZEN: the declared-marker path (harness set) classifies every known
    # pane identically to the heuristic path (harness=""). This is the hardest
    # T0015 constraint — every existing special case stays identical.
    for tail, harness, expected in KNOWN_CASES:
        assert classify(tail, "") == expected, ("heuristic", repr(tail))
        assert classify(tail, harness) == classify(tail, ""), (harness, repr(tail))


def test_declared_working_marker_is_preferred_over_verb_heuristic():
    # A claude pane showing only a bottom-slice verb spinner with NO
    # 'esc to interrupt': the heuristic trusts the verb (working); the declared
    # marker path prefers claude's vetted 'esc to interrupt' signal, so it does
    # NOT read the bare verb as working. Proves the marker is consulted.
    verb_only = "Running...\n"
    assert classify(verb_only, "") == "working"
    assert classify(verb_only, "claude") == "unknown"


def test_unknown_harness_falls_back_to_heuristic():
    # An @loop_lane_harness value not in the registry => no markers => heuristic.
    assert classify(CLAUDE_WORKING, "nosuchharness") == "working"
    assert classify("Running...\n", "nosuchharness") == "working"


def test_empty_pane_is_unknown():
    assert classify("", "claude") == "unknown"
    assert classify("", "") == "unknown"
