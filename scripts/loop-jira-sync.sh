#!/usr/bin/env bash
# loop-jira-sync.sh — bidirectional sync between tasks/ files and Jira. STUB.
#
# Task files are the source of truth (AGENTS.md "Task files"); Jira is a thin
# sync target, not the brain. This stub carries the complete CLI surface and
# contract so a later task can fill in the API calls without changing the
# interface. Every mode body currently exits 64 with "TODO: implement".
#
# Contract (binding on the future implementation):
#   pull — for each Jira issue assigned to this project, create or update the
#          matching tasks/T<NNNN>-<slug>.md: issue key -> frontmatter `jira:`,
#          issue status -> frontmatter `status:` (open|in-progress|done|
#          dropped), summary -> `title:`. Issues with no matching file get a
#          new task file with the required body sections stubbed for a
#          human/agent to complete. Files moved by the status<->location
#          invariant (done/dropped -> tasks/archive/) follow loop-task-lint.sh
#          rules.
#   push — for each task file with a `jira:` key, transition the Jira issue
#          so its status matches the file's frontmatter `status:`, and update
#          the issue summary from `title:` if they differ.
#   both — pull, then push, in that order.
#
# Conflict rule: when file and Jira disagree on any synced field, the FILE
# WINS. pull never overwrites a local field that differs from Jira; the
# divergence is pushed back to Jira on the next push.
#
# Logging: every issue created or updated in either direction appends
#   ## [YYYY-MM-DD] sync | <issue key>
# to ops-wiki/log.md (append-only log; one entry per issue per run).
#
# Credentials: NEVER stored in this repo, this script, or task files. The
# implementation must read endpoint + auth from the environment only (e.g.
# JIRA_BASE_URL, JIRA_USER, JIRA_API_TOKEN) and fail fast with a clear error
# when they are unset.
#
# --dry-run must print the planned file writes / Jira transitions without
# performing any of them (no task-file writes, no Jira calls that mutate,
# no ops-wiki/log.md entries).

set -euo pipefail

MODE=""
DRY_RUN=0
TASKS_DIR=""

# Resolve this script's real directory (symlink-aware) so the default tasks
# dir (<repo>/tasks, repo = this script's parent-of-parent directory) is
# correct even when the script is invoked from anywhere or via a symlink.
_jira_sync_script_dir() {
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
  loop-jira-sync.sh [options] <mode>

Modes (exactly one required):
  pull      Create/update task files from assigned Jira issues
            (issue key -> jira:, status -> status:, summary -> title:)
  push      Update Jira issues from task-file frontmatter
            (transition status, update summary from title:)
  both      pull, then push, in that order

Options:
  --dry-run           Print planned changes without performing any of them
                      (no task-file writes, no Jira mutations, no
                      ops-wiki/log.md entries)
  --tasks-dir <path>  Directory holding T<NNNN>-<slug>.md files (archive/
                      subdirectory included)
                      default: <repo>/tasks relative to this script
  -h, --help          Show this help

Contract:
  Task files are the source of truth; on any conflict the FILE wins (pull
  never overwrites a divergent local field — the divergence is pushed back
  on the next push). Every synced issue is logged to ops-wiki/log.md as
  '## [YYYY-MM-DD] sync | <issue key>'. Credentials come from the
  environment only (JIRA_BASE_URL, JIRA_USER, JIRA_API_TOKEN) — never from
  this repo or task files.

Status: STUB. Flags and contract are final; mode bodies exit 64 with
'TODO: implement' until a later task fills them in.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    pull|push|both)
      if [[ -n "$MODE" ]]; then
        echo "error: mode '$1' conflicts with '$MODE'" >&2
        exit 1
      fi
      MODE="$1"; shift
      ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --tasks-dir) TASKS_DIR="$2"; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
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
  echo "error: one of pull|push|both is required" >&2
  usage >&2
  exit 1
fi

if [[ -z "$TASKS_DIR" ]]; then
  TASKS_DIR="$(cd "$(_jira_sync_script_dir)/.." && pwd)/tasks"
fi
if [[ ! -d "$TASKS_DIR" ]]; then
  echo "error: tasks dir not found: $TASKS_DIR" >&2
  exit 1
fi

# STUB BODIES — the parsing and contract above are final; a later task
# replaces the case arms below with real sync logic ($MODE, $DRY_RUN and
# $TASKS_DIR are already validated and ready to use).
case "$MODE" in
  pull|push|both)
    : "$DRY_RUN"  # parsed and validated; consumed by the real implementation
    echo "TODO: implement" >&2
    exit 64
    ;;
esac
