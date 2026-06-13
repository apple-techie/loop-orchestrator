#!/usr/bin/env bash
# loop-checkpoint.sh — stateless coordinator boot from the compiled checkpoint.
#
# Each checkpoint cycle is a FRESH coord invocation booted from a
# constant-size compiled context: a fixed header + ops-wiki/checkpoint.md +
# ops-wiki/index.md + the pending-mailbox summary from loop-wiki-pending.sh.
# Coord never carries prior transcript; its memory lives on disk (see
# AGENTS.md "Coordinator contract"). Prompt size is independent of mailbox/
# processed history and session age.

set -euo pipefail

PROJECT_ROOT=""
HEADER_FILE=""
MODE=""
LANE="coord"
TOKEN_WARN_LIMIT=24000
# Hard ceiling (T0022): above this, refuse to emit the prompt (exit non-zero)
# rather than feed a runaway checkpoint to coord. Configurable via
# --token-ceiling or $LOOP_CHECKPOINT_TOKEN_CEILING; defaults to 2x the warn
# budget so it backstops genuine runaway while in-band drift only warns. The
# decision-log rotation (wiki.py) keeps the steady state well under this.
TOKEN_HARD_LIMIT="${LOOP_CHECKPOINT_TOKEN_CEILING:-48000}"

# Resolve this script's real directory (symlink-aware) so the sibling
# loop-wiki-pending.sh and the default project root (this script's
# parent-of-parent directory) resolve even when invoked via a ~/.local/bin
# symlink or from anywhere.
_checkpoint_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [[ -L "$src" ]]; do
    local dir; dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" && pwd
}
SCRIPT_DIR="$(_checkpoint_script_dir)"

usage() {
  cat <<'EOF'
Usage:
  loop-checkpoint.sh [options] --print
  loop-checkpoint.sh [options] --dispatch [lane]

Assembles the coordinator checkpoint prompt from, in order: a fixed header,
ops-wiki/checkpoint.md, ops-wiki/index.md, and the pending-mailbox summary
(scripts/loop-wiki-pending.sh). Size is constant regardless of session age.

Modes (exactly one required):
  --print               Emit the assembled prompt to stdout
  --dispatch [lane]     Send the prompt into a tmux lane via
                        loop-dispatch --mode text --wait-ready
                        (default lane: coord)

Options:
  --header-file <path>  Replace the fixed header with the file's contents
                        (e.g. a side-effect-free header for an external
                        engine that wants a decision block emitted instead
                        of checkpoint.md writes and dispatches)
  --project-root <path> Repo root containing ops-wiki/ and .loop/
                        default: this script's parent-of-parent directory
  --token-ceiling <n>   Hard ceiling on the bytes/4 token estimate; over it the
                        script refuses to emit (exit 3). Default 48000, or
                        $LOOP_CHECKPOINT_TOKEN_CEILING.
  -h, --help            Show this help

The assembled prompt's byte count and approximate token count (bytes/4) are
printed to STDERR so size drift stays visible without polluting --print
output; a warning is emitted past 24000 tokens and the prompt is REFUSED
(exit 3) past the hard ceiling (--token-ceiling, default 48000).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --print)
      if [[ -n "$MODE" ]]; then
        echo "error: --print conflicts with --$MODE" >&2
        exit 1
      fi
      MODE="print"; shift
      ;;
    --dispatch)
      if [[ -n "$MODE" ]]; then
        echo "error: --dispatch conflicts with --$MODE" >&2
        exit 1
      fi
      MODE="dispatch"; shift
      # Optional positional lane right after --dispatch (default: coord).
      if [[ $# -gt 0 && "$1" != -* ]]; then
        LANE="$1"; shift
      fi
      ;;
    --header-file)  HEADER_FILE="$2"; shift 2 ;;
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    --token-ceiling) TOKEN_HARD_LIMIT="$2"; shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    --*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      echo "Unexpected argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$MODE" ]]; then
  echo "error: one of --print or --dispatch is required" >&2
  usage >&2
  exit 1
fi

if [[ -z "$PROJECT_ROOT" ]]; then
  PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

CHECKPOINT_FILE="$PROJECT_ROOT/ops-wiki/checkpoint.md"
INDEX_FILE="$PROJECT_ROOT/ops-wiki/index.md"
PENDING_SCRIPT="$SCRIPT_DIR/loop-wiki-pending.sh"

if [[ ! -f "$CHECKPOINT_FILE" ]]; then
  echo "error: missing $CHECKPOINT_FILE" >&2
  exit 1
fi
if [[ ! -f "$INDEX_FILE" ]]; then
  echo "error: missing $INDEX_FILE" >&2
  exit 1
fi
if [[ ! -x "$PENDING_SCRIPT" ]]; then
  echo "error: missing sibling script $PENDING_SCRIPT" >&2
  exit 1
fi

default_header() {
  cat <<'EOF'
You are the coordinator for one checkpoint cycle. Read the compiled state
below. Drill into specific ops-wiki pages by path only if needed. Decide the
single next step per lane or stop. Run the critique: what is unproven, what
downstream state is only inferred, what would falsify confidence fastest.
Write your decision and reasoning into the coord section of
ops-wiki/checkpoint.md and into the relevant loop page. Do not implement;
dispatch.
EOF
}

if [[ -n "$HEADER_FILE" ]]; then
  if [[ ! -f "$HEADER_FILE" ]]; then
    echo "error: --header-file not found: $HEADER_FILE" >&2
    exit 1
  fi
  HEADER="$(cat "$HEADER_FILE")"
else
  HEADER="$(default_header)"
fi

PENDING_OUTPUT="$("$PENDING_SCRIPT" --project-root "$PROJECT_ROOT")"

PROMPT="$HEADER

--- ops-wiki/checkpoint.md ---
$(cat "$CHECKPOINT_FILE")

--- ops-wiki/index.md ---
$(cat "$INDEX_FILE")

--- pending mailbox summary (scripts/loop-wiki-pending.sh) ---
$PENDING_OUTPUT"

# Size report goes to stderr so --print stdout stays a clean prompt.
BYTE_COUNT="$(printf '%s' "$PROMPT" | wc -c | tr -d ' ')"
TOKEN_COUNT=$(( BYTE_COUNT / 4 ))
echo "prompt size: ${BYTE_COUNT} bytes (~${TOKEN_COUNT} tokens, bytes/4)" >&2
if [[ "$TOKEN_COUNT" -gt "$TOKEN_WARN_LIMIT" ]]; then
  echo "warning: assembled prompt ~${TOKEN_COUNT} tokens exceeds ${TOKEN_WARN_LIMIT} — compiled state is drifting; trim checkpoint.md/index.md" >&2
fi
# Hard gate (T0022): a prompt over the ceiling is refused, not emitted — a
# runaway checkpoint must fail loudly, not silently feed coord a bloated boot
# context. Configurable via --token-ceiling / LOOP_CHECKPOINT_TOKEN_CEILING.
if [[ "$TOKEN_COUNT" -gt "$TOKEN_HARD_LIMIT" ]]; then
  echo "error: assembled prompt ~${TOKEN_COUNT} tokens exceeds the hard ceiling ${TOKEN_HARD_LIMIT} — refusing to emit a runaway checkpoint. Rotate ops-wiki/checkpoint.md (decision-log retention) or raise --token-ceiling / LOOP_CHECKPOINT_TOKEN_CEILING." >&2
  exit 3
fi

# Resolve loop-dispatch: prefer the installed symlink on PATH (make install),
# fall back to the repo-root sibling next to this script's parent directory.
resolve_dispatch() {
  if command -v loop-dispatch >/dev/null 2>&1; then
    command -v loop-dispatch
    return 0
  fi
  local fallback
  fallback="$(cd "$SCRIPT_DIR/.." && pwd)/loop-dispatch.sh"
  if [[ -x "$fallback" ]]; then
    printf '%s\n' "$fallback"
    return 0
  fi
  return 1
}

case "$MODE" in
  print)
    printf '%s\n' "$PROMPT"
    ;;
  dispatch)
    # Dispatch path: the assembled prompt is handed to loop-dispatch as a
    # single text-mode payload:
    #   loop-dispatch --mode text --wait-ready <lane> "<prompt>"
    # loop-dispatch resolves the tmux session itself ($TMUX when inside a
    # session, --session otherwise) and pastes via tmux buffer; --wait-ready
    # polls loop-lane-status until the lane is idle before pasting, so a
    # slow-booting coord TUI is not raced. Without a live tmux session
    # loop-dispatch fails fast with "--session <name> is required" before
    # touching any pane — this script adds no tmux handling of its own.
    DISPATCH_BIN="$(resolve_dispatch)" || {
      echo "error: loop-dispatch not found on PATH or at $(cd "$SCRIPT_DIR/.." && pwd)/loop-dispatch.sh" >&2
      exit 1
    }
    "$DISPATCH_BIN" --mode text --wait-ready "$LANE" "$PROMPT"
    ;;
esac
