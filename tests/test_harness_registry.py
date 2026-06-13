"""Governance fields on the REAL lib/harness-registry.sh (T0010).

Runs the registry script through bash — no fakes — so these tests pin both
the new governance surface and the frozen field/oneshot contract around it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REGISTRY = Path(__file__).resolve().parents[1] / "lib" / "harness-registry.sh"

HARNESSES = [
    "pi",
    "claude",
    "opencode",
    "codex",
    "cursor-agent",
    "hermes",
    "droid",
    "forge",
    "amp",
    "openclaw",
    "mprocs",
    "shell",
]

GOVERNANCE_FIELDS = [
    "capability_tags",
    "cost_tier",
    "autonomy_class",
    "auth_requirement",
    "health_probe",
    "drift_pins",
]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(REGISTRY), *args], capture_output=True, text=True, check=False
    )


def field_value(name: str, field: str) -> str:
    proc = run_cli("field", name, field)
    assert proc.returncode == 0, f"field {name} {field}: {proc.stderr}"
    return proc.stdout.rstrip("\n")


def test_list_unchanged():
    proc = run_cli("list")
    assert proc.returncode == 0
    assert proc.stdout.split() == HARNESSES


def test_frozen_field_and_oneshot_verbs_unchanged():
    assert field_value("claude", "oneshot_template") == "claude -p {prompt}"
    proc = run_cli("oneshot", "pi")
    assert proc.returncode == 1
    assert proc.stdout == ""


def test_governance_fields_resolve_for_every_harness():
    for name in HARNESSES:
        for field in GOVERNANCE_FIELDS:
            field_value(name, field)  # asserts exit 0 internally


def test_governance_values_match_profile_matrix():
    # Spot checks against docs/plans/harness-governance.md A.3.
    assert field_value("claude", "drift_pins") == "low"
    assert "brain" in field_value("claude", "capability_tags").split(",")
    assert field_value("codex", "drift_pins") == "high"
    assert field_value("amp", "drift_pins") == "high"
    assert field_value("hermes", "drift_pins") == "high"
    assert field_value("pi", "capability_tags") == "product,synthesis"
    assert field_value("pi", "autonomy_class") == "attended"
    assert field_value("opencode", "cost_tier") == "low"
    assert field_value("openclaw", "auth_requirement") == "gateway"
    for name in ("mprocs", "shell"):
        assert field_value(name, "autonomy_class") == "none"
        assert field_value(name, "auth_requirement") == "none"


def test_unattended_class_iff_auto_approve_flag():
    # A.3: only harnesses with a real auto-approve flag are unattended-capable.
    for name in HARNESSES:
        unattended = field_value(name, "autonomy_class") == "unattended"
        has_flag = field_value(name, "auto_approve_flag") != ""
        assert unattended == has_flag, name


def test_unset_governance_field_is_empty_safe():
    # A registered harness with no governance vars set returns "" and exit 0,
    # so old/partial registries degrade to today's behavior.
    script = (
        f'source "{REGISTRY}"; HARNESS_REGISTRY_NAMES+=(newtool); '
        'v="$(harness_field newtool capability_tags)" && printf "[%s]" "$v"'
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "[]"


def test_unknown_field_still_rejected():
    # Sourced harness_field returns 1 for an unknown field name. (The CLI
    # `field` verb exits 0 regardless — pre-existing frozen behavior; the
    # trailing newline printf masks the return code.)
    script = f'source "{REGISTRY}"; harness_field claude bogus_field'
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert proc.returncode == 1
    assert "unknown field" in proc.stderr
    cli = run_cli("field", "claude", "bogus_field")
    assert cli.returncode == 0
    assert cli.stdout == "\n"


def test_fields_verb_includes_governance_rows():
    proc = run_cli("fields", "claude")
    assert proc.returncode == 0
    for field in GOVERNANCE_FIELDS:
        assert field in proc.stdout


def test_probe_output_untouched_by_governance_fields():
    # probe is a frozen verb: its output must not grow governance rows.
    proc = run_cli("probe", "shell")
    assert proc.returncode == 0
    for field in GOVERNANCE_FIELDS:
        assert field not in proc.stdout
