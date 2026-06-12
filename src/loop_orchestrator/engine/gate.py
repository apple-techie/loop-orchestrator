"""GATE — classify decision actions as safe | destructive | blocked.

Pure module: no substrate, no IO. Classification is defense in depth on top
of decision validation: 'coord' targeting and ADR acceptance are blocked here
even though validate() already rejects them. ADR acceptance ('loop-adr
accept' in any payload/brief) is human-only and is never automated.

Classification is on ACTION SHAPE, not on a text blocklist of the payload: a
regex blocklist over free text can never enumerate every shell-injection
vector, so any action that executes a raw command is destructive by shape and
needs a human. The payload-pattern regex is kept ONLY as an *additional*
destructive trigger for text-mode dispatch/steer (a cheap catch for obviously
dangerous instructions), never as the primary safety boundary.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .decision import Action

if TYPE_CHECKING:
    from .config import EngineConfig

SAFE = "safe"
DESTRUCTIVE = "destructive"
BLOCKED = "blocked"

_ADR_ACCEPT_RE = re.compile(r"loop-adr\s+accept")


def classify(action: Action, live_lane_count: int, config: EngineConfig) -> str:
    """Classify one action. blocked > destructive > safe."""
    target = getattr(action, "lane", None) or getattr(action, "window", None)
    text = getattr(action, "payload", None) or getattr(action, "brief", None)
    if target == "coord":
        return BLOCKED
    if text is not None and _ADR_ACCEPT_RE.search(text):
        return BLOCKED
    if action.kind == "drop_lane":
        return DESTRUCTIVE
    if action.kind == "steer" and action.interrupt:
        return DESTRUCTIVE
    # SHAPE rule (HIGH-1): a dispatch/steer that injects a raw command into the
    # lane is destructive regardless of its text — it bypasses the agent and
    # runs a shell. The gate does not carry the per-lane harness, so it cannot
    # tell whether 'command' mode is even meaningful for the target lane; the
    # conservative, harness-agnostic rule is therefore mode-based: text mode
    # stays safe, command mode is always destructive (human-approved). This is
    # a deliberate over-approximation, not a harness-aware decision.
    if action.kind in ("dispatch", "steer") and getattr(action, "mode", "text") == "command":
        return DESTRUCTIVE
    # SHAPE rule (HIGH-2a): an add_lane that supplies a raw 'cmd' spawns an
    # arbitrary process; only add_lane via a registry-validated 'harness' (no
    # cmd) stays safe (still subject to max_lanes below).
    if action.kind == "add_lane" and getattr(action, "cmd", None) is not None:
        return DESTRUCTIVE
    # Text-mode payload blocklist: an ADDITIONAL destructive trigger, never the
    # primary boundary (see module docstring).
    if action.kind in ("dispatch", "steer") and any(
        re.search(pattern, action.payload) for pattern in config.destructive.payload_patterns
    ):
        return DESTRUCTIVE
    if action.kind == "add_lane" and live_lane_count >= config.destructive.max_lanes:
        return DESTRUCTIVE
    return SAFE


def classify_batch(actions: list[Action], live_lane_count: int, config: EngineConfig) -> list[str]:
    """Per-action classify, then the fan-out guard: when the batch carries more
    dispatch+steer than max_dispatches_per_cycle, every 'safe' dispatch/steer
    in it is upgraded to 'destructive' (the whole burst needs approval)."""
    results = [classify(action, live_lane_count, config) for action in actions]
    fan_out = sum(1 for action in actions if action.kind in ("dispatch", "steer"))
    if fan_out > config.destructive.max_dispatches_per_cycle:
        results = [
            DESTRUCTIVE if result == SAFE and action.kind in ("dispatch", "steer") else result
            for action, result in zip(actions, results, strict=True)
        ]
    return results
