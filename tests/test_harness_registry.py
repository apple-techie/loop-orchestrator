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


# ── readiness marker fields (T0015) ─────────────────────────────────────────


def test_readiness_marker_fields_resolve_and_empty_safe():
    # working_marker / idle_marker are declared, regex-valued, and empty-safe.
    import re

    # claude/codex working_marker is a LIVE-only signal: it must match an active
    # generation line (an elapsed timer co-occurring with esc-to-interrupt / tokens
    # / thinking) and must NOT match Claude Code's idle composer footer, which
    # carries a bare "esc to interrupt" hint with no timer. A bare-string marker
    # false-positived every idle Claude lane as working and stalled the engine loop.
    idle_footer = "⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt"
    for h in ("claude", "codex"):
        marker = field_value(h, "working_marker")
        assert marker  # declared, non-empty
        assert re.search(marker, "• Working (1m 36s • esc to interrupt)")
        assert re.search(marker, "✶ Flibbertigibbeting… (5m 7s · 17.6k tokens · thinking)")
        assert not re.search(marker, idle_footer)
    assert field_value("claude", "idle_marker") == "accept edits on|bypass permissions on"
    assert "esc to interrupt" in field_value("pi", "working_marker")
    # Harnesses with no declared marker return "" (heuristics only), exit 0.
    assert field_value("amp", "working_marker") == ""
    assert field_value("shell", "working_marker") == ""
    assert field_value("shell", "idle_marker") == ""


def test_readiness_markers_resolve_for_every_harness():
    for name in HARNESSES:
        field_value(name, "working_marker")  # asserts exit 0 internally
        field_value(name, "idle_marker")


def test_fields_verb_includes_readiness_rows():
    proc = run_cli("fields", "claude")
    assert proc.returncode == 0
    assert "working_marker" in proc.stdout
    assert "idle_marker" in proc.stdout


# ── model_failover field (T0018, F3) ────────────────────────────────────────


def test_model_failover_field_resolves_and_empty_safe():
    # Declared fallback-model slot (F3). Operator-supplied per environment, so
    # the default is empty everywhere (empty-safe), and the field resolves for
    # every harness.
    for name in HARNESSES:
        assert field_value(name, "model_failover") == ""
    proc = run_cli("fields", "claude")
    assert "model_failover" in proc.stdout


# ── isolation field (T0025, Phase 4) ────────────────────────────────────────


def test_isolation_field_defaults_shared_for_every_harness():
    # Phase 4 worktree isolation is DORMANT by default: every harness declares
    # `shared`, so an add-lane lane inherits the project root (today's behavior)
    # until a lane opts in via --worktree.
    for name in HARNESSES:
        assert field_value(name, "isolation") == "shared"
    proc = run_cli("fields", "claude")
    assert "isolation" in proc.stdout


# ── roster + health verbs (T0011) ──────────────────────────────────────────


def test_roster_json_contract():
    import json

    proc = run_cli("roster", "--json")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["contract_version"] == 1
    entries = {h["name"]: h for h in doc["harnesses"]}
    assert list(entries) == HARNESSES
    for entry in entries.values():
        assert isinstance(entry["present"], bool)
        for field in GOVERNANCE_FIELDS:
            assert field in entry
    assert entries["claude"]["drift_pins"] == "low"
    # shell needs no binary, so it is always present.
    assert entries["shell"]["present"] is True
    # oneshot_template is exposed (T0017 F1): agent lanes have a non-empty
    # template, non-agent shell/dashboard lanes are empty.
    assert entries["claude"]["oneshot_template"] == "claude -p {prompt}"
    assert entries["shell"]["oneshot_template"] == ""
    assert entries["mprocs"]["oneshot_template"] == ""


def test_roster_plain_lists_every_harness():
    proc = run_cli("roster")
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert len(lines) == len(HARNESSES)
    assert [line.split()[0] for line in lines] == HARNESSES


def test_health_shell_ok():
    # shell has no binary and no probe: always ok, exit 0.
    proc = run_cli("health", "shell")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ok"


def test_health_unknown_harness():
    proc = run_cli("health", "nosuch")
    assert proc.returncode == 1
    assert "unknown harness" in proc.stderr
    assert proc.stdout == ""


def test_health_missing_binary():
    import os

    # A PATH without the codex binary (but with bash) must read missing.
    env = {**os.environ, "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        ["bash", str(REGISTRY), "health", "codex"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 1
    assert proc.stdout.strip() == "missing"


def _health_with_overrides(overrides: str) -> subprocess.CompletedProcess[str]:
    # Drive the probe paths hermetically: source the registry, override the
    # target harness's governance vars in-shell, then call the CLI entrypoint.
    script = f'source "{REGISTRY}"; {overrides}; _harness_registry_cli health shell'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


def test_health_probe_pass_is_ok():
    proc = _health_with_overrides("HARNESS_SHELL_HEALTH_PROBE=true")
    assert proc.returncode == 0
    assert proc.stdout.strip() == "ok"


def test_health_probe_fail_reads_unhealthy_without_auth():
    proc = _health_with_overrides("HARNESS_SHELL_HEALTH_PROBE=false")
    assert proc.returncode == 1
    assert proc.stdout.strip() == "unhealthy"


def test_health_probe_fail_reads_unauthenticated_with_auth():
    proc = _health_with_overrides(
        "HARNESS_SHELL_HEALTH_PROBE=false; HARNESS_SHELL_AUTH_REQUIREMENT=account"
    )
    assert proc.returncode == 1
    assert proc.stdout.strip() == "unauthenticated"
