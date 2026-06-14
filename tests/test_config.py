from __future__ import annotations

from loop_orchestrator.engine import config as config_mod
from loop_orchestrator.engine.config import EngineConfig, HarnessPolicy, load_config


def test_defaults_with_no_file(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg == EngineConfig()
    assert cfg.brain.harness == "claude"
    assert cfg.brain.timeout_s == 300
    assert cfg.approval_mode == "manual"
    assert cfg.checkpoint_interval_s == 900
    assert cfg.ingest.mode == "lane"
    assert cfg.ingest.lane == "docs"
    assert cfg.destructive.max_dispatches_per_cycle == 4
    assert cfg.destructive.payload_patterns == ["git push --force", "rm -rf", "reset --hard"]
    assert cfg.pm.adapters == []


def test_engine_section_parsed(tmp_path):
    (tmp_path / "lane-config.yaml").write_text(
        """
engine:
  approval_mode: auto
  brain:
    model: opus
    timeout_s: 60
  destructive:
    max_lanes: 5
  unknown_key: ignored
""",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.approval_mode == "auto"
    assert cfg.brain.model == "opus"
    assert cfg.brain.timeout_s == 60
    assert cfg.brain.harness == "claude"  # untouched default
    assert cfg.destructive.max_lanes == 5
    assert cfg.destructive.max_dispatches_per_cycle == 4  # untouched default
    assert cfg.poll_interval_s == 10


def test_host_override_replaces_top_level_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_short_hostname", lambda: "buildbox")
    (tmp_path / "lane-config.yaml").write_text(
        """
engine:
  approval_mode: auto
  brain:
    model: opus
    timeout_s: 60
""",
        encoding="utf-8",
    )
    (tmp_path / "lane-config.buildbox.yaml").write_text(
        """
engine:
  brain:
    harness: codex
""",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    # brain key replaced wholesale by the override: base's model/timeout gone.
    assert cfg.brain.harness == "codex"
    assert cfg.brain.model == ""
    assert cfg.brain.timeout_s == 300
    # keys the override didn't mention survive from the base.
    assert cfg.approval_mode == "auto"


def test_other_host_override_not_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "_short_hostname", lambda: "laptop")
    (tmp_path / "lane-config.buildbox.yaml").write_text(
        "engine:\n  approval_mode: full\n", encoding="utf-8"
    )
    assert load_config(tmp_path) == EngineConfig()


def test_lanes_key_ignored(tmp_path):
    (tmp_path / "lane-config.yaml").write_text(
        """
lanes:
  coord:
    harness: shell
  web:
    harness: claude
""",
        encoding="utf-8",
    )
    assert load_config(tmp_path) == EngineConfig()


def test_harness_policy_defaults_to_pass_through(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg.harness_policy == HarnessPolicy()
    assert cfg.harness_policy.allow == []
    assert cfg.harness_policy.deny == []
    assert cfg.harness_policy.cost_ceiling == ""
    assert cfg.harness_policy.autonomy_cap == ""
    assert cfg.harness_policy.role_tag_map == {}


def test_harness_policy_parsed(tmp_path):
    (tmp_path / "lane-config.yaml").write_text(
        """
engine:
  harness_policy:
    allow: [claude, pi, codex]
    deny: [amp]
    cost_ceiling: medium
    autonomy_cap: attended
    role_tag_map:
      infra: [ops, code]
      web: [product, synthesis]
""",
        encoding="utf-8",
    )
    policy = load_config(tmp_path).harness_policy
    assert policy.allow == ["claude", "pi", "codex"]
    assert policy.deny == ["amp"]
    assert policy.cost_ceiling == "medium"
    assert policy.autonomy_cap == "attended"
    assert policy.role_tag_map == {"infra": ["ops", "code"], "web": ["product", "synthesis"]}


def test_harness_policy_governance_fields_parsed(tmp_path):
    (tmp_path / "lane-config.yaml").write_text(
        """
engine:
  harness_policy:
    role_defaults:
      infra: claude
    high_risk_roles: [infra, ops]
""",
        encoding="utf-8",
    )
    policy = load_config(tmp_path).harness_policy
    assert policy.role_defaults == {"infra": "claude"}
    assert policy.high_risk_roles == ["infra", "ops"]
    # defaults: no role rewrites declared, infra is the high-risk role
    assert HarnessPolicy().role_defaults == {}
    assert HarnessPolicy().high_risk_roles == ["infra"]
    assert HarnessPolicy().brain_allow == []  # empty = any harness may be brain


def test_harness_policy_brain_allow_parsed(tmp_path):
    (tmp_path / "lane-config.yaml").write_text(
        """
engine:
  harness_policy:
    brain_allow: [claude, codex]
""",
        encoding="utf-8",
    )
    assert load_config(tmp_path).harness_policy.brain_allow == ["claude", "codex"]


def test_role_rules_default_empty_and_parsed(tmp_path):
    # T0020 per-role demand-provisioning rules.
    assert EngineConfig().harness_policy.role_rules == {}
    (tmp_path / "lane-config.yaml").write_text(
        "engine:\n"
        "  harness_policy:\n"
        "    role_rules:\n"
        "      routes-and-flows:\n"
        "        preferred_harness: claude\n"
        "        fallback: [codex]\n"
        "        concurrency_allowance: 2\n",
        encoding="utf-8",
    )
    rules = load_config(tmp_path).harness_policy.role_rules
    assert rules["routes-and-flows"]["preferred_harness"] == "claude"
    assert rules["routes-and-flows"]["fallback"] == ["codex"]
    assert rules["routes-and-flows"]["concurrency_allowance"] == 2


def test_checkpoint_keep_decisions_default_and_parsed(tmp_path):
    # default decision-log retention (T0022)
    assert EngineConfig().checkpoint.keep_decisions == 10
    (tmp_path / "lane-config.yaml").write_text(
        "engine:\n  checkpoint:\n    keep_decisions: 25\n", encoding="utf-8"
    )
    assert load_config(tmp_path).checkpoint.keep_decisions == 25


def test_harness_policy_partial_keeps_defaults(tmp_path):
    (tmp_path / "lane-config.yaml").write_text(
        """
engine:
  harness_policy:
    cost_ceiling: high
    unknown_policy_key: ignored
""",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.harness_policy.cost_ceiling == "high"
    assert cfg.harness_policy.allow == []
    assert cfg.harness_policy.role_tag_map == {}
    # the rest of the engine config is untouched by a policy-only file
    assert cfg.brain.harness == "claude"
    assert cfg.approval_mode == "manual"
