"""One-shot brain invocation for the engine cycle.

The ONLY module besides substrate.py allowed to import subprocess (CI
enforces). Never shell=True: the prompt travels as a single argv element.
Budget (max_calls_per_hour) is checked against events.jsonl BEFORE spawning;
every invocation leaves a prompt/response transcript pair under
paths.brain_dir and exactly one brain-call event carrying both paths.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..paths import SessionPaths
from ..substrate import Substrate
from .config import EngineConfig
from .events import EventLog

BUDGET_WINDOW_S = 3600


class BrainError(RuntimeError):
    """Base for brain invocation failures."""


class BrainBudgetError(BrainError):
    """max_calls_per_hour reached; no process was spawned."""


class BrainInvocationError(BrainError):
    """Every allowed attempt exited non-zero, timed out, or failed to spawn."""


class Brain:
    def __init__(
        self,
        config: EngineConfig,
        substrate: Substrate,
        paths: SessionPaths,
        events: EventLog,
    ):
        self.config = config
        self.substrate = substrate
        self.paths = paths
        self.events = events

    def _argv(self, prompt: str) -> list[str]:
        """LOOP_ENGINE_BRAIN_CMD overrides the registry's one-shot template."""
        override = os.environ.get("LOOP_ENGINE_BRAIN_CMD")
        if override:
            return shlex.split(override) + [prompt]
        tokens = shlex.split(self.substrate.oneshot_template(self.config.brain.harness))
        if "{prompt}" in tokens:
            tokens = [prompt if tok == "{prompt}" else tok for tok in tokens]
        else:
            tokens.append(prompt)
        return tokens + list(self.config.brain.extra_args)

    def _transcript_paths(self) -> tuple[Path, Path]:
        """Unique <ts>.prompt.md / <ts>.response.md pair under brain_dir."""
        self.paths.brain_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = stamp
        n = 1
        while (self.paths.brain_dir / f"{name}.prompt.md").exists():
            name = f"{stamp}-{n}"
            n += 1
        return (
            self.paths.brain_dir / f"{name}.prompt.md",
            self.paths.brain_dir / f"{name}.response.md",
        )

    def invoke(self, prompt: str) -> str:
        cfg = self.config.brain
        if self.events.count_since("brain-call", BUDGET_WINDOW_S) >= cfg.max_calls_per_hour:
            raise BrainBudgetError(
                f"brain budget exhausted: {cfg.max_calls_per_hour} calls in the last hour"
            )
        argv = self._argv(prompt)
        prompt_path, response_path = self._transcript_paths()
        prompt_path.write_text(prompt, encoding="utf-8")
        self.events.append(
            "brain-call",
            harness=cfg.harness,
            prompt_path=str(prompt_path),
            response_path=str(response_path),
        )
        attempts = cfg.max_retries + 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=cfg.timeout_s,
                    cwd=self.paths.project_root,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                last_error = f"timed out after {cfg.timeout_s}s"
                self.events.append("brain-timeout", attempt=attempt, timeout_s=cfg.timeout_s)
            except OSError as exc:
                last_error = f"spawn failed: {exc}"
            else:
                if proc.returncode == 0:
                    response_path.write_text(proc.stdout, encoding="utf-8")
                    return proc.stdout
                last_error = f"exit {proc.returncode}: {proc.stderr.strip()[:500]}"
            if attempt < attempts:
                self.events.append("brain-retry", attempt=attempt, error=last_error)
        self.events.append("brain-failed", error=last_error)
        raise BrainInvocationError(f"brain failed after {attempts} attempt(s): {last_error}")
