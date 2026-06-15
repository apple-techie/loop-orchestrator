"""bin/loop-restart — the sanctioned PM-aware restart wrapper (T0031 / B1).

Exercises the wrapper as a subprocess with stub `loop-engine`/`loop-pm` on PATH
and a no-op reinstall (LOOP_RESTART_INSTALL_CMD=':'), so no daemon is touched.
Proves: a PM loop with env present + adapter available exits 0; the same loop
with the env file absent fails BEFORE restart; a PM loop whose adapter is still
unavailable after restart fails the assert; a no-PM loop (govern) exits 0.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "loop-restart"

LANE_CONFIG_PM = "engine:\n  pm:\n    adapters:\n      - name: jira\n"
LANE_CONFIG_NO_PM = "engine:\n  pm:\n    adapters: []\n"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run(
    tmp_path: Path,
    *,
    lane_config: str | None,
    env_present: bool,
    pm_listing: str = "",
    session: str = "govern",
) -> subprocess.CompletedProcess[str]:
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
    _stub(stub_bin / "loop-engine", f'echo "$*" >> "{engine_marker}"\nexit 0\n')
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
    }
    proc = subprocess.run(
        [str(WRAPPER), session, "--project-root", str(root)],
        capture_output=True,
        text=True,
        env=env,
    )
    proc.engine_marker = engine_marker  # type: ignore[attr-defined]
    return proc


def test_pm_loop_env_present_adapter_available_exits_0(tmp_path):
    proc = _run(
        tmp_path, lane_config=LANE_CONFIG_PM, env_present=True, pm_listing="jira  available"
    )
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
