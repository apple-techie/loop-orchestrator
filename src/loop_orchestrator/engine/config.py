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


@dataclass(frozen=True)
class IngestConfig:
    mode: str = "lane"  # lane | headless
    lane: str = "docs"
    timeout_s: int = 600
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
class EngineConfig:
    brain: BrainConfig = field(default_factory=BrainConfig)
    approval_mode: str = "manual"  # manual | auto | full
    checkpoint_interval_s: int = 900
    poll_interval_s: int = 10
    min_cycle_interval_s: int = 120
    ingest: IngestConfig = field(default_factory=IngestConfig)
    destructive: DestructiveConfig = field(default_factory=DestructiveConfig)
    pm: PmConfig = field(default_factory=PmConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    lint: LintConfig = field(default_factory=LintConfig)


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
