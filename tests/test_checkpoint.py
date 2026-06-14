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


# ── T0021 ledger projection of the checkpoint compiled region ────────────────

import json  # noqa: E402

_CHECKPOINT = (
    "# checkpoint\n\n## Current objective\nHAND-AUTHORED objective\n\n"
    "<!-- coord-decisions -->\n## Decision needed\n(none)\n"
    "### [t] decision d-1 (approved)\nbody content\n"
)


def _project_with_checkpoint(tmp_path):
    (tmp_path / "ops-wiki").mkdir()
    (tmp_path / "ops-wiki" / "checkpoint.md").write_text(_CHECKPOINT, encoding="utf-8")
    (tmp_path / "ops-wiki" / "index.md").write_text("# index\n", encoding="utf-8")
    (tmp_path / ".loop" / "messages").mkdir(parents=True)
    return tmp_path


def test_absent_ledger_emits_hand_authored_region(tmp_path):
    r = _run(_project_with_checkpoint(tmp_path), ceiling="1000000")
    assert r.returncode == 0
    assert "HAND-AUTHORED objective" in r.stdout  # byte-identical fallback
    assert "canonical loop ledger" not in r.stdout  # no projection header


def test_present_ledger_projects_region_and_preserves_coord_region(tmp_path):
    proj = _project_with_checkpoint(tmp_path)
    (proj / ".loop" / "orchestrator-state.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "updated_at": "2026-06-14T01:00:00Z",
                "objective": "LEDGER objective: Phase 3",
                "loops": {
                    "harness-governance": {
                        "status": "in-progress",
                        "branch": "feature/harness-governance",
                        "blast_radius": "engine+lib",
                        "commits": ["cea8288"],
                    }
                },
                "open_conflicts": ["validate-left errored"],
            }
        ),
        encoding="utf-8",
    )
    r = _run(proj, ceiling="1000000")
    assert r.returncode == 0
    assert "canonical loop ledger" in r.stdout  # projection header
    assert "LEDGER objective: Phase 3" in r.stdout  # objective from ledger
    assert "**harness-governance** — status=in-progress" in r.stdout  # loop state
    assert "validate-left errored" in r.stdout  # open conflict
    assert "HAND-AUTHORED objective" not in r.stdout  # compiled region replaced
    # coord-decisions region (marker onward) preserved byte-for-byte
    assert "<!-- coord-decisions -->\n## Decision needed\n(none)" in r.stdout
    assert "### [t] decision d-1 (approved)" in r.stdout


def test_unparseable_ledger_falls_back(tmp_path):
    proj = _project_with_checkpoint(tmp_path)
    (proj / ".loop" / "orchestrator-state.json").write_text("{not valid json", encoding="utf-8")
    r = _run(proj, ceiling="1000000")
    assert r.returncode == 0
    assert "HAND-AUTHORED objective" in r.stdout  # malformed ledger -> fallback
    assert "canonical loop ledger" not in r.stdout


def test_sparse_ledger_projects_loops_preserves_hand_authored_objective(tmp_path):
    # F5 (T0024): a loops-only ledger (no objective) must project its loops yet
    # PRESERVE the hand-authored objective — never clobber it with "(none)".
    proj = _project_with_checkpoint(tmp_path)
    (proj / ".loop" / "orchestrator-state.json").write_text(
        json.dumps({"schema_version": 2, "loops": {"alpha": {"status": "working"}}}),
        encoding="utf-8",
    )
    r = _run(proj, ceiling="1000000")
    assert r.returncode == 0
    assert "canonical loop ledger" in r.stdout  # projection is active
    assert "**alpha** — status=working" in r.stdout  # loops projected from the ledger
    assert "HAND-AUTHORED objective" in r.stdout  # objective PRESERVED (the F5 fix)
    assert "none recorded in ledger" not in r.stdout  # not clobbered
    # coord-decisions region still preserved byte-for-byte
    assert "<!-- coord-decisions -->\n## Decision needed\n(none)" in r.stdout
