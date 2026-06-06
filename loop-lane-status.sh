#!/usr/bin/env bash
# loop-lane-status.sh — project-agnostic lane-readiness classifier for
# loop-orchestrator tmux sessions. Emits one of:
#   working | awaiting-approval | idle | errored | unknown
#
# Why: sequential orchestration modes (audit → review → critique → …)
# need to know when a lane is ready to be read from. Without this, a
# coordinator either races ahead (capturing TUI chrome as "output") or
# stalls silently behind an approval prompt.
#
# Usage:
#   loop-lane-status.sh <session> <lane>
#
# Lanes match the default layout established by loop-tmux.sh:
#   coord | web | infra | validate-left | validate-right |
#   ops-top | ops-bottom | docs
#
# Output: exactly one word on stdout.
# Exit 0 for any recognized status (including `unknown`).
# Non-zero only on invalid usage or lane-resolution failure.
set -euo pipefail

usage() {
  cat <<'EOF' >&2
Usage: loop-lane-status.sh <session> <lane>

Lanes: coord, web, infra, validate-left, validate-right, ops-top, ops-bottom, docs,
       or any dynamic lane created by 'loop-tmux add-lane' (addressed by window name).
EOF
  exit 2
}

[[ $# -eq 2 ]] || usage

SESSION_NAME="$1"
LANE="$2"

# Lane → tmux target resolution. Kept inline (not sourced) so this script
# stays usable on its own from anywhere — the
# orchestrator can invoke it without forcing callers to set up its full env.
# Sort panes numerically by index (prefix the index, sort -n, strip it) so the
# order is 0,1,2,…,10 rather than the lexical .10-before-.2 of a plain sort.
first_pane_target() {
  local window="$1"
  tmux list-panes -t "$SESSION_NAME:$window" -F '#{pane_index} #{session_name}:#{window_name}.#{pane_index}' 2>/dev/null | sort -n | head -n1 | cut -d' ' -f2-
}

nth_pane_target() {
  local window="$1"
  local n="$2"
  tmux list-panes -t "$SESSION_NAME:$window" -F '#{pane_index} #{session_name}:#{window_name}.#{pane_index}' 2>/dev/null | sort -n | sed -n "${n}p" | cut -d' ' -f2-
}

# Resolve a window NAME to its stable id (@N) via an exact match. Print id, or 1.
window_id_for() {
  local name="$1" id wname
  while IFS=' ' read -r id wname; do
    [[ "$wname" == "$name" ]] && { printf '%s' "$id"; return 0; }
  done < <(tmux list-windows -t "$SESSION_NAME" -F '#{window_id} #{window_name}' 2>/dev/null)
  return 1
}

resolve_lane() {
  case "$1" in
    coord)          first_pane_target coord ;;
    web)            first_pane_target web ;;
    infra)          first_pane_target infra ;;
    validate-left)  nth_pane_target validate 1 ;;
    validate-right) nth_pane_target validate 2 ;;
    ops-top)        nth_pane_target ops 1 ;;
    ops-bottom)     nth_pane_target ops 2 ;;
    docs)           first_pane_target docs ;;
    *)
      # Not a fixed lane — resolve as an add-lane (dynamic) window by name.
      local _wid
      if _wid="$(window_id_for "$1")"; then
        tmux list-panes -t "$_wid" -F '#{pane_index} #{pane_id}' 2>/dev/null | sort -n | head -n1 | cut -d' ' -f2-
      else
        echo "Unknown lane: $1" >&2
        exit 2
      fi
      ;;
  esac
}

TARGET="$(resolve_lane "$LANE")"
if [[ -z "$TARGET" ]]; then
  echo "Unable to resolve tmux target for lane '$LANE' in session '$SESSION_NAME'" >&2
  exit 3
fi

# Last 40 lines is enough to see both the prompt area (bottom ~5) and any
# active-work chrome (~15 above). We don't strip ANSI — tmux capture-pane
# already returns plaintext from a rendered buffer.
TAIL="$(tmux capture-pane -t "$TARGET" -p 2>/dev/null | tail -40)"
if [[ -z "$TAIL" ]]; then
  echo "unknown"
  exit 0
fi

# Errored and approval checks only look at the LAST ~5 lines so stale
# scrollback doesn't keep a recovered lane flagged forever. Working and
# idle can look across the full 40 because their markers are transient.
TAIL_BOTTOM="$(tail -5 <<<"$TAIL")"

# Rule order matters. Earlier rules win. This order is intentional:
#
#  1. awaiting-approval: a pane stuck on a y/n prompt is silently blocked —
#     downstream scripts need to fail-fast and tell the operator, not poll.
#  2. errored: a crashed lane should be visible before we confuse its
#     error output for "working" (the string "error" appears in normal
#     traces too, so we check specific exit / fatal / traceback markers).
#  3. working: any recognized active-work chrome means "wait, don't advance".
#  4. idle: no active markers and no errors and no prompts → ok to read from.
#  5. unknown: conservative fallback; coord should warn and let operator
#     decide rather than guessing.

# Rule 1 — awaiting approval. Patterns lifted from Claude Code's approval
# prompt + common bash (y/n) conventions. Only checked against the bottom
# slice because a y/n prompt scrolled out of view has already been
# dismissed or superseded.
if grep -qE 'Do you want to proceed\?|❯ [0-9]+\. Yes|Allow this tool|\(y/n\)|\(Y/n\)|\(y/N\)|Proceed\?|Esc to cancel · Tab to amend|waiting for your response' <<<"$TAIL_BOTTOM"; then
  echo "awaiting-approval"
  exit 0
fi

# Rule 2 — working. Active-work chrome in the BOTTOM slice only — a
# lane that "was working" 20 lines ago but has since returned to a
# prompt should read as idle, not working. Also guards against the
# coord lane echoing other lanes' tails into its own scrollback.
#
# IMPORTANT — past-tense markers are NOT working signals:
#   - `Churned for Xs` / `Cogitated for Xs` — the model has already returned.
#   - `active [1-9]` in Pi's footer — Pi does NOT decrement this when the
#     model finishes; only when the user/model explicitly closes the todo.
#     So a lane that finished its audit 5 min ago still shows `active 1`
#     with the last todo marked `[>]`. Trusting this caused the gate to
#     hang indefinitely on finished Pi lanes.
# The single reliable live-generation signal across both Pi and Claude Code
# is `esc to interrupt`, which is only rendered while generation is active.
# Verb patterns (`Working...`, `Thinking`, etc.) are kept as secondary
# signals but must co-occur with active spinner context, not past recap.
# Verb spinners in the bottom slice (Claude Code), PLUS a live braille spinner
# anywhere in the frame: Pi renders "⠙ Working…" just above its multi-line
# footer, out of the bottom slice, so a verb match alone misses a working Pi
# lane. Braille spinner frames only render while generation is active, and the
# `^` anchor ignores echoed "Web: ⠙ …" status lines, so matching them across
# the full tail is safe.
SPINNER_LINES="$(
  grep -E 'esc to interrupt|Working\.\.\.|Thinking\.\.\.|Orbiting|Planning\.\.\.|Searching\.\.\.|Envisioning|Analyzing\.\.\.|Inspecting\.\.\.|Running\.\.\.|Reading file|Reasoning\.\.\.|Computing\.\.\.|Generating\.\.\.|Loading\.\.\.' <<<"$TAIL_BOTTOM" || true
  grep -E '^[[:space:]]*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]' <<<"$TAIL" || true
)"
if [[ -n "$SPINNER_LINES" ]]; then
  # Reject matches that look like echoed status lines (e.g. "Web: ... ⠼
  # Working...") rather than live spinners — but check ONLY the spinner-matching
  # line(s), not the whole slice, so an unrelated "Validate: ..." narration line
  # elsewhere in the bottom slice can't suppress a genuinely live spinner.
  if ! grep -qE '^(Web|Infra|Docs|Validate|Ops|Coord):' <<<"$SPINNER_LINES"; then
    echo "working"
    exit 0
  fi
fi

# Rule 3 — errored. Pattern set biased toward definitive failure markers,
# not the bare word "error" which shows up in normal audit content. Only
# looks at the bottom slice so stale errors from 5 minutes ago don't keep
# flagging a lane that has since returned to a prompt.
if grep -qE '^(Error|FATAL|Fatal|Failed):|command not found|exit code [1-9]|Traceback \(most recent|Unhandled exception|Permission denied|tmux:.*session not found' <<<"$TAIL_BOTTOM"; then
  echo "errored"
  exit 0
fi

# Rule 4 — idle. Positive markers: a shell prompt sitting empty, an
# agent TUI rendered with its home footer visible, or Claude Code's
# `❯ ` prompt with its "accept edits" status line. Any of these plus
# absence of approval/working/errored means the lane is safe to read from.
# TUI home/status chrome can appear anywhere in the frame, but the bare-prompt
# patterns must be anchored and restricted to the bottom slice — otherwise a
# content line that merely ends in '$' or '%' (e.g. "coverage 87%", "CPU 100%")
# trips idle and a coordinator reads a still-working pane.
# Some harnesses render a product-specific home/footer string when idle at their
# launch screen. That string varies per harness, so set it via the
# LOOP_LANE_IDLE_HOME_PATTERN env var (an extended-regex alternation) rather than
# hardcoding it. This is safe because a *working* lane is already caught by the
# braille-spinner check in Rule 2 above, which runs first — so reaching here with
# a home footer means no live spinner, i.e. genuinely idle.
idle_home_chrome='accept edits on|bypass permissions on'
[[ -n "${LOOP_LANE_IDLE_HOME_PATTERN:-}" ]] && idle_home_chrome+="|${LOOP_LANE_IDLE_HOME_PATTERN}"
if grep -qE "$idle_home_chrome" <<<"$TAIL" \
   || grep -qE '^[[:space:]]*[$%❯][[:space:]]*$' <<<"$TAIL_BOTTOM"; then
  echo "idle"
  exit 0
fi

# Rule 5 — unknown. Don't guess. Coord should warn the operator.
echo "unknown"
exit 0
