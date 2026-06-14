#!/usr/bin/env bash
# scripts/lib/harness-registry.sh
#
# Per-harness contract registry for multi-harness orchestration (M002).
#
# Contract fields:
#   launch_cmd            How to invoke the harness in tmux (full command line)
#   model_flag            How to pass a model: "-m" | "--model" | "config" | "skip"
#   expected_process      Pane process-name regex for lane-status / readiness checks
#   auto_approve_flag     Per-harness analog of claude's --dangerously-skip-permissions ("" if N/A)
#   paste_enter_delay     Seconds to wait between paste and Enter (loop-dispatch.sh PASTE_ENTER_DELAY)
#   skill_dir             Where each harness loads project skills from (relative to repo root)
#   non_interactive_flag  Flag for one-shot subprocess dispatch ("-p", "exec", "run", "")
#   oneshot_template      Full one-shot command template with a {prompt}
#                         placeholder ("" = harness cannot run one-shot).
#                         Unlike non_interactive_flag, this is a complete
#                         runnable shape: e.g. hermes' interactive launch is
#                         `hermes chat --tui` but its one-shot is `hermes -z
#                         {prompt}` — not derivable from launch_cmd + flag.
#                         Callers shlex-split the template and substitute the
#                         {prompt} token as ONE argument (never shell-interp).
#
# Governance fields (harness-governance plan A.1/A.3 — declared FACTS only;
# policy lives in the engine's HarnessPolicy). All empty-safe: harness_field
# returns "" for any unset value, so partial registries degrade to today's
# behavior:
#   capability_tags       Comma-separated capability tags ("code,brain",
#                         "search,research", ...) matched against the
#                         engine policy's role_tag_map
#   cost_tier             Relative model cost: low | medium | high | none
#   autonomy_class        Unattended capability, ordered for the policy's
#                         autonomy cap: none < attended < unattended
#                         (unattended = has a real auto_approve_flag)
#   auth_requirement      What must be live beyond the binary on PATH:
#                         account | gateway | none
#   health_probe          Auth/gateway probe command ("" = none declared;
#                         health degrades to the PATH check)
#   drift_pins            Behavioral drift tier vs the claude baseline:
#                         low | med | high | none (matrix A.3 Drift column)
#   model_failover        Fallback model id to re-pin when this harness's primary
#                         model goes unavailable (F3). "" = none declared.
#                         Availability is per-(harness, model): a harness can be
#                         present + authenticated yet its requested model down.
#   isolation             Lane working-tree isolation (Phase 4 / T0025):
#                         shared | worktree. Default shared = an add-lane lane
#                         inherits PROJECT_ROOT (today's behavior). worktree =
#                         provision a dedicated git worktree per lane. DORMANT
#                         until a lane opts in (--worktree or a worktree harness).
#
# Readiness fields (harness-governance Phase 2 — declared readiness markers
# that loop-lane-status.sh PREFERS over its built-in heuristics when a lane's
# @loop_lane_harness is set, falling back to the heuristics otherwise). Both
# empty-safe (unset = "" = heuristics only) and matched as EXTENDED regexes
# against the FULL captured pane tail, so they must be LIVE-only / persistent
# home-chrome signals — never past-tense recaps or scrollback-stale prompts
# (bare/themed shell prompts stay on the heuristic's bottom-slice path):
#   working_marker        Regex that is present ONLY while the harness is
#                         actively generating (e.g. "esc to interrupt", a
#                         braille spinner). "" = use the working heuristic.
#   idle_marker           Regex for the harness's persistent idle home-chrome
#                         (e.g. claude's "accept edits on"). "" = use the idle
#                         heuristic (home-chrome default + bare/themed prompts).
#
# Source this file from any script that needs to resolve harness behavior:
#   source "$PROJECT_ROOT/scripts/lib/harness-registry.sh"
#   harness_field pi launch_cmd        # -> "pi"
#   harness_field claude auto_approve_flag  # -> "--dangerously-skip-permissions"
#
# CLI mode (when invoked directly):
#   harness-registry.sh list                          # list known harnesses
#   harness-registry.sh fields <name>                 # print all fields for one harness
#   harness-registry.sh field <name> <field>          # print one field
#   harness-registry.sh oneshot <name>                # print one-shot command template
#   harness-registry.sh probe <name>                  # verify binary exists, print resolved launch
#   harness-registry.sh roster [--json]               # every harness + governance fields + present flag
#   harness-registry.sh health <name>                 # ok | missing | unauthenticated | unhealthy
#
# This file MUST be POSIX-bash compatible (no zsh-isms) so it sources cleanly
# under tmux-spawned shells.

# Guard against double-sourcing.
[[ -n "${HARNESS_REGISTRY_LOADED:-}" ]] && return 0
HARNESS_REGISTRY_LOADED=1

# ─── Registry data ───────────────────────────────────────────────────────
# Each harness gets one block of variables. Naming convention:
#   HARNESS_<NAME>_<FIELD>
# Use uppercase for the harness key in variable names, even though the
# external interface uses lowercase ("pi", "claude", etc.).

# pi / gsd-pi — TypeScript agent harness with extension system, GSD lifecycle.
HARNESS_PI_LAUNCH_CMD="pi"
HARNESS_PI_MODEL_FLAG="--model"
HARNESS_PI_EXPECTED_PROCESS="pi"
HARNESS_PI_AUTO_APPROVE_FLAG=""
HARNESS_PI_PASTE_ENTER_DELAY="2.0"
HARNESS_PI_SKILL_DIR=".pi/skills"
HARNESS_PI_NON_INTERACTIVE_FLAG=""
HARNESS_PI_ONESHOT_TEMPLATE=""
HARNESS_PI_CAPABILITY_TAGS="product,synthesis"
HARNESS_PI_COST_TIER="medium"
HARNESS_PI_AUTONOMY_CLASS="attended"
HARNESS_PI_AUTH_REQUIREMENT="account"
HARNESS_PI_HEALTH_PROBE=""
HARNESS_PI_DRIFT_PINS="med"
HARNESS_PI_MODEL_FAILOVER=""
HARNESS_PI_ISOLATION="shared"
HARNESS_PI_WORKING_MARKER="esc to interrupt|^[[:space:]]*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]"
HARNESS_PI_IDLE_MARKER=""

# claude — Anthropic's Claude Code CLI. Anthropic-only models.
# launch_cmd is the bare invocation; consumers append auto_approve_flag
# when they want non-interactive lane behavior.
HARNESS_CLAUDE_LAUNCH_CMD="claude"
HARNESS_CLAUDE_MODEL_FLAG="config"
HARNESS_CLAUDE_EXPECTED_PROCESS="claude"
HARNESS_CLAUDE_AUTO_APPROVE_FLAG="--dangerously-skip-permissions"
HARNESS_CLAUDE_PASTE_ENTER_DELAY="2.0"
HARNESS_CLAUDE_SKILL_DIR=".claude/skills"
HARNESS_CLAUDE_NON_INTERACTIVE_FLAG="-p"
HARNESS_CLAUDE_ONESHOT_TEMPLATE="claude -p {prompt}"
HARNESS_CLAUDE_CAPABILITY_TAGS="brain,ingest,code,ops"
HARNESS_CLAUDE_COST_TIER="high"
HARNESS_CLAUDE_AUTONOMY_CLASS="unattended"
HARNESS_CLAUDE_AUTH_REQUIREMENT="account"
HARNESS_CLAUDE_HEALTH_PROBE=""
HARNESS_CLAUDE_DRIFT_PINS="low"
HARNESS_CLAUDE_MODEL_FAILOVER=""
HARNESS_CLAUDE_ISOLATION="shared"
HARNESS_CLAUDE_WORKING_MARKER="esc to interrupt"
HARNESS_CLAUDE_IDLE_MARKER="accept edits on|bypass permissions on"

# opencode — OpenCode Go TUI. Models via opencode-go provider (mimo/glm/kimi/qwen).
# May spawn as "node" or "opencode" depending on launch path; regex covers both.
HARNESS_OPENCODE_LAUNCH_CMD="opencode"
HARNESS_OPENCODE_MODEL_FLAG="config"
HARNESS_OPENCODE_EXPECTED_PROCESS="opencode|node"
HARNESS_OPENCODE_AUTO_APPROVE_FLAG=""
HARNESS_OPENCODE_PASTE_ENTER_DELAY="2.5"
HARNESS_OPENCODE_SKILL_DIR=".config/opencode"
HARNESS_OPENCODE_NON_INTERACTIVE_FLAG="run"
HARNESS_OPENCODE_ONESHOT_TEMPLATE="opencode run {prompt}"
HARNESS_OPENCODE_CAPABILITY_TAGS="code,bulk"
HARNESS_OPENCODE_COST_TIER="low"
HARNESS_OPENCODE_AUTONOMY_CLASS="attended"
HARNESS_OPENCODE_AUTH_REQUIREMENT="account"
HARNESS_OPENCODE_HEALTH_PROBE=""
HARNESS_OPENCODE_DRIFT_PINS="med"
HARNESS_OPENCODE_MODEL_FAILOVER=""
HARNESS_OPENCODE_ISOLATION="shared"
HARNESS_OPENCODE_WORKING_MARKER=""
HARNESS_OPENCODE_IDLE_MARKER=""

# codex — Codex CLI. Models via --config model=<id> override.
HARNESS_CODEX_LAUNCH_CMD="codex"
HARNESS_CODEX_MODEL_FLAG="--config"
HARNESS_CODEX_EXPECTED_PROCESS="codex|node"
# codex-cli dropped --full-auto; the current skip-all-approvals analog of
# claude's --dangerously-skip-permissions is this flag (verified on 0.139.0).
HARNESS_CODEX_AUTO_APPROVE_FLAG="--dangerously-bypass-approvals-and-sandbox"
HARNESS_CODEX_PASTE_ENTER_DELAY="2.0"
HARNESS_CODEX_SKILL_DIR=".codex"
HARNESS_CODEX_NON_INTERACTIVE_FLAG="exec"
HARNESS_CODEX_ONESHOT_TEMPLATE="codex exec {prompt}"
HARNESS_CODEX_CAPABILITY_TAGS="code,brain"
HARNESS_CODEX_COST_TIER="high"
HARNESS_CODEX_AUTONOMY_CLASS="unattended"
HARNESS_CODEX_AUTH_REQUIREMENT="account"
HARNESS_CODEX_HEALTH_PROBE=""
HARNESS_CODEX_DRIFT_PINS="high"
HARNESS_CODEX_MODEL_FAILOVER=""
HARNESS_CODEX_ISOLATION="shared"
HARNESS_CODEX_WORKING_MARKER="esc to interrupt"
HARNESS_CODEX_IDLE_MARKER=""

# cursor-agent — Cursor Agent CLI. Models via --model flag.
HARNESS_CURSOR_AGENT_LAUNCH_CMD="cursor-agent"
HARNESS_CURSOR_AGENT_MODEL_FLAG="--model"
HARNESS_CURSOR_AGENT_EXPECTED_PROCESS="cursor-agent|node"
HARNESS_CURSOR_AGENT_AUTO_APPROVE_FLAG=""
HARNESS_CURSOR_AGENT_PASTE_ENTER_DELAY="2.0"
HARNESS_CURSOR_AGENT_SKILL_DIR=""
HARNESS_CURSOR_AGENT_NON_INTERACTIVE_FLAG="-p"
HARNESS_CURSOR_AGENT_ONESHOT_TEMPLATE="cursor-agent -p {prompt}"
HARNESS_CURSOR_AGENT_CAPABILITY_TAGS="code"
HARNESS_CURSOR_AGENT_COST_TIER="medium"
HARNESS_CURSOR_AGENT_AUTONOMY_CLASS="attended"
HARNESS_CURSOR_AGENT_AUTH_REQUIREMENT="account"
HARNESS_CURSOR_AGENT_HEALTH_PROBE=""
HARNESS_CURSOR_AGENT_DRIFT_PINS="med"
HARNESS_CURSOR_AGENT_MODEL_FAILOVER=""
HARNESS_CURSOR_AGENT_ISOLATION="shared"
HARNESS_CURSOR_AGENT_WORKING_MARKER=""
HARNESS_CURSOR_AGENT_IDLE_MARKER=""

# hermes — Hermes Agent (NousResearch fork). Python argparse CLI.
# Interactive: `hermes chat --tui` (accepts -m/--model and --yolo).
# One-shot: `hermes -z <prompt>` (a top-level flag).
HARNESS_HERMES_LAUNCH_CMD="hermes chat --tui"
HARNESS_HERMES_MODEL_FLAG="--model"
HARNESS_HERMES_EXPECTED_PROCESS="hermes|python"
HARNESS_HERMES_AUTO_APPROVE_FLAG="--yolo"
HARNESS_HERMES_PASTE_ENTER_DELAY="2.0"
HARNESS_HERMES_SKILL_DIR=".hermes/skills"
HARNESS_HERMES_NON_INTERACTIVE_FLAG="-z"
HARNESS_HERMES_ONESHOT_TEMPLATE="hermes -z {prompt}"
HARNESS_HERMES_CAPABILITY_TAGS="code,experiment"
HARNESS_HERMES_COST_TIER="medium"
HARNESS_HERMES_AUTONOMY_CLASS="unattended"
HARNESS_HERMES_AUTH_REQUIREMENT="account"
HARNESS_HERMES_HEALTH_PROBE=""
HARNESS_HERMES_DRIFT_PINS="high"
HARNESS_HERMES_MODEL_FAILOVER=""
HARNESS_HERMES_ISOLATION="shared"
HARNESS_HERMES_WORKING_MARKER=""
HARNESS_HERMES_IDLE_MARKER=""

# droid — Factory's coding agent. Interactive `droid`; model + autonomy
# (--auto low|medium|high) are exec-only flags, so the interactive lane reads
# them from droid settings (model_flag=config). One-shot is `droid exec`.
HARNESS_DROID_LAUNCH_CMD="droid"
HARNESS_DROID_MODEL_FLAG="config"
HARNESS_DROID_EXPECTED_PROCESS="droid|node"
HARNESS_DROID_AUTO_APPROVE_FLAG=""
HARNESS_DROID_PASTE_ENTER_DELAY="2.0"
HARNESS_DROID_SKILL_DIR=""
HARNESS_DROID_NON_INTERACTIVE_FLAG="exec"
HARNESS_DROID_ONESHOT_TEMPLATE="droid exec {prompt}"
HARNESS_DROID_CAPABILITY_TAGS="code"
HARNESS_DROID_COST_TIER="medium"
HARNESS_DROID_AUTONOMY_CLASS="attended"
HARNESS_DROID_AUTH_REQUIREMENT="account"
HARNESS_DROID_HEALTH_PROBE=""
HARNESS_DROID_DRIFT_PINS="med"
HARNESS_DROID_MODEL_FAILOVER=""
HARNESS_DROID_ISOLATION="shared"
HARNESS_DROID_WORKING_MARKER=""
HARNESS_DROID_IDLE_MARKER=""

# forge — Forge agent CLI (Rust). Interactive by default; model/agent selected
# via `forge config`/agent (model_flag=config). One-shot is `forge -p <prompt>`.
HARNESS_FORGE_LAUNCH_CMD="forge"
HARNESS_FORGE_MODEL_FLAG="config"
HARNESS_FORGE_EXPECTED_PROCESS="forge"
HARNESS_FORGE_AUTO_APPROVE_FLAG=""
HARNESS_FORGE_PASTE_ENTER_DELAY="2.0"
HARNESS_FORGE_SKILL_DIR=""
HARNESS_FORGE_NON_INTERACTIVE_FLAG="-p"
HARNESS_FORGE_ONESHOT_TEMPLATE="forge -p {prompt}"
HARNESS_FORGE_CAPABILITY_TAGS="code"
HARNESS_FORGE_COST_TIER="low"
HARNESS_FORGE_AUTONOMY_CLASS="attended"
HARNESS_FORGE_AUTH_REQUIREMENT="account"
HARNESS_FORGE_HEALTH_PROBE=""
HARNESS_FORGE_DRIFT_PINS="med"
HARNESS_FORGE_MODEL_FAILOVER=""
HARNESS_FORGE_ISOLATION="shared"
HARNESS_FORGE_WORKING_MARKER=""
HARNESS_FORGE_IDLE_MARKER=""

# amp — Sourcegraph Amp. Auto-selects models via --mode (no model id), so
# model_flag=skip. Auto-approve is --dangerously-allow-all; one-shot is `amp -x`.
HARNESS_AMP_LAUNCH_CMD="amp"
HARNESS_AMP_MODEL_FLAG="skip"
HARNESS_AMP_EXPECTED_PROCESS="amp|node"
HARNESS_AMP_AUTO_APPROVE_FLAG="--dangerously-allow-all"
HARNESS_AMP_PASTE_ENTER_DELAY="2.0"
HARNESS_AMP_SKILL_DIR=""
HARNESS_AMP_NON_INTERACTIVE_FLAG="-x"
HARNESS_AMP_ONESHOT_TEMPLATE="amp -x {prompt}"
HARNESS_AMP_CAPABILITY_TAGS="search,research"
HARNESS_AMP_COST_TIER="high"
HARNESS_AMP_AUTONOMY_CLASS="unattended"
HARNESS_AMP_AUTH_REQUIREMENT="account"
HARNESS_AMP_HEALTH_PROBE=""
HARNESS_AMP_DRIFT_PINS="high"
HARNESS_AMP_MODEL_FAILOVER=""
HARNESS_AMP_ISOLATION="shared"
HARNESS_AMP_WORKING_MARKER=""
HARNESS_AMP_IDLE_MARKER=""

# openclaw — OpenClaw gateway runtime. The interactive entrypoint is
# `openclaw tui` (a terminal UI to the running Gateway). Model + approvals are
# gateway/agent config, not CLI flags. One-shot is `openclaw agent --message`.
HARNESS_OPENCLAW_LAUNCH_CMD="openclaw tui"
HARNESS_OPENCLAW_MODEL_FLAG="config"
HARNESS_OPENCLAW_EXPECTED_PROCESS="openclaw|node"
HARNESS_OPENCLAW_AUTO_APPROVE_FLAG=""
HARNESS_OPENCLAW_PASTE_ENTER_DELAY="2.5"
HARNESS_OPENCLAW_SKILL_DIR=""
HARNESS_OPENCLAW_NON_INTERACTIVE_FLAG="agent"
HARNESS_OPENCLAW_ONESHOT_TEMPLATE="openclaw agent --message {prompt}"
HARNESS_OPENCLAW_CAPABILITY_TAGS="ops,fleet"
HARNESS_OPENCLAW_COST_TIER="medium"
HARNESS_OPENCLAW_AUTONOMY_CLASS="attended"
HARNESS_OPENCLAW_AUTH_REQUIREMENT="gateway"
HARNESS_OPENCLAW_HEALTH_PROBE=""
HARNESS_OPENCLAW_DRIFT_PINS="med"
HARNESS_OPENCLAW_MODEL_FAILOVER=""
HARNESS_OPENCLAW_ISOLATION="shared"
HARNESS_OPENCLAW_WORKING_MARKER=""
HARNESS_OPENCLAW_IDLE_MARKER=""

# mprocs — process-group dashboard. Not an LLM harness, but lanes can run it.
HARNESS_MPROCS_LAUNCH_CMD="mprocs"
HARNESS_MPROCS_MODEL_FLAG="skip"
HARNESS_MPROCS_EXPECTED_PROCESS="mprocs"
HARNESS_MPROCS_AUTO_APPROVE_FLAG=""
HARNESS_MPROCS_PASTE_ENTER_DELAY="0"
HARNESS_MPROCS_SKILL_DIR=""
HARNESS_MPROCS_NON_INTERACTIVE_FLAG=""
HARNESS_MPROCS_ONESHOT_TEMPLATE=""
HARNESS_MPROCS_CAPABILITY_TAGS="dashboard"
HARNESS_MPROCS_COST_TIER="none"
HARNESS_MPROCS_AUTONOMY_CLASS="none"
HARNESS_MPROCS_AUTH_REQUIREMENT="none"
HARNESS_MPROCS_HEALTH_PROBE=""
HARNESS_MPROCS_DRIFT_PINS="none"
HARNESS_MPROCS_MODEL_FAILOVER=""
HARNESS_MPROCS_ISOLATION="shared"
HARNESS_MPROCS_WORKING_MARKER=""
HARNESS_MPROCS_IDLE_MARKER=""

# shell — bare shell lane (e.g. ops-top runs a watch command). No harness invocation.
HARNESS_SHELL_LAUNCH_CMD=""
HARNESS_SHELL_MODEL_FLAG="skip"
HARNESS_SHELL_EXPECTED_PROCESS="zsh|bash|sh|watch|ssh"
HARNESS_SHELL_AUTO_APPROVE_FLAG=""
HARNESS_SHELL_PASTE_ENTER_DELAY="0"
HARNESS_SHELL_SKILL_DIR=""
HARNESS_SHELL_NON_INTERACTIVE_FLAG=""
HARNESS_SHELL_ONESHOT_TEMPLATE=""
HARNESS_SHELL_CAPABILITY_TAGS="probe,watch"
HARNESS_SHELL_COST_TIER="none"
HARNESS_SHELL_AUTONOMY_CLASS="none"
HARNESS_SHELL_AUTH_REQUIREMENT="none"
HARNESS_SHELL_HEALTH_PROBE=""
HARNESS_SHELL_DRIFT_PINS="none"
HARNESS_SHELL_MODEL_FAILOVER=""
HARNESS_SHELL_ISOLATION="shared"
HARNESS_SHELL_WORKING_MARKER=""
HARNESS_SHELL_IDLE_MARKER=""

# Ordered list — order matters for `list` output.
HARNESS_REGISTRY_NAMES=(pi claude opencode codex cursor-agent hermes droid forge amp openclaw mprocs shell)
HARNESS_REGISTRY_FIELDS=(launch_cmd model_flag expected_process auto_approve_flag paste_enter_delay skill_dir non_interactive_flag oneshot_template capability_tags cost_tier autonomy_class auth_requirement health_probe drift_pins model_failover isolation working_marker idle_marker)

# ─── Lookup helpers ──────────────────────────────────────────────────────

# Lowercase a harness name and convert hyphens to underscores for variable
# lookup. "cursor-agent" -> "CURSOR_AGENT".
_harness_var_key() {
  local name="$1"
  echo "${name//-/_}" | tr '[:lower:]' '[:upper:]'
}

# harness_known <name> — exit 0 if registered, 1 otherwise.
harness_known() {
  local name="$1"
  local n
  for n in "${HARNESS_REGISTRY_NAMES[@]}"; do
    [[ "$n" == "$name" ]] && return 0
  done
  return 1
}

# harness_field <name> <field> — print the field value, or empty + exit 1 if unknown.
harness_field() {
  local name="$1"
  local field="$2"
  if ! harness_known "$name"; then
    echo "harness-registry: unknown harness '$name'" >&2
    return 1
  fi
  local field_uc
  field_uc="$(echo "$field" | tr '[:lower:]' '[:upper:]')"
  local var
  var="HARNESS_$(_harness_var_key "$name")_${field_uc}"
  # Validate the field name itself
  local f found=0
  for f in "${HARNESS_REGISTRY_FIELDS[@]}"; do
    [[ "$f" == "$field" ]] && found=1 && break
  done
  if (( found == 0 )); then
    echo "harness-registry: unknown field '$field' (valid: ${HARNESS_REGISTRY_FIELDS[*]})" >&2
    return 1
  fi
  printf '%s' "${!var-}"
}

# harness_binary_path <name> — print the path to the harness binary, exit 1 if missing.
# Resolves the first token of launch_cmd via `command -v`.
harness_binary_path() {
  local name="$1"
  local launch
  launch="$(harness_field "$name" launch_cmd)" || return 1
  if [[ -z "$launch" ]]; then
    # shell harness has empty launch by design.
    echo ""
    return 0
  fi
  # First whitespace-delimited token.
  local bin="${launch%% *}"
  command -v "$bin" 2>/dev/null
}

# harness_is_bare_shell_process <process_name> — exit 0 if the process name
# represents a bare login shell (i.e. an AI harness has exited and tmux fell
# back to the user's shell). Used by lane-health auto-restart and by
# normalize_audit_lanes' settle loop. Distinct from the shell harness's
# expected_process regex which intentionally also matches `watch|ssh`
# (legitimate shell-driven lanes like ops-top). The "fell back" set is
# narrower: only the interactive shells.
harness_is_bare_shell_process() {
  case "$1" in
    zsh|bash|sh|fish|"") return 0 ;;
    *) return 1 ;;
  esac
}

# harness_resolve_launch <name> [model] — print the full launch command with
# model selection applied (where applicable). Empty model -> launch_cmd as-is.
harness_resolve_launch() {
  local name="$1"
  local model="${2:-}"
  local launch
  launch="$(harness_field "$name" launch_cmd)" || return 1
  local model_flag
  model_flag="$(harness_field "$name" model_flag)" || return 1

  if [[ -z "$model" || "$model_flag" == "skip" || "$model_flag" == "config" ]]; then
    # No CLI model selection needed (skip = harness has no model concept;
    # config = harness reads model from its own config file at startup).
    printf '%s' "$launch"
    return 0
  fi

  case "$model_flag" in
    -m|--model)
      printf '%s %s %s' "$launch" "$model_flag" "$model"
      ;;
    --config)
      # codex pattern: codex --config model=<id>
      printf '%s %s model=%s' "$launch" "$model_flag" "$model"
      ;;
    *)
      # Unknown flag style — append literally.
      printf '%s %s %s' "$launch" "$model_flag" "$model"
      ;;
  esac
}

# harness_present <name> — exit 0 if the harness can spawn on this host:
# its binary resolves on PATH, or it needs no binary at all (shell lanes).
harness_present() {
  local name="$1"
  local launch
  launch="$(harness_field "$name" launch_cmd)" || return 1
  [[ -z "$launch" ]] && return 0
  [[ -n "$(harness_binary_path "$name" 2>/dev/null)" ]]
}

# harness_health <name> — print exactly one of ok|missing|unauthenticated|
# unhealthy on stdout (single-word, mirroring the lane-status contract style).
# Exit 0 only for ok. Health beyond the PATH check comes from the harness's
# declared health_probe command; with no probe declared ("" — the registry
# default), health degrades to the PATH check, which is today's behavior.
# A failing probe reads as unauthenticated when the harness declares an
# account/gateway auth_requirement, unhealthy otherwise.
harness_health() {
  local name="$1"
  if ! harness_known "$name"; then
    echo "harness-registry: unknown harness '$name'" >&2
    return 1
  fi
  if ! harness_present "$name"; then
    echo "missing"
    return 1
  fi
  local probe
  probe="$(harness_field "$name" health_probe)"
  if [[ -z "$probe" ]]; then
    echo "ok"
    return 0
  fi
  if bash -c "$probe" >/dev/null 2>&1; then
    echo "ok"
    return 0
  fi
  local auth
  auth="$(harness_field "$name" auth_requirement)"
  case "$auth" in
    account|gateway) echo "unauthenticated" ;;
    *) echo "unhealthy" ;;
  esac
  return 1
}

# ─── CLI mode ────────────────────────────────────────────────────────────
# When invoked directly (not sourced), expose a small CLI for inspection.

_harness_registry_cli() {
  local cmd="${1:-list}"
  shift || true
  case "$cmd" in
    list)
      printf '%s\n' "${HARNESS_REGISTRY_NAMES[@]}"
      ;;
    fields)
      local name="${1:?usage: fields <name>}"
      harness_known "$name" || { echo "unknown harness: $name" >&2; return 1; }
      local f
      for f in "${HARNESS_REGISTRY_FIELDS[@]}"; do
        printf '%-22s %s\n' "$f" "$(harness_field "$name" "$f")"
      done
      ;;
    field)
      local name="${1:?usage: field <name> <field>}"
      local field="${2:?usage: field <name> <field>}"
      harness_field "$name" "$field"
      printf '\n'
      ;;
    oneshot)
      # Sugar for `field <name> oneshot_template`: print the one-shot command
      # template ({prompt} placeholder intact). Empty output + exit 1 means
      # the harness cannot run one-shot, so callers can gate on the exit code.
      local name="${1:?usage: oneshot <name>}"
      local tpl
      tpl="$(harness_field "$name" oneshot_template)" || return 1
      if [[ -z "$tpl" ]]; then
        echo "harness-registry: harness '$name' has no one-shot mode" >&2
        return 1
      fi
      printf '%s\n' "$tpl"
      ;;
    probe)
      local name="${1:?usage: probe <name>}"
      local model="${2:-}"
      if ! harness_known "$name"; then
        echo "unknown harness: $name" >&2
        echo "known: ${HARNESS_REGISTRY_NAMES[*]}" >&2
        return 1
      fi
      local launch
      launch="$(harness_resolve_launch "$name" "$model")"
      local bin_path
      bin_path="$(harness_binary_path "$name" 2>/dev/null || true)"
      printf 'harness:           %s\n' "$name"
      printf 'launch_cmd:        %s\n' "$launch"
      if [[ -n "$model" ]]; then
        printf 'model:             %s\n' "$model"
      fi
      printf 'expected_process:  %s\n' "$(harness_field "$name" expected_process)"
      printf 'auto_approve_flag: %s\n' "$(harness_field "$name" auto_approve_flag)"
      printf 'paste_enter_delay: %s\n' "$(harness_field "$name" paste_enter_delay)"
      printf 'skill_dir:         %s\n' "$(harness_field "$name" skill_dir)"
      printf 'non_interactive:   %s\n' "$(harness_field "$name" non_interactive_flag)"
      printf 'oneshot_template:  %s\n' "$(harness_field "$name" oneshot_template)"
      if [[ -z "$launch" ]]; then
        printf 'binary:            (no launch — bare shell lane)\n'
        return 0
      fi
      if [[ -n "$bin_path" ]]; then
        printf 'binary:            %s\n' "$bin_path"
        return 0
      fi
      printf 'binary:            NOT FOUND on PATH\n' >&2
      return 1
      ;;
    roster)
      # Governance roster: every registered harness with its governance
      # fields and a `present` flag (binary resolves on PATH, or no binary
      # needed). Additive surface — probe/field/oneshot untouched.
      local json=0
      [[ "${1:-}" == "--json" ]] && json=1
      local n present
      if (( json )); then
        printf '{\n  "contract_version": 1,\n  "harnesses": [\n'
        local first=1
        for n in "${HARNESS_REGISTRY_NAMES[@]}"; do
          present=false
          harness_present "$n" && present=true
          (( first )) || printf ',\n'
          first=0
          # Registry values are static identifiers/short phrases (no quotes,
          # backslashes, or newlines — oneshot_template carries spaces and a
          # {prompt} token but no JSON metacharacters), so plain printf
          # interpolation is JSON-safe here. oneshot_template is exposed so the
          # engine gate can tell an agent lane (non-empty template) from a
          # non-agent shell/dashboard lane (empty) — F1 dispatch-target check.
          printf '    {"name": "%s", "present": %s, "capability_tags": "%s", "cost_tier": "%s", "autonomy_class": "%s", "auth_requirement": "%s", "health_probe": "%s", "drift_pins": "%s", "oneshot_template": "%s"}' \
            "$n" "$present" \
            "$(harness_field "$n" capability_tags)" \
            "$(harness_field "$n" cost_tier)" \
            "$(harness_field "$n" autonomy_class)" \
            "$(harness_field "$n" auth_requirement)" \
            "$(harness_field "$n" health_probe)" \
            "$(harness_field "$n" drift_pins)" \
            "$(harness_field "$n" oneshot_template)"
        done
        printf '\n  ]\n}\n'
      else
        for n in "${HARNESS_REGISTRY_NAMES[@]}"; do
          present=missing
          harness_present "$n" && present=present
          printf '%-14s %-8s %-22s %-7s %-11s %-8s %s\n' \
            "$n" "$present" \
            "$(harness_field "$n" capability_tags)" \
            "$(harness_field "$n" cost_tier)" \
            "$(harness_field "$n" autonomy_class)" \
            "$(harness_field "$n" auth_requirement)" \
            "$(harness_field "$n" drift_pins)"
        done
      fi
      ;;
    health)
      local name="${1:?usage: health <name>}"
      harness_health "$name"
      ;;
    -h|--help|help|"")
      cat <<'EOF'
harness-registry — per-harness contract lookup

Usage:
  harness-registry.sh list
  harness-registry.sh fields <name>
  harness-registry.sh field  <name> <field>
  harness-registry.sh oneshot <name>
  harness-registry.sh probe  <name> [model]
  harness-registry.sh roster [--json]
  harness-registry.sh health <name>

Known harnesses (see HARNESS_REGISTRY_NAMES): pi, claude, opencode, codex, cursor-agent, hermes, droid, forge, amp, openclaw, mprocs, shell
Known fields: launch_cmd model_flag expected_process auto_approve_flag paste_enter_delay skill_dir non_interactive_flag oneshot_template capability_tags cost_tier autonomy_class auth_requirement health_probe drift_pins model_failover isolation working_marker idle_marker

When sourced from another script, exposes:
  harness_known <name>
  harness_field <name> <field>
  harness_binary_path <name>
  harness_resolve_launch <name> [model]
EOF
      ;;
    *)
      echo "unknown command: $cmd" >&2
      _harness_registry_cli help >&2
      return 1
      ;;
  esac
}

# Detect direct execution (BASH_SOURCE[0] == $0) and run CLI.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _harness_registry_cli "$@"
fi
