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

import dataclasses
import re
from typing import TYPE_CHECKING

from .config import HarnessPolicy
from .decision import Action, AddLaneAction

if TYPE_CHECKING:
    from .config import EngineConfig

SAFE = "safe"
DESTRUCTIVE = "destructive"
BLOCKED = "blocked"

_ADR_ACCEPT_RE = re.compile(r"loop-adr\s+accept")

# A roster snapshot is dict[harness_name -> roster entry] as emitted by
# `harness-registry roster --json` (resolved by the loop, never in here —
# the gate stays pure). roster=None or an empty HarnessPolicy means the
# harness pass is a no-op: today's behavior exactly.
Roster = dict[str, dict]

_EMPTY_POLICY = HarnessPolicy()
_COST_RANK = {"": 0, "none": 0, "low": 1, "medium": 2, "high": 3}
_AUTONOMY_RANK = {"": 0, "none": 0, "attended": 1, "unattended": 2}

# T0034 (B4): capability tags that mean "this harness consumes a prose brief as
# a PROMPT" — a genuine agent. A harness with NONE of these and no one-shot
# template does not act on a text brief as an agent: `shell` (probe,watch) runs
# it as a raw shell command (a real safety hazard), `mprocs` (dashboard) ignores
# it (a silent no-op). Either way a human must confirm, so a text brief to such
# a lane is destructive. Driving off the registry's capability_tags (not just
# the one-shot template) is what distinguishes the two and stops over-flagging an
# interactive agent that simply has no one-shot mode (e.g. pi).
_AGENT_CAPABILITY_TAGS = frozenset(
    {
        "brain",
        "code",
        "ingest",
        "ops",
        "research",
        "synthesis",
        "product",
        "experiment",
        "bulk",
        "fleet",
        "search",
    }
)


def _is_agent_harness(entry: dict) -> bool:
    """True when a harness consumes a text brief as a PROMPT (a genuine agent):
    it declares a one-shot template OR any agent capability tag. Generalizes the
    T0017 one-shot-only proxy so an interactive agent with no one-shot mode is not
    mis-flagged, while shell/dashboard harnesses (no template, no agent tag) read
    as non-agents."""
    if entry.get("oneshot_template", ""):
        return True
    tags = {tag.strip() for tag in str(entry.get("capability_tags", "")).split(",") if tag.strip()}
    return bool(tags & _AGENT_CAPABILITY_TAGS)


def _allowed_for_role(harness: str, role: str | None, policy: HarnessPolicy, entry: dict) -> bool:
    if policy.allow and harness not in policy.allow:
        return False
    if role and role in policy.role_tag_map:
        tags = set(str(entry.get("capability_tags", "")).split(","))
        if not tags & set(policy.role_tag_map[role]):
            return False
    return True


def classify_harness(
    action: Action, config: EngineConfig, roster: Roster | None = None
) -> str | None:
    """Harness-governance verdict for an add_lane, per plan A.2 — or None
    when the pass has no opinion (not an add_lane, no roster threaded, no
    harness on the action, or the policy is empty = pass-through)."""
    if roster is None or not isinstance(action, AddLaneAction) or not action.harness:
        return None
    policy = config.harness_policy
    if policy == _EMPTY_POLICY:
        return None
    harness = action.harness
    if harness in policy.deny:
        return BLOCKED  # mirrors the coord-target block
    entry = roster.get(harness)
    if entry is None:
        return BLOCKED  # unknown to roster: never reaches the bash boundary
    if not _allowed_for_role(harness, action.role, policy, entry):
        return BLOCKED  # not allowed and no rewrite applied upstream
    if entry.get("present") is False:
        return DESTRUCTIVE  # roster says missing: human decides
    if str(entry.get("health", "")) in ("missing", "unauthenticated", "unhealthy"):
        return DESTRUCTIVE
    if policy.cost_ceiling and _COST_RANK.get(str(entry.get("cost_tier", "")), 0) > _COST_RANK.get(
        policy.cost_ceiling, 3
    ):
        return DESTRUCTIVE
    if policy.autonomy_cap and _AUTONOMY_RANK.get(
        str(entry.get("autonomy_class", "")), 0
    ) > _AUTONOMY_RANK.get(policy.autonomy_cap, 2):
        return DESTRUCTIVE
    if (
        str(entry.get("drift_pins", "")) == "high"
        and action.auto_approve
        and action.role in policy.high_risk_roles
    ):
        return DESTRUCTIVE  # high drift + unattended + high-risk role
    return SAFE


def govern_add_lanes(
    actions: list[Action], config: EngineConfig, roster: Roster | None = None
) -> tuple[list[Action], list[dict]]:
    """Pure rewrite pass (plan A.2 row 3): an add_lane whose harness is not
    allowed for its role is rewritten to the policy's role default when that
    default is itself allowed. Returns (actions, governance event dicts);
    with roster=None or an empty policy this is the identity."""
    if roster is None:
        return actions, []
    policy = config.harness_policy
    if policy == _EMPTY_POLICY:
        return actions, []
    rewritten: list[Action] = []
    events: list[dict] = []
    for action in actions:
        if (
            isinstance(action, AddLaneAction)
            and action.harness
            and action.harness not in policy.deny
            and action.harness in roster
            and not _allowed_for_role(action.harness, action.role, policy, roster[action.harness])
        ):
            default = policy.role_defaults.get(action.role or "", "")
            entry = roster.get(default)
            if (
                default
                and default != action.harness
                and default not in policy.deny
                and entry is not None
                and _allowed_for_role(default, action.role, policy, entry)
            ):
                events.append(
                    {
                        "event": "harness-rewrite",
                        "window": action.window,
                        "role": action.role,
                        "from_harness": action.harness,
                        "to_harness": default,
                    }
                )
                action = dataclasses.replace(action, harness=default)
        rewritten.append(action)
    return rewritten, events


def _classify_dispatch_target(
    action: Action, roster: Roster | None, lane_harnesses: dict[str, str] | None
) -> str | None:
    """F1 (Phase 2): govern the TARGET of a dispatch/steer, not the add_lane
    harness choice. A `mode='text'` dispatch/steer is an agent BRIEF — it only
    does anything on a lane whose harness can act on prose. Returns:

      - BLOCKED      the target lane is unknown to the per-cycle lane snapshot,
                     or runs a harness unknown to the roster (cannot verify it
                     is an agent at all);
      - DESTRUCTIVE  the target runs a non-agent harness — `shell` (text is a
                     raw shell command), or any harness that is not a genuine
                     agent per the registry (`_is_agent_harness`: no one-shot
                     template AND no agent capability tag, e.g. mprocs). T0034
                     drives this off the harness's text-handling semantics, not
                     just the one-shot template, so a text brief that would run
                     as a shell command always needs a human;
      - None         no opinion — no lane snapshot threaded, not a text
                     dispatch/steer, or the target is a genuine agent lane.

    `mode='command'` is never F1's concern (running a shell command on a shell
    lane is exactly what command mode is for) — it is left to the existing shape
    rule. With `lane_harnesses=None` (every pre-F1 caller / empty policy) this is
    inert, so existing behavior is unchanged.
    """
    if lane_harnesses is None or action.kind not in ("dispatch", "steer"):
        return None
    if getattr(action, "mode", "text") != "text":
        return None
    harness = lane_harnesses.get(action.lane)
    if harness is None:
        return BLOCKED  # target lane unknown to the cycle's lane snapshot
    entry = roster.get(harness) if roster is not None else None
    if entry is None:
        return BLOCKED  # target runs a harness unknown to the roster
    if harness == "shell" or not _is_agent_harness(entry):
        return DESTRUCTIVE  # text-as-shell or non-agent (dashboard) target lane
    return None


def _classify_reuse_before_spawn(
    action: Action, config: EngineConfig, role_workers: dict[str, dict] | None
) -> str | None:
    """T0020 reuse-before-spawn HARD rule: an add_lane for a role that already
    has an idle worker lane is DESTRUCTIVE (prefer reuse over a duplicate),
    until the role's `concurrency_allowance` (default 1) permits more live
    workers. `role_workers` is the per-cycle {role: {"idle": n, "live": n}}
    snapshot the loop resolves (worker-kind lanes only). None / empty policy /
    no role / no workers for the role = no opinion (today's behavior)."""
    if role_workers is None or not isinstance(action, AddLaneAction):
        return None
    policy = config.harness_policy
    if policy == _EMPTY_POLICY:
        return None
    role = action.role
    if not role:
        return None
    counts = role_workers.get(role)
    if not counts:
        return None
    allowance = policy.role_rules.get(role, {}).get("concurrency_allowance", 1)
    if counts.get("idle", 0) >= 1 and counts.get("live", 0) >= allowance:
        return DESTRUCTIVE
    return None


def classify(
    action: Action,
    live_lane_count: int,
    config: EngineConfig,
    roster: Roster | None = None,
    lane_harnesses: dict[str, str] | None = None,
    lane_kinds: dict[str, str] | None = None,
    role_workers: dict[str, dict] | None = None,
) -> str:
    """Classify one action. blocked > destructive > safe.

    With a roster threaded in, the harness-governance pass (classify_harness)
    runs ABOVE the shape rules and merges by severity; with a lane snapshot
    threaded in, the F1 dispatch-target pass (_classify_dispatch_target) and the
    T0019 standing-lane drop guard do too. With roster=None, lane_harnesses=None,
    and lane_kinds=None (the defaults, and every pre-governance caller) behavior
    is unchanged.
    """
    harness_verdict = classify_harness(action, config, roster)
    if harness_verdict == BLOCKED:
        return BLOCKED
    target_verdict = _classify_dispatch_target(action, roster, lane_harnesses)
    if target_verdict == BLOCKED:
        return BLOCKED
    reuse_verdict = _classify_reuse_before_spawn(action, config, role_workers)
    target = getattr(action, "lane", None) or getattr(action, "window", None)
    text = getattr(action, "payload", None) or getattr(action, "brief", None)
    if target == "coord":
        return BLOCKED
    if text is not None and _ADR_ACCEPT_RE.search(text):
        return BLOCKED
    if action.kind == "verify":
        # loop-verify is a read-only review; any merge it justifies remains a
        # separate human-gated escalate action.
        return SAFE
    if action.kind == "build":
        # codex exec builds are isolated to the lane worktree branch; merge,
        # push, and reinstall remain outside this action's authority.
        return SAFE
    if action.kind == "drop_lane":
        # T0019: a declared 'standing' lane is never auto-dropped — the engine
        # never passes --force, and dropping coord/ops/docs or a long-lived
        # writer must be a human action. With no lane snapshot threaded
        # (lane_kinds=None) this falls back to today's DESTRUCTIVE.
        if lane_kinds is not None and lane_kinds.get(getattr(action, "window", None)) == "standing":
            return BLOCKED
        return DESTRUCTIVE
    if action.kind == "escalate":
        # A help-request is not dangerous, but it must surface to an operator
        # instead of being consumed by auto mode.
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
    if reuse_verdict == DESTRUCTIVE:
        return DESTRUCTIVE
    if target_verdict == DESTRUCTIVE:
        return DESTRUCTIVE
    if harness_verdict == DESTRUCTIVE:
        return DESTRUCTIVE
    return SAFE


def classify_batch(
    actions: list[Action],
    live_lane_count: int,
    config: EngineConfig,
    roster: Roster | None = None,
    lane_harnesses: dict[str, str] | None = None,
    lane_kinds: dict[str, str] | None = None,
    role_workers: dict[str, dict] | None = None,
) -> list[str]:
    """Per-action classify, then the fan-out guard: when the batch carries more
    dispatch+steer than max_dispatches_per_cycle, every 'safe' dispatch/steer
    in it is upgraded to 'destructive' (the whole burst needs approval)."""
    results = [
        classify(action, live_lane_count, config, roster, lane_harnesses, lane_kinds, role_workers)
        for action in actions
    ]
    fan_out = sum(1 for action in actions if action.kind in ("dispatch", "steer"))
    if fan_out > config.destructive.max_dispatches_per_cycle:
        results = [
            DESTRUCTIVE if result == SAFE and action.kind in ("dispatch", "steer") else result
            for action, result in zip(actions, results, strict=True)
        ]
    return results


# Default sustained code-writer count at which a dedicated integration lane
# (sole writer of main) is warranted (plan C.3; T0026).
INTEGRATION_LANE_THRESHOLD = 3


def should_provision_worktree(existing_code_writers: int, dirty_peers: bool = False) -> bool:
    """T0026 conditional provisioning rule (plan C.4) — DORMANT at concurrency=1.

    A new code-writer lane gets an isolated git worktree only once parallelism is
    REAL: another code-writer lane is already live (so the new one would be
    concurrent), or a peer already holds dirty state. When the new lane would be
    the sole serialized writer (no other code-writer, no dirty peer) it stays
    SHARED — byte-identical to pre-Phase-4. The asymmetry justifies the default:
    isolation is ~200ms + a venv (bounded, front-loaded) while a shared tree's
    cross-lane commit-sweep is unbounded and invisible until review, so the
    default flips from 'shared unless asked' to 'shared only while serialized'."""
    return existing_code_writers >= 1 or bool(dirty_peers)


def needs_integration_lane(code_writers: int, threshold: int = INTEGRATION_LANE_THRESHOLD) -> bool:
    """T0026: at N>=threshold sustained concurrent code-writers, a dedicated
    integration lane (its own worktree, the SOLE writer of main) contains the
    O(N^2) reconciliation cost in one partition owner. Pure predicate — the
    engine surfaces it; lane creation stays brain/operator-driven."""
    return code_writers >= threshold
