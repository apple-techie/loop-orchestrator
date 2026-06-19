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
    # T0019: declared lane kind ('standing' | 'worker'). list-lanes resolves it
    # (explicit @loop_lane_kind, else base->standing / dynamic->worker), so it is
    # always populated from a live snapshot; None only for a pre-T0019 payload.
    kind: str | None = None


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

    def _run_process(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.project_root,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            raise SubstrateError(
                argv, None, f"timed out after {timeout}s\n{stdout}{stderr}"
            ) from exc
        except OSError as exc:
            raise SubstrateError(argv, None, f"spawn failed: {exc}") from exc

    # ── verify runner ─────────────────────────────────────────────────────

    def run_gate(self, timeout: float) -> tuple[bool, str]:
        chunks: list[str] = []
        for argv in (["make", "check"], ["uv", "run", "pytest"]):
            try:
                proc = self._run_process(argv, timeout)
            except SubstrateError as exc:
                chunks.append(f"$ {' '.join(argv)}\n{exc.stderr}")
                return False, "\n".join(chunks)
            output = f"$ {' '.join(argv)}\n{proc.stdout}{proc.stderr}"
            chunks.append(output)
            if proc.returncode != 0:
                return False, "\n".join(chunks)
        return True, "\n".join(chunks)

    def git_diff(self, base: str, tip: str, timeout: float = 60) -> str:
        if not base.strip():
            raise SubstrateError(["git", "diff"], None, "base ref must be non-empty")
        if not tip.strip():
            raise SubstrateError(["git", "diff"], None, "tip ref must be non-empty")
        argv = ["git", "diff", f"{base}..{tip}"]
        proc = self._run_process(argv, timeout)
        if proc.returncode != 0:
            raise SubstrateError(argv, proc.returncode, proc.stderr)
        return proc.stdout

    def _verify_argv(self) -> list[str]:
        hit = shutil.which("loop-verify")
        if hit:
            return [hit]
        uv = shutil.which("uv")
        if uv:
            return [uv, "run", "loop-verify"]
        raise SubstrateError(
            ["loop-verify"], None, "loop-verify not found on PATH and uv is unavailable"
        )

    def _cloexec_pipe(self) -> tuple[int, int]:
        if hasattr(os, "pipe2") and hasattr(os, "O_CLOEXEC"):
            return os.pipe2(os.O_CLOEXEC)
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, False)
        os.set_inheritable(write_fd, False)
        return read_fd, write_fd

    def spawn_verify(
        self,
        worktree: str | Path,
        base: str,
        tip: str,
        out_path: str | Path,
    ) -> int:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        log_path = out.with_suffix(".log")
        argv = self._verify_argv() + [
            "--worktree",
            str(worktree),
            "--base",
            base,
            "--tip",
            tip,
            "--out",
            str(out),
        ]
        pid_read_fd, pid_write_fd = self._cloexec_pipe()
        exec_read_fd, exec_write_fd = self._cloexec_pipe()
        try:
            child_pid = os.fork()
        except OSError as exc:
            os.close(pid_read_fd)
            os.close(pid_write_fd)
            os.close(exec_read_fd)
            os.close(exec_write_fd)
            raise SubstrateError(argv, None, f"spawn failed: {exc}") from exc
        if child_pid == 0:
            os.close(pid_read_fd)
            os.close(exec_read_fd)
            try:
                os.setsid()
                grandchild_pid = os.fork()
                if grandchild_pid != 0:
                    os.close(exec_write_fd)
                    os.write(pid_write_fd, str(grandchild_pid).encode("ascii"))
                    os._exit(0)
                os.close(pid_write_fd)
                os.setpgid(0, 0)
                os.chdir(self.project_root)
                log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.dup2(log_fd, 1)
                    os.dup2(log_fd, 2)
                finally:
                    os.close(log_fd)
                os.execvp(argv[0], argv)
            except BaseException as exc:
                try:
                    msg = f"spawn failed: {argv[0]}: {exc}\n".encode("utf-8", errors="replace")
                    os.write(exec_write_fd, msg)
                except OSError:
                    pass
                os._exit(127)
        os.close(pid_write_fd)
        os.close(exec_write_fd)
        try:
            data = os.read(pid_read_fd, 64)
            _, status = os.waitpid(child_pid, 0)
            exec_error = bytearray()
            while True:
                chunk = os.read(exec_read_fd, 4096)
                if not chunk:
                    break
                exec_error.extend(chunk)
        finally:
            os.close(pid_read_fd)
            os.close(exec_read_fd)
        if exec_error:
            raise SubstrateError(argv, 127, exec_error.decode("utf-8", errors="replace"))
        if not data or status != 0:
            raise SubstrateError(argv, status, "verify child failed to detach")
        return int(data.decode("ascii"))

    def read_verify_result(self, out_path: str | Path) -> dict | None:
        import json

        path = Path(out_path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            return None
        return result if isinstance(result, dict) else None

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
                kind=lane.get("kind"),
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
        worktree: bool = False,
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
        # T0025: opt-in git-worktree isolation. Default False = shared (today's
        # behavior); loop-tmux also honors a worktree harness via the registry.
        if worktree:
            args += ["--worktree"]
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
        no_clear: bool = False,
        timeout: float = 90,  # covers 20s ready-poll + clear settle + 2s paste delay + margin
    ) -> None:
        """At-most-once delivery; raises on non-zero exit. Never retry blindly —
        a double paste into a TUI composer is worse than a missed dispatch.

        loop-dispatch auto-sends `/clear` before a FRESH (non-interrupt) dispatch
        into an idle claude lane so a lane never accumulates context across a
        session and stalls; `no_clear=True` opts out (steers always opt out via
        `interrupt=True`)."""
        args = ["--session", self.session, "--mode", mode]
        if wait_ready:
            args.append("--wait-ready")
        if interrupt:
            args.append("--interrupt")
        if no_clear:
            args.append("--no-clear")
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

    def task_lint(self, tasks_dir: str | Path | None = None) -> subprocess.CompletedProcess:
        """Validate the tasks-as-files convention (F9 wrapper). Passes an explicit
        --tasks-dir (default: project_root/tasks == paths.tasks_dir) so the lint
        targets THIS loop's tasks, not loop-task-lint's install-relative default —
        the engine-internal path is now worktree-correct like digest/checkpoint.
        check=False: exit 1 means lint findings (a normal, inspectable result), not
        a substrate crash — callers read returncode/stdout."""
        target = str(tasks_dir or (self.project_root / "tasks"))
        return self._run("loop-task-lint", "--tasks-dir", target, check=False)

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

    def harness_roster(self) -> dict[str, dict]:
        """Governance roster snapshot keyed by harness name (`harness-registry
        roster --json`): per-harness governance fields + a `present` flag.
        Never cached — `present` reflects the host's PATH right now."""
        doc = self._run_json("harness-registry", "roster", "--json", timeout=15)
        return {entry["name"]: entry for entry in doc.get("harnesses", [])}

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


# ── cross-session fleet view (T0033/B3): read-only aggregation over many loops ──


@dataclass(frozen=True)
class LoopSummary:
    """A read-only snapshot of one loop for the cross-session fleet view, built
    entirely from engine state files (engine.pid / paused / pending-decision.json
    / snapshot.json) — no tmux, no subprocess, no writes."""

    project_root: str
    session: str
    engine: str  # running | paused | stopped
    pid: int | None
    pending: int  # in-flight decisions (the engine keeps at most one)
    awaiting_approval: bool
    lane_health: dict[str, int]  # lane status -> count, from snapshot.json


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def _loop_summary(project_root: Path, session: str) -> LoopSummary:
    from .locking import read_json
    from .paths import SessionPaths

    paths = SessionPaths(project_root, session)
    try:
        pid: int | None = int(paths.pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = None
    alive = pid is not None and _pid_alive(pid)
    if not alive:
        engine, pid_out = "stopped", None
    elif paths.paused_path.exists():
        engine, pid_out = "paused", pid
    else:
        engine, pid_out = "running", pid

    pending, awaiting = 0, False
    decision = read_json(paths.pending_decision_path, None)
    if isinstance(decision, dict):
        pending = 1
        awaiting = any(
            isinstance(action, dict) and action.get("status") == "awaiting-approval"
            for action in decision.get("actions") or []
        )

    lane_health: dict[str, int] = {}
    snap = read_json(paths.snapshot_path, None)
    if isinstance(snap, dict):
        for info in (snap.get("lanes") or {}).values():
            status = info.get("status") if isinstance(info, dict) else None
            if status:
                lane_health[status] = lane_health.get(status, 0) + 1
    return LoopSummary(str(project_root), session, engine, pid_out, pending, awaiting, lane_health)


def discover_loops(roots: list[Path]) -> list[LoopSummary]:
    """Every loop with engine state under any of `roots`
    (`<root>/.loop/sessions/<session>/engine/`) as read-only LoopSummary rows —
    running, paused, AND stopped. De-duped by (resolved root, session); a root
    with no sessions contributes nothing. Pure reads — never writes."""
    out: list[LoopSummary] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        root = Path(root)
        sessions_dir = root / ".loop" / "sessions"
        if not sessions_dir.is_dir():
            continue
        for session_dir in sorted(sessions_dir.iterdir()):
            if not (session_dir / "engine").is_dir():
                continue
            key = (str(root.resolve()), session_dir.name)
            if key in seen:
                continue
            seen.add(key)
            out.append(_loop_summary(root, session_dir.name))
    return out


def render_fleet(summaries: list[LoopSummary]) -> str:
    """Plain-text fleet table for `loop-deck --all` (a read-only view). Empty =>
    a clear 'no loops' line. `pending` shows '1*' when a decision awaits approval."""
    if not summaries:
        return "no loops found (nothing under any <root>/.loop/sessions/)"
    lines = [f"{'SESSION':<18} {'ENGINE':<8} {'PEND':<5} LANE HEALTH"]
    for summary in summaries:
        health = " ".join(f"{k}:{v}" for k, v in sorted(summary.lane_health.items())) or "-"
        pend = f"{summary.pending}{'*' if summary.awaiting_approval else ''}"
        lines.append(f"{summary.session:<18} {summary.engine:<8} {pend:<5} {health}")
    return "\n".join(lines)
