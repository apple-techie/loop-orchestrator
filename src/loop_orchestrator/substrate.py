"""The ONLY module that spawns the bash substrate (see CONTRACT.md).

Every engine/deck interaction with tmux, lanes, the digest, the wiki helpers,
or the harness registry goes through one of these typed wrappers. That gives
three properties the whole layer depends on:

- the Python layer can be tested without tmux by pointing
  ``LOOP_SUBSTRATE_BIN`` at a directory of fake executables;
- the bash surfaces we depend on are enumerable (they are exactly the methods
  of :class:`Substrate`), so CONTRACT.md stays honest;
- nobody re-derives tmux targets or paste timing in Python.

Repo rule (enforced in CI): ``subprocess`` is imported only here and in
``engine/brain.py`` (which spawns the one-shot LLM brain, not the substrate).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .contract import check_contract

# Canonical primitive name -> repo-relative fallback script.
_REPO_RELATIVE = {
    "loop-tmux": "loop-tmux.sh",
    "loop-dispatch": "loop-dispatch.sh",
    "loop-lane-status": "loop-lane-status.sh",
    "loop-digest": "loop-digest.sh",
    "loop-adr": "loop-adr.sh",
    "loop-checkpoint": "scripts/loop-checkpoint.sh",
    "loop-wiki-pending": "scripts/loop-wiki-pending.sh",
    "loop-task-lint": "scripts/loop-task-lint.sh",
    "loop-jira-sync": "scripts/loop-jira-sync.sh",
    "loop-metrics": "scripts/loop-metrics.sh",
    "loop-wiki-lint": "scripts/loop-wiki-lint.sh",
    "harness-registry": "lib/harness-registry.sh",
}


class SubstrateError(RuntimeError):
    """A bash primitive failed or could not be found."""

    def __init__(self, argv: list[str], returncode: int | None, stderr: str):
        pretty = " ".join(argv)
        super().__init__(f"substrate call failed ({returncode}): {pretty}\n{stderr.strip()}")
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class LaneStatus:
    lane: str
    status: str  # working | awaiting-approval | idle | errored | unknown
    target: str
    kind: str  # fixed | dynamic


@dataclass(frozen=True)
class LaneInfo:
    window: str
    harness: str | None
    model: str | None
    role: str | None
    cmd: str | None
    base: bool


class Substrate:
    """Typed wrappers, 1:1 with the CONTRACT.md CLI surfaces."""

    def __init__(self, project_root: str | Path, session: str):
        self.project_root = Path(project_root)
        self.session = session
        self._registry_cache: dict[tuple[str, str], str] = {}

    # ── resolution + execution ────────────────────────────────────────────

    def _resolve(self, name: str) -> list[str]:
        """LOOP_SUBSTRATE_BIN dir -> PATH -> repo-relative script."""
        bin_dir = os.environ.get("LOOP_SUBSTRATE_BIN")
        if bin_dir:
            cand = Path(bin_dir) / name
            if cand.is_file() and os.access(cand, os.X_OK):
                return [str(cand)]
        hit = shutil.which(name)
        if hit:
            return [hit]
        rel = self.project_root / _REPO_RELATIVE[name]
        if rel.is_file():
            return ["bash", str(rel)]
        raise SubstrateError(
            [name], None, f"primitive '{name}' not found via LOOP_SUBSTRATE_BIN, PATH, or {rel}"
        )

    def _run(
        self,
        name: str,
        *args: str,
        timeout: float = 30,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        argv = self._resolve(name) + list(args)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.project_root,
            )
        except subprocess.TimeoutExpired as exc:
            raise SubstrateError(argv, None, f"timed out after {timeout}s") from exc
        if check and proc.returncode != 0:
            raise SubstrateError(argv, proc.returncode, proc.stderr)
        return proc

    def _run_json(self, name: str, *args: str, timeout: float = 30) -> dict:
        proc = self._run(name, *args, timeout=timeout)
        import json

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise SubstrateError(
                self._resolve(name) + list(args), proc.returncode, f"bad JSON: {exc}"
            ) from exc
        return check_contract(payload, name)

    # ── lanes ─────────────────────────────────────────────────────────────

    def lanes(self) -> list[LaneInfo]:
        doc = self._run_json("loop-tmux", "list-lanes", "--session", self.session, "--json")
        return [
            LaneInfo(
                window=lane["window"],
                harness=lane.get("harness"),
                model=lane.get("model"),
                role=lane.get("role"),
                cmd=lane.get("cmd"),
                base=bool(lane.get("base")),
            )
            for lane in doc.get("lanes", [])
        ]

    def lane_status_all(self) -> dict[str, LaneStatus]:
        doc = self._run_json("loop-lane-status", "--json", "--all", self.session)
        return {
            lane: LaneStatus(
                lane=lane,
                status=info["status"],
                target=info["target"],
                kind=info["kind"],
            )
            for lane, info in doc.get("lanes", {}).items()
        }

    def lane_status(self, lane: str) -> str:
        return self._run("loop-lane-status", self.session, lane, timeout=15).stdout.strip()

    def print_target(self, lane: str) -> str:
        return self._run(
            "loop-lane-status", "--print-target", self.session, lane, timeout=15
        ).stdout.strip()

    def add_lane(
        self,
        window: str,
        harness: str | None = None,
        cmd: str | None = None,
        model: str | None = None,
        role: str | None = None,
        auto_approve: bool = False,
        wait_ready: bool = True,
        timeout: float = 120,
    ) -> None:
        args = ["add-lane", "--session", self.session, "--window", window]
        if harness:
            args += ["--harness", harness]
        if cmd:
            args += ["--cmd", cmd]
        if model:
            args += ["--model", model]
        if role:
            args += ["--role", role]
        if auto_approve:
            args += ["--auto-approve"]
        if wait_ready:
            args += ["--wait-ready"]
        self._run("loop-tmux", *args, timeout=timeout)

    def drop_lane(self, window: str) -> None:
        # NEVER --force: the substrate's dynamic-only guard is what protects
        # base lanes from automation. Forcing is a human-at-keyboard act.
        self._run("loop-tmux", "drop-lane", "--session", self.session, "--window", window)

    # ── dispatch ──────────────────────────────────────────────────────────

    def dispatch(
        self,
        lane: str,
        payload: str,
        mode: str = "text",
        wait_ready: bool = False,
        interrupt: bool = False,
        timeout: float = 90,  # covers 20s ready-poll + 2s paste delay + margin
    ) -> None:
        """At-most-once delivery; raises on non-zero exit. Never retry blindly —
        a double paste into a TUI composer is worse than a missed dispatch."""
        args = ["--session", self.session, "--mode", mode]
        if wait_ready:
            args.append("--wait-ready")
        if interrupt:
            args.append("--interrupt")
        args += [lane, payload]
        self._run("loop-dispatch", *args, timeout=timeout)

    # ── compiled state ────────────────────────────────────────────────────

    def digest(self) -> dict:
        return self._run_json("loop-digest", "--project-root", str(self.project_root), "--json")

    def checkpoint_prompt(self, header_file: str | Path | None = None) -> str:
        args = ["--print", "--project-root", str(self.project_root)]
        if header_file:
            args += ["--header-file", str(header_file)]
        return self._run("loop-checkpoint", *args).stdout

    def pending_count(self) -> int:
        out = self._run(
            "loop-wiki-pending", "--project-root", str(self.project_root), "--quiet"
        ).stdout.strip()
        try:
            return int(out)
        except ValueError as exc:
            raise SubstrateError(
                ["loop-wiki-pending"], 0, f"expected integer, got {out!r}"
            ) from exc

    # ── metrics + lint (scripts/ helpers) ─────────────────────────────────

    def metrics_log(self, session: str | None = None) -> str:
        """Record the T0006 metrics block (`loop-metrics --log` appends one
        `## [date] metrics | …` entry to ops-wiki/log.md)."""
        return self._run(
            "loop-metrics",
            "--session",
            session or self.session,
            "--project-root",
            str(self.project_root),
            "--log",
            timeout=60,
        ).stdout

    def wiki_lint_dispatch(self, timeout: float = 180) -> str:
        """Kick off a wiki lint run (`loop-wiki-lint --dispatch` creates the
        dynamic lint window and pastes the assembled prompt). Retiring the
        window afterwards stays the operator's call — v1 never auto-drops."""
        return self._run(
            "loop-wiki-lint",
            "--dispatch",
            "--session",
            self.session,
            "--project-root",
            str(self.project_root),
            timeout=timeout,
        ).stdout

    # ── harness registry ──────────────────────────────────────────────────

    def harness_field(self, name: str, field: str) -> str:
        key = (name, field)
        if key not in self._registry_cache:
            self._registry_cache[key] = self._run(
                "harness-registry", "field", name, field, timeout=10
            ).stdout.strip()
        return self._registry_cache[key]

    def oneshot_template(self, name: str) -> str:
        """One-shot command template with {prompt} placeholder; raises if the
        harness has no one-shot mode (registry exits 1)."""
        return self._run("harness-registry", "oneshot", name, timeout=10).stdout.strip()

    # ── deck support ──────────────────────────────────────────────────────
    # The deck is a NON-WRITER: every mutation it triggers goes through the
    # same audited CLIs a human would use. These wrappers exist so the deck
    # never imports subprocess itself (CI-enforced boundary).

    def adr_accept(self, adr_id: str, adr_dir: str | Path | None = None) -> str:
        """Run the human-gated ADR accept. Only ever called from an explicit
        human keypress in the deck — automation must never reach this."""
        args = ["accept", adr_id]
        if adr_dir:
            args += ["--adr-dir", str(adr_dir)]
        return self._run("loop-adr", *args).stdout

    def capture_pane(self, lane: str, lines: int = 40) -> str:
        """Read-only pane tail for the lane-detail view. Pane resolution stays
        in bash (--print-target); only the capture itself calls tmux, which
        CONTRACT.md lists as an allowed deck read."""
        target = self.print_target(lane)
        proc = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", target],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            raise SubstrateError(
                ["tmux", "capture-pane", "-t", target], proc.returncode, proc.stderr
            )
        return "\n".join(proc.stdout.splitlines()[-lines:])

    def jump_to_window(self, window: str) -> None:
        """Switch the current tmux client to a lane's window (deck 'g' key).
        select-window when the deck runs inside the target session,
        switch-client otherwise. No-op error if not inside tmux."""
        inside = os.environ.get("TMUX")
        if not inside:
            raise SubstrateError(["tmux"], None, "not inside tmux (use attach instead)")
        target = f"{self.session}:{window}"
        proc = subprocess.run(
            ["tmux", "switch-client", "-t", target],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                ["tmux", "select-window", "-t", target],
                capture_output=True,
                text=True,
                timeout=10,
            )
        if proc.returncode != 0:
            raise SubstrateError(
                ["tmux", "switch-client", "-t", target], proc.returncode, proc.stderr
            )

    def engine_cmd(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess:
        """Invoke the loop-engine CLI in-interpreter (python -m) so the deck's
        approve/reject/pause/checkpoint actions execute through the exact same
        audited path as a human at the shell. Returns the completed process
        (callers surface stdout/stderr as toasts; non-zero is NOT raised —
        a failed approve is a user-visible outcome, not a deck crash)."""
        import sys

        argv = [
            sys.executable,
            "-m",
            "loop_orchestrator.engine.cli",
            "--project-root",
            str(self.project_root),
            "--session",
            self.session,
            *args,
        ]
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=self.project_root,
        )
