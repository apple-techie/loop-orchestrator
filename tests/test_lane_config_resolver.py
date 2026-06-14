"""lib/lane-config-resolver.sh — the declared `kind` field (T0019).

Runs the resolver through bash (no tmux) to pin the new lane-config `kind`
field: it is parsed and surfaced, optional (absent configs validate unchanged),
and validated to standing|worker when present.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

RESOLVER = Path(__file__).resolve().parents[1] / "lib" / "lane-config-resolver.sh"


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "lane-config.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(RESOLVER), *args], capture_output=True, text=True)


def test_kind_field_is_parsed(tmp_path):
    cfg = _write(
        tmp_path,
        "lanes:\n"
        "  web:\n    harness: claude\n    kind: standing\n"
        "  w1:\n    harness: claude\n    kind: worker\n",
    )
    assert _run("lane-field", "web", "kind", "--lane-config", str(cfg)).stdout.strip() == "standing"
    assert _run("lane-field", "w1", "kind", "--lane-config", str(cfg)).stdout.strip() == "worker"


def test_absent_kind_is_empty_and_valid(tmp_path):
    # A pre-T0019 config (no kind) resolves to "" and validates unchanged.
    cfg = _write(tmp_path, "lanes:\n  web:\n    harness: claude\n  infra:\n    harness: shell\n")
    assert _run("lane-field", "web", "kind", "--lane-config", str(cfg)).stdout.strip() == ""
    assert _run("validate", "--lane-config", str(cfg)).returncode == 0


def test_invalid_kind_fails_validation(tmp_path):
    cfg = _write(tmp_path, "lanes:\n  web:\n    harness: claude\n    kind: bogus\n")
    r = _run("validate", "--lane-config", str(cfg))
    assert r.returncode != 0
    assert "invalid kind" in r.stderr


def test_standing_and_worker_validate_clean(tmp_path):
    cfg = _write(
        tmp_path,
        "lanes:\n"
        "  coord:\n    harness: claude\n    kind: standing\n"
        "  w1:\n    harness: claude\n    kind: worker\n",
    )
    assert _run("validate", "--lane-config", str(cfg)).returncode == 0
