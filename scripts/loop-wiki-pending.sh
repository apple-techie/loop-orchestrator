#!/usr/bin/env bash
# loop-wiki-pending.sh — docs-lane ingest digest for the ops-wiki mailbox.
#
# Pending work = files still in .loop/messages/ (the docs lane moves each
# ingested message to .loop/messages/processed/, filename unchanged — see
# AGENTS.md "Ingest protocol"). Prints the pending messages oldest-first,
# the processed count, and the last 5 ops-wiki/log.md entries.
#
# --quiet prints only the pending count (for use in prompts and cron).

set -euo pipefail

PROJECT_ROOT=""
QUIET=0

# Resolve this script's real directory (symlink-aware) so the default project
# root (this script's parent-of-parent directory) is correct even when the
# script is invoked from anywhere or via a symlink.
_pending_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [[ -L "$src" ]]; do
    local dir; dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" && pwd
}

usage() {
  cat <<'EOF'
Usage:
  loop-wiki-pending.sh [options]

Options:
  --project-root <path>  Repo root containing .loop/ and ops-wiki/
                         default: this script's parent-of-parent directory
  --quiet                Print only the pending-message count (integer)
  -h, --help             Show this help

Output (default mode):
  pending mailbox messages oldest-first, processed count, and the last
  5 ops-wiki/log.md entries.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    --quiet)        QUIET=1; shift ;;
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

if [[ -z "$PROJECT_ROOT" ]]; then
  PROJECT_ROOT="$(cd "$(_pending_script_dir)/.." && pwd)"
fi

MAILBOX_DIR="$PROJECT_ROOT/.loop/messages"
PROCESSED_DIR="$MAILBOX_DIR/processed"
LOG_FILE="$PROJECT_ROOT/ops-wiki/log.md"

# Mailbox filenames are YYYYMMDD-HHMMSS-<from>-to-<to>.md, so a lexicographic
# sort is oldest-first.
pending_list=""
pending_count=0
if [[ -d "$MAILBOX_DIR" ]]; then
  pending_list="$(find "$MAILBOX_DIR" -maxdepth 1 -type f -name '*.md' -exec basename {} \; | sort)"
  if [[ -n "$pending_list" ]]; then
    pending_count="$(printf '%s\n' "$pending_list" | wc -l | tr -d ' ')"
  fi
fi

if [[ "$QUIET" -eq 1 ]]; then
  echo "$pending_count"
  exit 0
fi

processed_count=0
if [[ -d "$PROCESSED_DIR" ]]; then
  processed_count="$(find "$PROCESSED_DIR" -maxdepth 1 -type f -name '*.md' | wc -l | tr -d ' ')"
fi

echo "pending: $pending_count"
if [[ -n "$pending_list" ]]; then
  printf '%s\n' "$pending_list" | sed 's/^/  /'
fi
echo "processed: $processed_count"
echo "last log entries:"
if [[ -f "$LOG_FILE" ]]; then
  grep '^## \[' "$LOG_FILE" | tail -5 | sed 's/^/  /' || true
else
  echo "  (no ops-wiki/log.md)"
fi
