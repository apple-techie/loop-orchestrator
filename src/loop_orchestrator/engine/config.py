"""Engine configuration from the `engine:` top-level key of lane-config.yaml.

The bash substrate's lane-config-resolver reads ONLY the top-level `lanes:`
key (CONTRACT.md), so the `engine:` section lives in the same file but is
invisible to bash. A `lane-config.<short-hostname>.yaml` beside the base file
is merged on top: top-level keys of its `engine:` section REPLACE the base's
(no deep merge). Absent file or absent `engine:` key => all defaults.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import get_type_hints

import yaml


@dataclass(frozen=True)
class BrainConfig:
    harness: str = "claude"
    model: str = ""
    timeout_s: int = 300
    max_retries: int = 1
    max_calls_per_hour: int = 12
    extra_args: list[str] = field(default_factory=list)
    stream: bool = False  # claude stream-json into the live response transcript
    # When a brain failure is classified failure_kind=="quota", the watch loop
    # suppresses brain calls (only the brain — observation/PM continue) until a
    # reset deadline. If the stderr carries no parseable "resets <time>", back
    # off this many minutes instead of burning retries against the wall.
    quota_backoff_minutes: int = 60


@dataclass(frozen=True)
class IngestConfig:
    mode: str = "lane"  # lane | headless
    lane: str = "docs"
    # F17: the headless-ingest one-shot timeout, SEPARATE from and materially
    # lower than the brain/coord BrainConfig.timeout_s (300). A hung ingest
    # (observed: claude -p producing 0 bytes and stalling to the wall) must
    # degrade fast — not block the decision cycle for ~10 min — so the cycle
    # still reaches the brain and the offending message gets quarantined.
    timeout_s: int = 120
    harness: str = ""  # headless one-shot harness ("" = use brain.harness)
    auto_approve: bool = True  # append the registry auto-approve flag (file writes needed)


@dataclass(frozen=True)
class DestructiveConfig:
    max_dispatches_per_cycle: int = 4
    max_lanes: int = 12
    payload_patterns: list[str] = field(
        default_factory=lambda: ["git push --force", "rm -rf", "reset --hard"]
    )


@dataclass(frozen=True)
class PmConfig:
    adapters: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class MetricsConfig:
    log_after_cycle: bool = False  # loop-metrics --log after each completed cycle


@dataclass(frozen=True)
class LintConfig:
    enabled: bool = False  # dispatch loop-wiki-lint when the last lint run is stale
    interval_h: int = 24


@dataclass(frozen=True)
class CheckpointConfig:
    # Decision-log retention (T0022): file_decision keeps the last N decision
    # entries below the coord-decisions marker and rotates the overflow into
    # ops-wiki/decisions-archive.md, so the boot checkpoint stays bounded.
    keep_decisions: int = 10


@dataclass(frozen=True)
class HarnessPolicy:
    """Harness governance policy (harness-governance plan A.1).

    Facts live in lib/harness-registry.sh (capability_tags, cost_tier,
    autonomy_class, ...); this is the policy layer the gate enforces.
    The empty policy is a strict pass-through — today's behavior.
    """

    allow: list[str] = field(default_factory=list)  # empty = every harness allowed
    deny: list[str] = field(default_factory=list)  # deny wins over allow
    cost_ceiling: str = ""  # max registry cost_tier (low|medium|high); "" = no ceiling
    autonomy_cap: str = ""  # max registry autonomy_class (none|attended|unattended); "" = no cap
    role_tag_map: dict[str, list[str]] = field(default_factory=dict)  # role -> capability tags
    role_defaults: dict[str, str] = field(default_factory=dict)  # role -> rewrite-to harness
    # Roles where a high-drift harness running unattended is forced through
    # human approval (plan A.2). Only consulted once a policy is written —
    # the empty policy never reaches the gate's harness pass.
    high_risk_roles: list[str] = field(default_factory=lambda: ["infra"])
    # Harnesses allowed as the brain / headless-ingest one-shot; empty = any.
    brain_allow: list[str] = field(default_factory=list)
    # Per-role demand-provisioning rules (T0020), keyed by role. Each value is a
    # plain mapping (forward-compatible; read with .get + defaults) with keys:
    #   preferred_harness: str          — harness to provision for this role
    #   fallback: list[str]             — ordered fallbacks if preferred is down
    #   spawn_when: str                 — declared condition (brain guidance):
    #                                     "unclaimed_brief_and_no_idle_worker"
    #   retire_after_idle_cycles: int   — declare a retire-candidate after N idle
    #                                     cycles (T0023 acts; T0020 only declares)
    #   concurrency_allowance: int      — max concurrent workers of this role
    #                                     before the gate forces reuse (default 1)
    # Only the reuse-before-spawn HARD rule (concurrency_allowance) is gate-
    # enforced here; the rest are declared facts the brain/T0023 consult.
    role_rules: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineConfig:
    brain: BrainConfig = field(default_factory=BrainConfig)
    approval_mode: str = "manual"  # manual | auto | full
    checkpoint_interval_s: int = 900
    poll_interval_s: int = 10
    min_cycle_interval_s: int = 120
    # >0 turns on the lane-utilization drive: the brain prompt surfaces idle lanes
    # with open backlog + a rubric to route work there before `stop`. 0.0 = off
    # (byte-identical prompt). Raise destructive.max_dispatches_per_cycle in lockstep
    # when enabling, else fanning to many idle lanes trips the fan-out gate.
    target_lane_utilization: float = 0.0
    max_fix_rounds: int = 2
    ingest: IngestConfig = field(default_factory=IngestConfig)
    destructive: DestructiveConfig = field(default_factory=DestructiveConfig)
    pm: PmConfig = field(default_factory=PmConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    lint: LintConfig = field(default_factory=LintConfig)
    harness_policy: HarnessPolicy = field(default_factory=HarnessPolicy)
    checkpoint: CheckpointConfig = field(default_factory=CheckpointConfig)


def _merge(cls: type, data: object):
    """Build `cls` from defaults, overriding fields present in `data`.

    Unknown keys are ignored (forward compatibility); non-mapping values for
    nested sections fall back to that section's defaults.
    """
    if not isinstance(data, dict):
        data = {}
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        hint = hints.get(f.name)
        if isinstance(hint, type) and is_dataclass(hint):
            kwargs[f.name] = _merge(hint, data[f.name])
        else:
            kwargs[f.name] = data[f.name]
    return cls(**kwargs)


def _engine_section(path: Path) -> dict:
    """The `engine:` mapping of a lane-config YAML; {} for any absence."""
    if not path.is_file():
        return {}
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return {}
    if not isinstance(doc, dict):
        return {}
    section = doc.get("engine")
    return section if isinstance(section, dict) else {}


def _short_hostname() -> str:
    return socket.gethostname().split(".")[0]


def load_config(project_root: str | Path) -> EngineConfig:
    """Resolve EngineConfig for a project root, applying the host override."""
    root = Path(project_root)
    base = _engine_section(root / "lane-config.yaml")
    override = _engine_section(root / f"lane-config.{_short_hostname()}.yaml")
    return _merge(EngineConfig, {**base, **override})


def lane_config_harnesses(project_root: str | Path) -> dict[str, str]:
    """Per-lane harness declared in lane-config.yaml's `lanes:` section (with the
    host override applied, lane-level), as {lane: harness} for lanes that name
    one. F6 (T0027): the AUTHORITATIVE per-lane harness the gate falls back to
    when a lane has no `@loop_lane_harness` tmux tag (pre-existing sessions) or
    when a multi-pane window cannot carry a per-lane tag. Returns {} when there
    is no lane-config — so tag-only resolution (today) is unchanged/dormant."""
    root = Path(project_root)
    result: dict[str, str] = {}
    for path in (root / "lane-config.yaml", root / f"lane-config.{_short_hostname()}.yaml"):
        if not path.is_file():
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        lanes = doc.get("lanes") if isinstance(doc, dict) else None
        if not isinstance(lanes, dict):
            continue
        for lane, block in lanes.items():
            harness = block.get("harness") if isinstance(block, dict) else None
            if isinstance(harness, str) and harness:
                result[lane] = harness  # host override replaces the primary
    return result
