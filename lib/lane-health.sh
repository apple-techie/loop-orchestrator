#!/usr/bin/env bash
# lib/lane-health.sh — AI-lane process liveness checks + auto-restart.
#
# Designed to be invoked as a long-running watchdog by loop-tmux.sh
# (--auto-restart). Polls each AI-lane pane's $pane_current_command;
# if the AI process (pi/claude/etc.) has exited and the pane fell back
# to a bare shell (zsh/bash/sh), re-runs the original launch command.
# Beyond the lanes passed via --web-cmd/etc., it discovers dynamically-added
# (add-lane) lanes each cycle via their @loop_lane window tags and watches the
# AI ones the same way.
#
# Restart events are appended (one-line JSON) to:
#   <state-dir>/lane-restarts.jsonl
# so list-sessions / observe modes can report on flaps.
#
# Usage (called by loop-tmux.sh, not directly):
#   ./lib/lane-health.sh --session my-app \
#       --interval 30 \
#       --state-dir /path/to/.loop/sessions/my-app \
#       --web-cmd 'pi' --infra-cmd 'claude --dangerously-skip-permissions' \
#       --docs-cmd 'pi'
#
# Standalone:
#   ./lib/lane-health.sh --session my-app --probe-only
#       (prints status table + exits without restarting)

set -o pipefail

# Source harness registry to consult harness_is_bare_shell_process. This
# is the single source of truth for "what process name means the AI
# harness has exited". Sourcing without `set -u` here so unset env vars
# in the registry don't blow up.
_LH_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=harness-registry.sh
source "$_LH_LIB_DIR/harness-registry.sh"

SESSION_NAME=""
INTERVAL=30
STATE_DIR=""
PROBE_ONLY=0
# Consecutive failed restarts per lane before the watchdog gives up on it.
# Prevents an un-bootable lane (bad model id, missing binary) from being
# relaunched forever. Override with LANE_HEALTH_MAX_RESTARTS.
MAX_RESTARTS="${LANE_HEALTH_MAX_RESTARTS:-3}"
# Startup grace before the first probe — long enough for a slow AI TUI (Claude
# can take ~4s) and aligned with loop-tmux's --boot-check default (8s) so the
# first pass doesn't spuriously "restart" a still-booting lane. Override with
# LANE_HEALTH_GRACE.
GRACE="${LANE_HEALTH_GRACE:-8}"
WEB_CMD=""
INFRA_CMD=""
DOCS_CMD=""
VALIDATE_LEFT_CMD=""
VALIDATE_RIGHT_CMD=""

usage() {
  sed -n '2,30p' "$0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)            SESSION_NAME="$2"; shift 2 ;;
    --interval)           INTERVAL="$2"; shift 2 ;;
    --state-dir)          STATE_DIR="$2"; shift 2 ;;
    --web-cmd)            WEB_CMD="$2"; shift 2 ;;
    --infra-cmd)          INFRA_CMD="$2"; shift 2 ;;
    --docs-cmd)           DOCS_CMD="$2"; shift 2 ;;
    --validate-left-cmd)  VALIDATE_LEFT_CMD="$2"; shift 2 ;;
    --validate-right-cmd) VALIDATE_RIGHT_CMD="$2"; shift 2 ;;
    --probe-only)         PROBE_ONLY=1; shift ;;
    -h|--help)            usage; exit 0 ;;
    *)
      echo "lane-health: unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$SESSION_NAME" ]]; then
  echo "lane-health: --session required" >&2
  exit 2
fi

if [[ -z "$STATE_DIR" ]]; then
  STATE_DIR="/tmp/lane-health-$SESSION_NAME"
fi
mkdir -p "$STATE_DIR"
RESTARTS_LOG="$STATE_DIR/lane-restarts.jsonl"

# A "shell" command name means the AI exited and fell back to the
# user's login shell. Delegates to the harness registry — kept as a
# named function so call-sites read clearly.
is_shell() {
  harness_is_bare_shell_process "$1"
}

# Per-lane consecutive-failure tracking (bash-3.2 safe variable indirection).
# A lane is "given up" once it exceeds MAX_RESTARTS without coming back live;
# coming back live on its own resets the budget.
_lh_key() {
  # Sanitize ALL non-alphanumerics (not just hyphens) so dynamic-lane window
  # names are always safe as printf -v variable keys.
  echo "${1//[^a-zA-Z0-9]/_}" | tr '[:lower:]' '[:upper:]'
}
_lh_fails() {
  local v="_LH_FAILS_$(_lh_key "$1")"
  printf '%s' "${!v:-0}"
}
_lh_incr_fail() {
  local v="_LH_FAILS_$(_lh_key "$1")"
  printf -v "$v" '%s' "$(( ${!v:-0} + 1 ))"
}
_lh_gaveup() {
  local v="_LH_GAVEUP_$(_lh_key "$1")"
  [[ "${!v:-0}" == "1" ]]
}
_lh_set_gaveup() {
  local v="_LH_GAVEUP_$(_lh_key "$1")"
  printf -v "$v" '%s' "1"
}
_lh_reset() {
  local k
  k="$(_lh_key "$1")"
  unset "_LH_FAILS_$k" "_LH_GAVEUP_$k"
}

# Escape a string for safe embedding inside a JSON double-quoted value so a
# cmd/target containing a quote or backslash can't produce malformed JSONL.
_lh_json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"   # backslash -> \\  (must run first)
  s="${s//\"/\\\"}"   # double quote -> \"
  printf '%s' "$s"
}

# Append a one-line JSON lifecycle event (e.g. giving-up) to the restarts log.
_lh_log_event() {
  local event="$1" lane="$2" target="$3" cmd="$4" now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"timestamp":"%s","event":"%s","session":"%s","lane":"%s","target":"%s","cmd":"%s"}\n' \
    "$now" "$(_lh_json_escape "$event")" "$(_lh_json_escape "$SESSION_NAME")" \
    "$(_lh_json_escape "$lane")" "$(_lh_json_escape "$target")" "$(_lh_json_escape "$cmd")" >> "$RESTARTS_LOG"
}

# True only when a lane's command launches an AI harness (pi/claude/…), so a
# bare-shell sighting means the harness died and should be resurrected. A
# shell/watcher lane (cmd is a script path, watch, tail, mprocs, …) is
# *legitimately* a shell — restarting it would clobber operator input or
# re-run a healthy watcher — so it is left alone. Registry-driven to avoid
# duplicating the harness list.
_lh_is_ai_cmd() {
  local first="${1%% *}"
  [[ -z "$first" ]] && return 1
  local h launch
  for h in "${HARNESS_REGISTRY_NAMES[@]}"; do
    case "$h" in shell|mprocs) continue ;; esac
    launch="$(harness_field "$h" launch_cmd)"
    # Compare against the launch_cmd's first token so multi-token launches
    # (e.g. "hermes chat --tui", "openclaw tui") still match.
    [[ -n "$launch" && "$first" == "${launch%% *}" ]] && return 0
  done
  return 1
}

# Sleep up to $INTERVAL seconds in short steps, returning non-zero as soon as
# the tmux session disappears so a killed session is noticed within a couple
# seconds instead of after a full interval.
_lh_wait() {
  # Non-integer interval: fall back to a plain sleep (no early exit).
  if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]]; then
    sleep "$INTERVAL"
    tmux has-session -t "$SESSION_NAME" 2>/dev/null
    return
  fi
  local remaining="$INTERVAL" step=2
  while (( remaining > 0 )); do
    tmux has-session -t "$SESSION_NAME" 2>/dev/null || return 1
    (( remaining < step )) && step="$remaining"
    sleep "$step"
    remaining=$((remaining - step))
  done
  return 0
}

nth_pane() {
  local window="$1" n="$2"
  # Sort numerically by pane index so order is 0,1,…,10 (not lexical .10<.2).
  tmux list-panes -t "$SESSION_NAME:$window" -F '#{pane_index} #{session_name}:#{window_name}.#{pane_index}' 2>/dev/null \
    | sort -n | sed -n "${n}p" | cut -d' ' -f2-
}

pane_cmd() {
  tmux display-message -p -t "$1" '#{pane_current_command}' 2>/dev/null || echo ""
}

restart_pane() {
  local lane="$1" target="$2" cmd="$3"
  if [[ -z "$cmd" || -z "$target" ]]; then return 0; fi
  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"timestamp":"%s","session":"%s","lane":"%s","target":"%s","cmd":"%s"}\n' \
    "$now" "$(_lh_json_escape "$SESSION_NAME")" "$(_lh_json_escape "$lane")" \
    "$(_lh_json_escape "$target")" "$(_lh_json_escape "$cmd")" >> "$RESTARTS_LOG"
  # Cancel anything in the input + send fresh launch
  tmux send-keys -t "$target" C-c 2>/dev/null || true
  sleep 0.5
  tmux send-keys -t "$target" "$cmd" C-m 2>/dev/null || true
  echo "[lane-health] restarted lane=$lane cmd='$cmd' target=$target"
}

probe_once() {
  # Returns 0 if all configured lanes are live, 1 if any restarts happened.
  # Use '|' as the field separator since tmux pane targets contain ':'.
  local restarts=0

  # Fixed lanes from the launch flags, plus any dynamically-added (add-lane)
  # lanes discovered live via their @loop_lane window tags — so a lane the
  # coordinator spun up at runtime is watched just like the base lanes. The
  # body's is_shell + _lh_is_ai_cmd checks leave shell/watcher lanes alone.
  local -a entries=(
    "web|$(nth_pane web 1)|$WEB_CMD"
    "infra|$(nth_pane infra 1)|$INFRA_CMD"
    "docs|$(nth_pane docs 1)|$DOCS_CMD"
    "validate-left|$(nth_pane validate 1)|$VALIDATE_LEFT_CMD"
    "validate-right|$(nth_pane validate 2)|$VALIDATE_RIGHT_CMD"
  )
  local _wid _wname _dyn _dcmd _dpane
  while IFS=' ' read -r _wid _wname; do
    [[ -z "$_wid" ]] && continue
    _dyn="$(tmux show-options -wqv -t "$_wid" @loop_lane 2>/dev/null || true)"
    [[ "$_dyn" == "1" ]] || continue
    _dcmd="$(tmux show-options -wqv -t "$_wid" @loop_lane_cmd 2>/dev/null || true)"
    [[ -z "$_dcmd" ]] && continue
    _dpane="$(tmux list-panes -t "$_wid" -F '#{pane_index} #{pane_id}' 2>/dev/null | sort -n | head -n1 | cut -d' ' -f2-)"
    [[ -z "$_dpane" ]] && continue
    entries+=("$_wname|$_dpane|$_dcmd")
  done < <(tmux list-windows -t "$SESSION_NAME" -F '#{window_id} #{window_name}' 2>/dev/null)

  local entry
  for entry in "${entries[@]}"; do
    local lane="${entry%%|*}"
    local rest="${entry#*|}"
    local target="${rest%%|*}"
    local cmd="${rest#*|}"
    if [[ -z "$cmd" || -z "$target" ]]; then
      continue
    fi
    local current
    current="$(pane_cmd "$target")"
    if is_shell "$current"; then
      # Lane fell back to a bare shell. Only resurrect AI-harness lanes — a
      # shell/watcher lane being a shell is its normal state, and C-c'ing it
      # would clobber operator input or kill a healthy watcher.
      if ! _lh_is_ai_cmd "$cmd"; then
        if [[ "$PROBE_ONLY" = "1" ]]; then
          echo "[lane-health] SHELL lane=$lane target=$target current='$current' (non-AI lane — left alone)"
        fi
        continue
      fi
      # Lane fell back to a bare shell — the AI process exited.
      if [[ "$PROBE_ONLY" = "1" ]]; then
        echo "[lane-health] DOWN  lane=$lane target=$target current='$current' expected_cmd='$cmd'"
        continue
      fi
      # Already abandoned this lane — don't restart-storm or spam the log.
      if _lh_gaveup "$lane"; then
        continue
      fi
      _lh_incr_fail "$lane"
      local fails
      fails="$(_lh_fails "$lane")"
      if (( fails > MAX_RESTARTS )); then
        _lh_set_gaveup "$lane"
        _lh_log_event "giving-up" "$lane" "$target" "$cmd"
        echo "[lane-health] GAVE UP lane=$lane after $MAX_RESTARTS restarts target=$target cmd='$cmd' — not restarting until it recovers" >&2
        continue
      fi
      restart_pane "$lane" "$target" "$cmd"
      restarts=$((restarts+1))
    else
      # Lane is live — reset its failure budget so a future death gets a fresh
      # set of restart attempts.
      _lh_reset "$lane"
      if [[ "$PROBE_ONLY" = "1" ]]; then
        echo "[lane-health] LIVE  lane=$lane target=$target current='$current'"
      fi
    fi
  done

  return $restarts
}

if [[ "$PROBE_ONLY" = "1" ]]; then
  probe_once || true
  exit 0
fi

# Watchdog loop. Record our pid so loop-tmux can avoid stacking a second
# watchdog and the operator has a documented stop handle; clear it on exit.
PIDFILE="$STATE_DIR/lane-health.pid"
echo "$$" > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT
trap 'echo "[lane-health] watchdog stopped"; exit 0' INT TERM

# Startup grace: panes opened by loop-tmux.sh take a few seconds to launch
# their AI process. Without this delay, the first probe finds them as bare
# shells and re-issues the launch command — a spurious restart. $GRACE (default
# 8s) covers a slow Claude TUI and matches loop-tmux's --boot-check default.
echo "[lane-health] watchdog started session=$SESSION_NAME interval=${INTERVAL}s log=$RESTARTS_LOG (grace=${GRACE}s, pidfile=$PIDFILE)"
sleep "$GRACE"

while true; do
  if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "[lane-health] tmux session disappeared — exiting"
    exit 0
  fi
  probe_once || true
  if ! _lh_wait; then
    echo "[lane-health] tmux session disappeared — exiting"
    exit 0
  fi
done
