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
INTERRUPT=0
LANE_NAME=""
# Auto-context-reset before a FRESH claude dispatch. Default on; opt out with
# --no-clear or LOOP_DISPATCH_NO_CLEAR=1. See the clear block below for why.
NO_CLEAR="${LOOP_DISPATCH_NO_CLEAR:-0}"
# Max secs to wait for the lane to re-settle after a /clear before proceeding
# anyway (SAFETY: never hang a dispatch on a clear that can't be confirmed).
CLEAR_TIMEOUT="${LOOP_DISPATCH_CLEAR_TIMEOUT:-8}"

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
  --interrupt         Send Escape to the target first (cancels in-flight
                      generation in Claude/Pi composers), wait 1s, then
                      dispatch — for steering a busy lane onto a new course
                      instead of appending mid-turn guidance
  --no-clear          Disable the auto-/clear context reset that otherwise runs
                      before a FRESH dispatch into an idle claude lane
                      (env: LOOP_DISPATCH_NO_CLEAR=1)
  -h, --help          Show this help

Env vars:
  LOOP_DISPATCH_PASTE_DELAY  Seconds to wait between paste-buffer and Enter
                             (default: 2.0). Raise if your Claude/Pi composer
                             is slow to process long prompts. Also honors
                             TMUX_DISPATCH_PASTE_DELAY for backward compat.
  LOOP_DISPATCH_NO_CLEAR     Set to 1 to disable the auto-/clear context reset
                             before a fresh claude dispatch (same as --no-clear).
  LOOP_DISPATCH_CLEAR_TIMEOUT Max secs to wait for the lane to re-settle after a
                             /clear before dispatching anyway (default: 8).

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
    --interrupt)    INTERRUPT=1; shift ;;
    --no-clear)     NO_CLEAR=1; shift ;;
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

# Auto-context-reset (loop improvement #36): before pasting a FRESH task into a
# claude lane, send `/clear` so the lane never accumulates context across a
# session. Observed 3×: after ~5-6 builds in one session a claude composer lags
# so badly a dispatched prompt PASTES but the Enter never registers — the prompt
# sits unsent and repeated sends stack pastes. Each task dispatch is fully
# self-contained (whole prompt + task file), so resetting the lane's context
# before a fresh task loses nothing. Gated on ALL of:
#   (a) harness is claude — `/clear` is claude-specific; NEVER send it to a
#       shell/other lane (it would land as literal text or a bad command);
#   (b) NOT --interrupt — steering a busy lane must never wipe its context;
#   (c) the lane is idle/ready — never clear a working lane mid-task;
#   (d) not opted out (--no-clear / LOOP_DISPATCH_NO_CLEAR).
# SAFETY: if the post-clear settle can't be confirmed within CLEAR_TIMEOUT, we
# proceed with the dispatch anyway — a clear must never hang a dispatch. The
# clear itself is one paste+Enter, so the at-most-once payload delivery below is
# unchanged (we never re-send the task).
# _lane_status_for_target — classify the resolved TARGET by capturing its pane
# and running it through loop-lane-status's --classify-stdin seam (the same rule
# chain the fleet sweep uses). Works for both named lanes and the --window
# direct form since it operates on the already-resolved tmux target. Echoes one
# status word (idle|working|awaiting-approval|errored|unknown), or `unknown` if
# anything fails — the caller treats non-idle conservatively (skips the clear).
_lane_status_for_target() {
  local tail
  tail="$(tmux capture-pane -t "$TARGET" -p 2>/dev/null || true)"
  [[ -z "$tail" ]] && { echo unknown; return; }
  printf '%s' "$tail" | bash "$LANE_STATUS_SCRIPT" --classify-stdin claude 2>/dev/null || echo unknown
}

if [[ "$NO_CLEAR" -ne 1 && "$INTERRUPT" -ne 1 && -f "$LANE_STATUS_SCRIPT" ]]; then
  _lane_harness="$(tmux show-options -wqv -t "$TARGET" @loop_lane_harness 2>/dev/null || true)"
  if [[ "$_lane_harness" == "claude" ]]; then
    # Only clear a confirmed-idle lane — never wipe a working lane mid-task.
    if [[ "$(_lane_status_for_target)" == "idle" ]]; then
      # Send /clear as keystrokes (it's a short, fixed slash-command — no paste
      # buffer needed), then Enter to submit.
      tmux send-keys -t "$TARGET" -l -- "/clear"
      tmux send-keys -t "$TARGET" Enter
      # Give the clear a moment to take effect before re-probing — claude's idle
      # home-chrome ("accept edits on") persists across the clear, so an instant
      # re-probe could read the PRE-clear chrome and race the redraw.
      sleep 1
      # Wait for the lane to re-settle to idle (the cleared composer home-chrome)
      # before pasting the task, so the paste can't race the clear's redraw.
      # SAFETY: bounded by CLEAR_TIMEOUT — on timeout we fall through and
      # dispatch anyway rather than hang.
      _c_elapsed=0
      while (( _c_elapsed < CLEAR_TIMEOUT )); do
        [[ "$(_lane_status_for_target)" == "idle" ]] && break
        sleep 1; _c_elapsed=$((_c_elapsed + 1))
      done
    fi
  fi
fi

# Steering interrupt: cancel any in-flight generation before the payload
# lands. Escape (not C-c) — Claude/Pi composers treat Escape as "stop
# generating, keep the session"; C-c can kill the harness process. The 1s
# settle lets the TUI return to its composer before the paste arrives.
if [[ "$INTERRUPT" -eq 1 ]]; then
  tmux send-keys -t "$TARGET" Escape
  sleep 1
fi

case "$MODE" in
  command)
    # Run the payload via a temp SCRIPT FILE rather than typing it into the
    # shell as literal keystrokes. Pasting a payload directly is fragile: an
    # unbalanced quote or apostrophe (common when a brain composes a quoted
    # `bash -lc '...'` one-liner, or mixes prose with a command) leaves the
    # shell stuck at a `quote>` continuation and the command never runs — a
    # SILENT hang the caller can't distinguish from "still working". Writing
    # the payload to a file and typing only a fixed `bash <file>` line makes
    # the keystrokes immune to the payload's own content; the mktemp path has
    # no shell-special chars, so the typed line is always safe.
    cmd_file="$(mktemp "${TMPDIR:-/tmp}/loop-dispatch-cmd.XXXXXX")"
    printf '%s\n' "$PAYLOAD" > "$cmd_file"
    tmux send-keys -t "$TARGET" -l -- "bash '$cmd_file'; rm -f '$cmd_file'"
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
