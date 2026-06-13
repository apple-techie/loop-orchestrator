"""loop-checkpoint.sh hard token gate (T0022).

The bytes/4 token estimate is now a hard gate, not just a stderr warning: a
prompt over the configurable ceiling is refused (exit 3) rather than fed to
coord as a runaway boot context.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "loop-checkpoint.sh"


def _project(tmp_path: Path) -> Path:
    (tmp_path / "ops-wiki").mkdir()
    (tmp_path / "ops-wiki" / "checkpoint.md").write_text(
        "# checkpoint\n<!-- coord-decisions -->\nbody content\n", encoding="utf-8"
    )
    (tmp_path / "ops-wiki" / "index.md").write_text("# index\n", encoding="utf-8")
    (tmp_path / ".loop" / "messages").mkdir(parents=True)
    return tmp_path


def _run(proj: Path, *args: str, ceiling: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if ceiling is not None:
        env["LOOP_CHECKPOINT_TOKEN_CEILING"] = ceiling
    return subprocess.run(
        ["bash", str(SCRIPT), "--print", "--project-root", str(proj), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_token_hard_gate_refuses_over_ceiling(tmp_path):
    r = _run(_project(tmp_path), ceiling="1")
    assert r.returncode == 3
    assert "hard ceiling" in r.stderr
    assert r.stdout == ""  # nothing emitted — the prompt is refused, not printed


def test_token_hard_gate_passes_under_ceiling(tmp_path):
    r = _run(_project(tmp_path), ceiling="1000000")
    assert r.returncode == 0
    assert "body content" in r.stdout


def test_token_ceiling_flag_overrides(tmp_path):
    r = _run(_project(tmp_path), "--token-ceiling", "1")
    assert r.returncode == 3
    assert "hard ceiling" in r.stderr
