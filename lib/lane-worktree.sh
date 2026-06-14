#!/usr/bin/env bash
# lib/lane-worktree.sh — conditional git-worktree isolation lifecycle for
# dynamically-added lanes (harness-governance Phase 4 / T0025).
#
# DORMANT by default: only exercised when a lane's resolved isolation is
# `worktree`. The default `shared` keeps add-lane on PROJECT_ROOT (today's exact
# behavior — a no-`--repo` add-lane inherits the base window's path). The
# machinery is built ahead of the plan's concurrency > 1 trigger so it is ready
# when >=2 concurrent code-writers run; nothing here runs on the shared path.
#
# Sourceable functions (sourced by loop-tmux.sh; also unit-tested directly):
#   lane_worktree_dir <root> <session> <window>      # the worktree path
#   lane_worktree_branch <session> <window>          # the lane's branch name
#   lane_worktree_record_branch <root> <window> <br> # merge loops.<window>.branch
#   lane_worktree_provision <root> <session> <window># provision (prints cwd)
#   lane_worktree_teardown <root> <session> <window> # remove, NEVER orphaning
#
# This file MUST be POSIX-bash compatible (no zsh-isms) so it sources cleanly
# under tmux-spawned shells.

[[ -n "${LANE_WORKTREE_LIB_LOADED:-}" ]] && return 0
LANE_WORKTREE_LIB_LOADED=1

# lane_worktree_dir <root> <session> <window> — where a lane's worktree lives.
# Under .loop/ so it shares the project's .gitignore conventions and is easy to
# find/prune; one subdir per (session, window).
lane_worktree_dir() {
  printf '%s/.loop/worktrees/%s/%s' "$1" "$2" "$3"
}

# lane_worktree_branch <session> <window> — the dedicated branch a lane's tree
# checks out, namespaced so N lanes never collide and the digest/integration
# lane can recognize loop-owned branches.
lane_worktree_branch() {
  printf 'loop/%s/%s' "$1" "$2"
}

# lane_worktree_record_branch <root> <window> <branch> — merge
# loops.<window>.branch into .loop/orchestrator-state.json (the canonical
# ledger) so the digest and a future integration lane (T0026) find the branch.
# ADDITIVE: creates the ledger if absent, preserves every existing key, only
# sets the one branch field. Atomic write.
lane_worktree_record_branch() {
  local root="$1" window="$2" branch="$3"
  mkdir -p "$root/.loop"
  LEDGER="$root/.loop/orchestrator-state.json" WINDOW="$window" BRANCH="$branch" python3 - <<'PY'
import json, os

ledger = os.environ["LEDGER"]
try:
    with open(ledger, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        data = {}
except (OSError, ValueError):
    data = {}
data.setdefault("schema_version", 2)
loops = data.get("loops")
if not isinstance(loops, dict):
    loops = data["loops"] = {}
entry = loops.get(os.environ["WINDOW"])
if not isinstance(entry, dict):
    entry = loops[os.environ["WINDOW"]] = {}
entry["branch"] = os.environ["BRANCH"]
tmp = ledger + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
os.replace(tmp, ledger)
PY
}

# _lane_worktree_venv <wtdir> — best-effort per-worktree venv rebuild. A worktree
# shares .git objects but does NOT carry untracked .venv/caches, so the Python
# layer needs its own. Skipped when uv is absent or LOOP_WORKTREE_SKIP_VENV is
# set (unit tests / shared-only hosts). A failure WARNS and continues — it never
# fails provisioning. Handles the macOS iCloud UF_HIDDEN `.pth` gotcha.
_lane_worktree_venv() {
  local wtdir="$1"
  [[ -n "${LOOP_WORKTREE_SKIP_VENV:-}" ]] && return 0
  if ! command -v uv >/dev/null 2>&1; then
    echo "lane-worktree: uv not found; skipping per-worktree venv in $wtdir" >&2
    return 0
  fi
  if ( cd "$wtdir" && uv sync >&2 ); then
    ( cd "$wtdir" && command -v chflags >/dev/null 2>&1 \
        && chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null ) || true
  else
    echo "lane-worktree: venv rebuild failed in $wtdir (continuing)" >&2
  fi
  return 0
}

# lane_worktree_provision <root> <session> <window> — provision a git worktree on
# a dedicated branch, record the branch in the ledger, rebuild the venv. Prints
# the worktree path (the lane cwd) on stdout. Reuses an existing tree (re-records
# the branch). Returns non-zero only if the worktree could not be created.
lane_worktree_provision() {
  local root="$1" session="$2" window="$3" wtdir branch
  wtdir="$(lane_worktree_dir "$root" "$session" "$window")"
  branch="$(lane_worktree_branch "$session" "$window")"
  if [[ -d "$wtdir" ]]; then
    lane_worktree_record_branch "$root" "$window" "$branch"
    printf '%s' "$wtdir"
    return 0
  fi
  mkdir -p "$(dirname "$wtdir")"
  if git -C "$root" show-ref --verify --quiet "refs/heads/$branch"; then
    git -C "$root" worktree add "$wtdir" "$branch" >&2 || return 1
  else
    git -C "$root" worktree add -b "$branch" "$wtdir" HEAD >&2 || return 1
  fi
  lane_worktree_record_branch "$root" "$window" "$branch"
  _lane_worktree_venv "$wtdir"
  printf '%s' "$wtdir"
}

# lane_worktree_teardown <root> <session> <window> — remove a lane's worktree
# without EVER orphaning it and without EVER force-discarding uncommitted work:
#   - no worktree dir            -> no-op (shared lane / already removed)
#   - clean tree                 -> `git worktree remove` (+ prune)
#   - dirty tree (uncommitted)   -> PRESERVE it (left listed, branch recorded)
#                                   with a loud warning for manual recovery.
# The branch is always kept — it holds the lane's work for the integration lane.
lane_worktree_teardown() {
  local root="$1" session="$2" window="$3" wtdir branch
  wtdir="$(lane_worktree_dir "$root" "$session" "$window")"
  [[ -d "$wtdir" ]] || return 0
  branch="$(lane_worktree_branch "$session" "$window")"
  if [[ -n "$(git -C "$wtdir" status --porcelain 2>/dev/null)" ]]; then
    echo "lane-worktree: $wtdir has uncommitted work on '$branch' — PRESERVING the worktree (never force-removing); commit/recover it then 'git -C $root worktree remove $wtdir'." >&2
    return 0
  fi
  if ! git -C "$root" worktree remove "$wtdir" 2>/dev/null; then
    # A clean tree that still refuses (e.g. a stale lock) — safe to force since
    # there is no uncommitted work to lose.
    git -C "$root" worktree remove --force "$wtdir" 2>/dev/null || {
      echo "lane-worktree: could not remove clean worktree $wtdir" >&2
      return 1
    }
  fi
  git -C "$root" worktree prune 2>/dev/null || true
  return 0
}
