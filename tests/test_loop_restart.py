"""bin/loop-restart — the sanctioned PM-aware restart wrapper (T0031 / B1).

Exercises the wrapper as a subprocess with stub `loop-engine`/`loop-pm` on PATH
and a no-op reinstall (LOOP_RESTART_INSTALL_CMD=':'), so no daemon is touched.
Proves: a PM loop with env present + adapter available exits 0; the same loop
with the env file absent fails BEFORE restart; a PM loop whose adapter is still
unavailable after restart fails the assert; a no-PM loop (govern) exits 0.

The `loop-engine` stub emulates the REAL daemon (F10/T0037): `restart` BLOCKS
forever (a heartbeat loop, like Watch.run()) and `status` reports `watch: alive
(...)` once the daemon is up. So the old foreground-blocking wrapper hangs here
(caught as a timeout — see test_restart_is_nonblocking) and only the
background+poll wrapper completes.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "loop-restart"

LANE_CONFIG_PM = "engine:\n  pm:\n    adapters:\n      - name: jira\n"
LANE_CONFIG_NO_PM = "engine:\n  pm:\n    adapters: []\n"

_RUN_TIMEOUT = 15  # a hung (foreground-blocking) wrapper trips this -> timed_out


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


@dataclass
class _Result:
    returncode: int | None
    stderr: str
    timed_out: bool
    engine_marker: Path


def _engine_stub(marker: Path, ready_file: Path) -> str:
    # `restart` records the call, marks the daemon ready, then BLOCKS in a
    # heartbeat loop forever (the real Watch.run() never returns). `status`
    # reports alive with a fresh heartbeat once the ready file exists.
    return (
        'cmd="${@: -1}"\n'
        'case "$cmd" in\n'
        f'  restart) echo restart >> "{marker}"; : > "{ready_file}";\n'
        f'    while true; do : > "{ready_file}"; sleep 0.2; done ;;\n'
        f'  status) if [ -f "{ready_file}" ]; then\n'
        '      echo "watch: alive (pid $$, heartbeat 0s ago)";\n'
        '    else echo "watch: not running"; fi ;;\n'
        "esac\n"
        "exit 0\n"
    )


def _run(
    tmp_path: Path,
    *,
    lane_config: str | None,
    env_present: bool,
    pm_listing: str = "",
    session: str = "govern",
) -> _Result:
    root = tmp_path / "proj"
    root.mkdir()
    if lane_config is not None:
        (root / "lane-config.yaml").write_text(lane_config, encoding="utf-8")

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    if env_present:
        (secrets / f"{session}.env").write_text(
            "export JIRA_BASE_URL=https://example.atlassian.net\n", encoding="utf-8"
        )

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    engine_marker = tmp_path / "engine-called"
    _stub(stub_bin / "loop-engine", _engine_stub(engine_marker, tmp_path / "daemon-ready"))
    # the wrapper only invokes `loop-pm list-adapters`; echo the canned listing.
    _stub(
        stub_bin / "loop-pm",
        f"if [ \"$1\" = list-adapters ]; then cat <<'EOF'\n{pm_listing}\nEOF\nfi\nexit 0\n",
    )

    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "LOOP_SECRETS_DIR": str(secrets),
        "LOOP_RESTART_INSTALL_CMD": ":",  # no-op reinstall — never touch a daemon
        "LOOP_RESTART_TIMEOUT": "10",
    }
    # Own session/process group so we can reap the wrapper AND the backgrounded
    # (or hung-foreground) daemon stub on completion or timeout. start_new_session
    # makes the wrapper a group leader (pgid == its pid); capture that pgid NOW —
    # on the green path the wrapper exits and is reaped by communicate(), so a
    # later os.getpgid(proc.pid) would raise ProcessLookupError and the orphaned
    # daemon (which outlives the wrapper, but stays in the group) would leak.
    proc = subprocess.Popen(
        [str(WRAPPER), session, "--project-root", str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    pgid = proc.pid  # group leader pgid; survives the leader's exit
    timed_out = False
    try:
        _, stderr = proc.communicate(timeout=_RUN_TIMEOUT)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        returncode = None
        stderr = ""
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if timed_out:
            try:
                _, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stderr = ""
    return _Result(returncode, stderr, timed_out, engine_marker)


def test_restart_is_nonblocking_reaches_pm_assert(tmp_path):
    """F10/T0037 regression guard: the daemon stub BLOCKS forever (like the real
    Watch.run()), so a synchronous `loop-engine restart` hangs the wrapper and
    this trips the timeout. The background+poll wrapper detects readiness, runs
    the PM-assert, and exits 0 — i.e. it must NEVER foreground-block."""
    proc = _run(
        tmp_path, lane_config=LANE_CONFIG_PM, env_present=True, pm_listing="jira  available"
    )
    assert not proc.timed_out, "wrapper foreground-blocked on the never-returning daemon"
    assert proc.returncode == 0, proc.stderr
    assert "daemon alive" in proc.stderr
    assert proc.engine_marker.exists()  # the (backgrounded) restart actually ran


def test_pm_loop_env_present_adapter_available_exits_0(tmp_path):
    proc = _run(
        tmp_path, lane_config=LANE_CONFIG_PM, env_present=True, pm_listing="jira  available"
    )
    assert not proc.timed_out, proc.stderr
    assert proc.returncode == 0, proc.stderr
    assert "PM adapter(s) available: jira" in proc.stderr
    assert proc.engine_marker.exists()  # restart actually ran


def test_pm_loop_env_absent_fails_before_restart(tmp_path):
    proc = _run(tmp_path, lane_config=LANE_CONFIG_PM, env_present=False)
    assert proc.returncode == 1
    assert "missing" in proc.stderr and "jira" in proc.stderr
    assert not proc.engine_marker.exists()  # aborted BEFORE the restart


def test_pm_loop_adapter_unavailable_after_restart_fails_assert(tmp_path):
    proc = _run(
        tmp_path,
        lane_config=LANE_CONFIG_PM,
        env_present=True,
        pm_listing="jira  unavailable (missing: JIRA_API_TOKEN)",
    )
    assert proc.returncode == 1
    assert "NOT available after restart" in proc.stderr
    assert proc.engine_marker.exists()  # restart ran; the assert caught it


def test_no_pm_loop_exits_0_without_env(tmp_path):
    proc = _run(tmp_path, lane_config=LANE_CONFIG_NO_PM, env_present=False)
    assert proc.returncode == 0, proc.stderr
    assert "no PM adapter configured" in proc.stderr
    assert proc.engine_marker.exists()


def test_missing_session_arg_is_usage_error(tmp_path):
    proc = subprocess.run([str(WRAPPER)], capture_output=True, text=True, env={**os.environ})
    assert proc.returncode == 2
    assert "usage:" in proc.stderr
