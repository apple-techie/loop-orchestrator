from __future__ import annotations

from loop_orchestrator.engine import config as config_mod
from loop_orchestrator.engine.config import EngineConfig, load_config


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
