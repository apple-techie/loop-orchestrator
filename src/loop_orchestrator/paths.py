"""Resolution of repo + engine state paths for one (project_root, session)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def normalize_project_root(project_root: str | Path) -> Path:
    return Path(os.path.abspath(project_root))


@dataclass(frozen=True)
class SessionPaths:
    """Everything path-shaped the engine/deck need, derived once.

    The engine's own state lives under `.loop/sessions/<session>/engine/` —
    the per-session state dir the substrate already uses for
    lane-restarts.jsonl, and a namespace CONTRACT.md reserves for this layer.
    """

    project_root: Path
    session: str
    engine_dir: Path = field(init=False)

    def __post_init__(self):
        root = normalize_project_root(self.project_root)
        object.__setattr__(self, "project_root", root)
        object.__setattr__(
            self,
            "engine_dir",
            root / ".loop" / "sessions" / self.session / "engine",
        )

    @property
    def state_file(self) -> Path:
        return self.project_root / ".loop" / "orchestrator-state.json"

    @property
    def mailbox_dir(self) -> Path:
        return self.project_root / ".loop" / "messages"

    @property
    def processed_dir(self) -> Path:
        return self.mailbox_dir / "processed"

    @property
    def lane_restarts(self) -> Path:
        return self.project_root / ".loop" / "sessions" / self.session / "lane-restarts.jsonl"

    @property
    def ops_wiki(self) -> Path:
        return self.project_root / "ops-wiki"

    @property
    def checkpoint_page(self) -> Path:
        return self.ops_wiki / "checkpoint.md"

    def lane_page(self, window: str) -> Path:
        return self.ops_wiki / "lanes" / f"{window}.md"

    @property
    def tasks_dir(self) -> Path:
        return self.project_root / "tasks"

    # Engine-owned state (CONTRACT.md reserves .loop/sessions/<s>/engine/).
    @property
    def events_path(self) -> Path:
        return self.engine_dir / "events.jsonl"

    @property
    def snapshot_path(self) -> Path:
        return self.engine_dir / "snapshot.json"

    @property
    def pending_decision_path(self) -> Path:
        return self.engine_dir / "pending-decision.json"

    @property
    def decisions_dir(self) -> Path:
        return self.engine_dir / "decisions"

    @property
    def brain_dir(self) -> Path:
        return self.engine_dir / "brain"

    @property
    def proposals_dir(self) -> Path:
        return self.engine_dir / "proposals"

    @property
    def verify_dir(self) -> Path:
        return self.project_root / ".loop" / "sessions" / self.session / "verify"

    def verify_result_path(self, window: str) -> Path:
        return self.verify_dir / f"{window}.json"

    def verify_run_result_path(self, window: str, run_token: str) -> Path:
        return self.verify_dir / f"{window}-{run_token}.json"

    @property
    def verify_markers_path(self) -> Path:
        return self.engine_dir / "verify-in-progress.json"

    @property
    def build_markers_path(self) -> Path:
        return self.engine_dir / "build-in-progress.json"

    @property
    def pid_path(self) -> Path:
        return self.engine_dir / "engine.pid"

    @property
    def paused_path(self) -> Path:
        return self.engine_dir / "paused"

    @property
    def cycle_now_path(self) -> Path:
        return self.engine_dir / "cycle-now"

    @property
    def lock_path(self) -> Path:
        return self.engine_dir / ".lock"

    # Deck-OWNED diagnostic log. The deck stays a strict NON-WRITER of engine
    # STATE (decisions / snapshot / wiki) — appending crash tracebacks to this
    # plain log does NOT violate that invariant: it is a diagnostic sink, never
    # authoritative state the engine reads back as a decision input.
    @property
    def deck_crash_log(self) -> Path:
        return self.engine_dir / "deck-crash.log"

    # Stale-daemon guard: the watch daemon records the build mtime of its loaded
    # gate module here at boot; `status` compares it to the on-disk module.
    @property
    def daemon_build_path(self) -> Path:
        return self.engine_dir / "daemon-build.json"

    def ensure(self) -> None:
        self.engine_dir.mkdir(parents=True, exist_ok=True)
        self.decisions_dir.mkdir(exist_ok=True)
        self.brain_dir.mkdir(exist_ok=True)
