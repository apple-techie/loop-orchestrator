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
# The coord-decisions partition marker (CONTRACT.md / wiki.py). Everything above
# it in checkpoint.md is the compiled region (T0021 projects it from the ledger);
# the marker line and everything below are coordinator-owned, preserved verbatim.
CHECKPOINT_MARKER="<!-- coord-decisions -->"

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

# Default project root: prefer the git working tree we are run from (correct in a
# worktree), then $PWD when it holds ops-wiki/ or .loop/, else the legacy
# install-relative parent-of-parent. An explicit --project-root always overrides;
# the engine always passes it, so this default only affects bare invocations.
_checkpoint_default_root() {
  local top
  if top="$(git rev-parse --show-toplevel 2>/dev/null)" && [[ -n "$top" ]]; then
    printf '%s\n' "$top"; return 0
  fi
  if [[ -d "$PWD/ops-wiki" || -d "$PWD/.loop" ]]; then printf '%s\n' "$PWD"; return 0; fi
  (cd "$SCRIPT_DIR/.." && pwd)
}

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
  PROJECT_ROOT="$(_checkpoint_default_root)"
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

# T0021: project the checkpoint COMPILED region (above the coord-decisions
# marker) from the canonical loop ledger (.loop/orchestrator-state.json) at
# assembly time, then append the coord-owned region (marker onward) from
# checkpoint.md byte-for-byte. Absent / empty / unparseable ledger => emit
# checkpoint.md as-is (today's hand-authored region, byte-identical default).
project_checkpoint() {
  local ledger="$PROJECT_ROOT/.loop/orchestrator-state.json"
  if [[ ! -s "$ledger" ]]; then
    cat "$CHECKPOINT_FILE"
    return 0
  fi
  LEDGER="$ledger" CHECKPOINT="$CHECKPOINT_FILE" MARKER="$CHECKPOINT_MARKER" python3 -c '
import json, os, sys

ckpt = os.environ["CHECKPOINT"]
marker = os.environ["MARKER"]


def fallback():
    with open(ckpt, encoding="utf-8") as fh:
        sys.stdout.write(fh.read())


try:
    with open(os.environ["LEDGER"], encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not data:
        fallback(); sys.exit(0)
except (OSError, ValueError):
    fallback(); sys.exit(0)

# Split the current checkpoint into its hand-authored compiled region (above the
# marker) and the coord-owned region (marker onward, preserved byte-for-byte).
above, below = "", ""
try:
    with open(ckpt, encoding="utf-8") as fh:
        content = fh.read()
    pos = content.find(marker)
    if pos != -1:
        above, below = content[:pos], content[pos:]
    else:
        above = content
except OSError:
    above, below = "", ""


def section_body(region, heading):
    # Body under the "## <heading>" section of the compiled region (stripped),
    # or None. F5: PRESERVE a hand-authored field the ledger does not carry
    # instead of clobbering it with a placeholder.
    head = "## " + heading
    i = region.find(head)
    if i == -1:
        return None
    start = i + len(head)
    nxt = region.find("\n## ", start)
    body = (region[start:nxt] if nxt != -1 else region[start:]).strip()
    return body or None


def render_loops(loops):
    rows = []
    for lid, lp in loops.items():
        lp = lp or {}
        parts = ["status=%s" % lp.get("status", "?")]
        for k in ("branch", "blast_radius"):
            if lp.get(k):
                parts.append("%s=%s" % (k, lp[k]))
        for k in ("artifacts", "commits"):
            v = lp.get(k)
            if isinstance(v, list) and v:
                parts.append("%s=%s" % (k, ",".join(str(x) for x in v)))
            elif v:
                parts.append("%s=%s" % (k, v))
        rows.append("- **%s** — %s" % (lid, "; ".join(parts)))
    return "\n".join(rows)


# F5: project each compiled-region field from the ledger ONLY when it carries a
# non-empty value; otherwise PRESERVE the hand-authored value from checkpoint.md.
# Never replace a hand-authored field with "(none)". A fully-populated ledger
# renders exactly as before (additive); a sparse ledger keeps what it lacks.
objective = (
    data.get("objective") or section_body(above, "Current objective") or "(none recorded in ledger)"
)
loops = data.get("loops")
if isinstance(loops, dict) and loops:
    loop_block = render_loops(loops)
else:
    loop_block = section_body(above, "Loop states") or "(none)"
conflicts = data.get("open_conflicts") or data.get("conflicts")
if isinstance(conflicts, list) and conflicts:
    conflict_block = "\n".join("- %s" % c for c in conflicts)
else:
    conflict_block = section_body(above, "Open conflicts") or "(none)"

lines = [
    "# checkpoint",
    "",
    "_Compiled from .loop/orchestrator-state.json (the canonical loop ledger) at "
    "boot — do not hand-edit this region; edit the ledger._",
]
if data.get("updated_at"):
    lines += ["", "_ledger as-of: %s_" % data["updated_at"]]
lines += ["", "## Current objective", objective]
lines += ["", "## Loop states", loop_block]
lines += ["", "## Open conflicts", conflict_block]

out = "\n".join(lines) + "\n"
if below:
    out += "\n" + below
sys.stdout.write(out)
' || cat "$CHECKPOINT_FILE"
}

PENDING_OUTPUT="$("$PENDING_SCRIPT" --project-root "$PROJECT_ROOT")"

PROMPT="$HEADER

--- ops-wiki/checkpoint.md ---
$(project_checkpoint)

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
