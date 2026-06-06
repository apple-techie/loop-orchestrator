#!/usr/bin/env bash
# loop-dispatch.sh — project-agnostic dispatcher into loop-aware tmux lanes.
#
# --session is required (no hardcoded default) unless the caller is already
# inside a tmux session, in which case that session is used.

set -euo pipefail

SESSION_NAME=""
MODE="command"
PRESS_ENTER=1
VERIFY=0
# Delay (seconds) between a bracketed-paste and the Enter keystroke.
# Needed for Claude Code / Pi composers: paste-buffer emits the paste-end
# marker asynchronously, and if Enter arrives before the AI composer has
# finished processing the paste, Enter is captured as an in-prompt newline
# instead of a submit keystroke.
#
# 0.5s was the original default and covered short payloads, but Claude
# Code splits multi-kB pastes into multiple internal paste-chunks (each
# rendered as a separate `[Pasted text #N +K lines]` placeholder in the
# composer). Enter arriving 0.5s after the LAST paste-chunk's end-marker
# gets swallowed as newline-in-content on long payloads — content queues
# but never submits. 2.0s covers every payload size observed to date
# (including ~8kB orchestrator-loop dispatches). Still tunable via env
# for edge cases.
PASTE_ENTER_DELAY="${LOOP_DISPATCH_PASTE_DELAY:-${TMUX_DISPATCH_PASTE_DELAY:-2.0}}"
TARGET=""
PAYLOAD=""
WAIT_READY=0
READY_TIMEOUT="${LOOP_DISPATCH_READY_TIMEOUT:-20}"
LANE_NAME=""

# Resolve this script's real directory (symlink-aware) so --wait-ready can find
# the sibling loop-lane-status.sh even when invoked via a ~/.local/bin symlink.
_dispatch_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [[ -L "$src" ]]; do
    local dir; dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" && pwd
}
LANE_STATUS_SCRIPT="$(_dispatch_script_dir)/loop-lane-status.sh"

usage() {
  cat <<'EOF'
Usage:
  loop-dispatch.sh [options] <lane> <text>

Lanes:
  coord
  web
  infra
  validate-left
  validate-right
  ops-top
  ops-bottom
  docs
  <window-name>       any dynamic lane created by 'loop-tmux add-lane'

Options:
  --session <name>    tmux session name (required; falls back to $TMUX session
                      if you are already inside one)
  --window <name>     send to an explicit window.pane instead of a named lane
                      (e.g. --window "myproj:coord.0"); overrides <lane>
  --mode <mode>       command | text (default: command)
  --no-enter          Do not press Enter after sending text
  --verify            Capture pane after submit to confirm the composer cleared
  --wait-ready        Poll loop-lane-status until the lane is idle before
                      dispatching (named lane only) — avoids racing a
                      slow-booting TUI like Claude Code's welcome screen
  --ready-timeout <s> Max secs to wait for readiness (default: 20;
                      env: LOOP_DISPATCH_READY_TIMEOUT)
  -h, --help          Show this help

Env vars:
  LOOP_DISPATCH_PASTE_DELAY  Seconds to wait between paste-buffer and Enter
                             (default: 2.0). Raise if your Claude/Pi composer
                             is slow to process long prompts. Also honors
                             TMUX_DISPATCH_PASTE_DELAY for backward compat.

Modes:
  command  Send text and press Enter. Best for shell commands.
  text     Paste text via tmux buffer (no shell-escaping). Use for AI prompts.

Examples:
  loop-dispatch.sh --session my-app web "npm run typecheck"
  loop-dispatch.sh --session my-app --mode text infra "Audit the API for stale state."
  loop-dispatch.sh --session my-app --mode text --no-enter coord "Checkpoint note"
  loop-dispatch.sh --window my-app:validate.0 "npm run test"
EOF
}

WINDOW_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)      SESSION_NAME="$2"; shift 2 ;;
    --window)       WINDOW_OVERRIDE="$2"; shift 2 ;;
    --mode)         MODE="$2"; shift 2 ;;
    --no-enter)     PRESS_ENTER=0; shift ;;
    --verify)       VERIFY=1; shift ;;
    --wait-ready)   WAIT_READY=1; shift ;;
    --ready-timeout) READY_TIMEOUT="$2"; shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    --*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

# If no --session and we're inside tmux, use the current session.
if [[ -z "$SESSION_NAME" && -n "${TMUX:-}" ]]; then
  SESSION_NAME="$(tmux display-message -p '#S' 2>/dev/null || true)"
fi

if [[ -z "$WINDOW_OVERRIDE" && -z "$SESSION_NAME" ]]; then
  echo "error: --session <name> is required when not inside tmux" >&2
  usage >&2
  exit 1
fi

# Sort panes numerically by index (prefix the index, sort -n, strip it) so the
# order is 0,1,2,…,10 rather than the lexical .10-before-.2 of a plain sort.
first_pane_target() {
  local window="$1"
  tmux list-panes -t "$SESSION_NAME:$window" -F '#{pane_index} #{session_name}:#{window_name}.#{pane_index}' 2>/dev/null | sort -n | head -n 1 | cut -d' ' -f2-
}

nth_pane_target() {
  local window="$1"
  local n="$2"
  tmux list-panes -t "$SESSION_NAME:$window" -F '#{pane_index} #{session_name}:#{window_name}.#{pane_index}' 2>/dev/null | sort -n | sed -n "${n}p" | cut -d' ' -f2-
}

# Resolve a window NAME to its stable id (@N) via an exact match (window_id has
# no spaces, so the name is the read remainder). Print the id, or return 1.
window_id_for() {
  local name="$1" id wname
  while IFS=' ' read -r id wname; do
    [[ "$wname" == "$name" ]] && { printf '%s' "$id"; return 0; }
  done < <(tmux list-windows -t "$SESSION_NAME" -F '#{window_id} #{window_name}' 2>/dev/null)
  return 1
}

map_lane_to_target() {
  local target=""
  case "$1" in
    coord)            target="$(first_pane_target coord)" ;;
    web)              target="$(first_pane_target web)" ;;
    infra)            target="$(first_pane_target infra)" ;;
    validate-left)    target="$(nth_pane_target validate 1)" ;;
    validate-right)   target="$(nth_pane_target validate 2)" ;;
    ops-top)          target="$(nth_pane_target ops 1)" ;;
    ops-bottom)       target="$(nth_pane_target ops 2)" ;;
    docs)             target="$(first_pane_target docs)" ;;
    *)
      # Not a fixed lane — resolve as an add-lane (dynamic) window by name.
      local _wid
      if _wid="$(window_id_for "$1")"; then
        target="$(tmux list-panes -t "$_wid" -F '#{pane_index} #{pane_id}' 2>/dev/null | sort -n | head -n1 | cut -d' ' -f2-)"
      else
        echo "Unknown lane or window: $1" >&2
        echo "  (fixed lanes: coord web infra validate-left validate-right ops-top ops-bottom docs;" >&2
        echo "   or any add-lane window name in session '$SESSION_NAME')" >&2
        exit 1
      fi
      ;;
  esac
  if [[ -z "$target" ]]; then
    echo "Unable to resolve tmux target for lane: $1 in session $SESSION_NAME" >&2
    exit 1
  fi
  echo "$target"
}

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed or not on PATH" >&2
  exit 1
fi

if [[ -n "$WINDOW_OVERRIDE" ]]; then
  if [[ $# -lt 1 ]]; then
    usage >&2
    exit 1
  fi
  TARGET="$WINDOW_OVERRIDE"
  PAYLOAD="$*"
else
  if [[ $# -lt 2 ]]; then
    usage >&2
    exit 1
  fi
  # Check the session up front so a missing one reports clearly, rather than
  # the vaguer "unable to resolve lane target" from map_lane_to_target.
  if ! tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "tmux session '$SESSION_NAME' does not exist" >&2
    exit 1
  fi
  LANE_NAME="$1"
  TARGET="$(map_lane_to_target "$1")"
  shift
  PAYLOAD="$*"
fi

# Optionally wait for the target lane to be input-ready before dispatching, so a
# paste can't race a slow-booting TUI (e.g. Claude Code's welcome screen). Needs
# a named lane (not --window) and the sibling loop-lane-status; polls until it
# reports 'idle' or --ready-timeout secs elapse.
if [[ "$WAIT_READY" -eq 1 ]]; then
  if [[ -n "$WINDOW_OVERRIDE" ]]; then
    echo "warning: --wait-ready needs a named lane (ignored with --window)" >&2
  elif [[ ! -f "$LANE_STATUS_SCRIPT" ]]; then
    echo "warning: --wait-ready: loop-lane-status not found at $LANE_STATUS_SCRIPT (skipping)" >&2
  else
    _ready=0; _elapsed=0
    while (( _elapsed < READY_TIMEOUT )); do
      if [[ "$(bash "$LANE_STATUS_SCRIPT" "$SESSION_NAME" "$LANE_NAME" 2>/dev/null || echo unknown)" == "idle" ]]; then
        _ready=1; break
      fi
      sleep 1; _elapsed=$((_elapsed + 1))
    done
    [[ "$_ready" -eq 1 ]] || echo "warning: lane '$LANE_NAME' not confirmed ready within ${READY_TIMEOUT}s (dispatching anyway)" >&2
  fi
fi

case "$MODE" in
  command)
    # -l/-- sends the payload as literal text, so a command that happens to be
    # a tmux key name (e.g. "Enter", "Up", "C-c") is typed rather than fired as
    # a keystroke. Enter is sent separately in the PRESS_ENTER block below.
    tmux send-keys -t "$TARGET" -l -- "$PAYLOAD"
    ;;
  text)
    # Use a named, per-dispatch buffer. tmux's unnamed buffer is shared
    # process-wide, so back-to-back dispatches (e.g. fusion fanning out to
    # web/infra/validate/docs within ~1s) can race: dispatch B overwrites
    # the unnamed buffer before dispatch A's paste-buffer fires, and A
    # ends up pasting B's payload into its target pane. A named buffer
    # isolates each dispatch.
    #
    # The -p flag is CRITICAL. Without it, tmux streams the buffer as
    # individual keystrokes, so every `\n` in the payload is treated as
    # an Enter keypress. Pi / Claude Code composers then submit each
    # line as its own message and render the trailing chunks as
    # "Steering: …" mid-turn additions. With -p, tmux wraps the paste
    # in bracketed-paste markers (ESC [ 200 ~ … ESC [ 201 ~), telling
    # the composer "this is a paste, not keystrokes" — the whole
    # payload lands as a single insertion and the subsequent Enter
    # submits it as one message.
    # $$ already makes the buffer unique per dispatch process; append $RANDOM
    # instead of `date +%s%N` (BSD/macOS date has no %N). An EXIT trap deletes
    # the buffer even if paste-buffer fails under set -e, so it can't leak.
    # printf (not <<<) avoids appending a stray trailing newline to the paste.
    buf="loop-dispatch-$$-${RANDOM}"
    trap 'tmux delete-buffer -b "$buf" 2>/dev/null || true' EXIT
    printf '%s' "$PAYLOAD" | tmux load-buffer -b "$buf" -
    tmux paste-buffer -p -b "$buf" -t "$TARGET"
    ;;
  *)
    echo "Unknown mode: $MODE" >&2
    usage >&2
    exit 1
    ;;
esac

if [[ "$PRESS_ENTER" -eq 1 ]]; then
  # For text/paste mode, the composer needs a moment to finish processing
  # the bracketed-paste end marker before Enter can be treated as a submit.
  # Without this delay the Enter gets captured as a newline inside the prompt.
  if [[ "$MODE" == "text" ]]; then
    sleep "$PASTE_ENTER_DELAY"
  fi
  # Use the named Enter key — more reliable than C-m in Claude Code / Pi
  # composers, which distinguish the two in bracketed-paste contexts.
  tmux send-keys -t "$TARGET" Enter
fi

echo "Dispatched to $TARGET"

if [[ "$VERIFY" -eq 1 ]]; then
  sleep 1
  echo "--- pane tail after dispatch ---"
  # `|| true`: grep exits non-zero when the tail is all blank lines, which
  # under set -o pipefail would otherwise make a successful dispatch exit 1.
  tmux capture-pane -t "$TARGET" -p | grep -v '^$' | tail -5 || true
fi
