#!/usr/bin/env bash
# lib/lane-config-resolver.sh
#
# Parses lane-config YAML and resolves each lane to a concrete launch
# command via the harness registry. Provides:
#
#   Sourceable functions (when sourced from another script):
#     lane_config_load <path>       # load YAML, populate associative arrays
#     lane_config_lanes             # echo names of all lanes (newline-separated)
#     lane_config_field <l> <f>     # echo lane field (harness, model, repo, role, cmd)
#     lane_config_launch <l>        # echo resolved launch command for lane
#
#   CLI mode (when invoked directly):
#     lane-config-resolver.sh print-resolved --lane-config <path>
#     lane-config-resolver.sh lane-launch <lane> --lane-config <path>
#     lane-config-resolver.sh lane-field  <lane> <field> --lane-config <path>
#     lane-config-resolver.sh validate    --lane-config <path>
#
# Project root for relative-path resolution: see LANE_CONFIG_PROJECT_ROOT
# below. loop-tmux passes its --project-root through this env so any
# `cmd: scripts/foo.sh` inside the YAML resolves against the caller's repo.
#
# Host override resolution:
#   Given /path/to/lane-config.yaml, the resolver auto-merges
#   /path/to/lane-config.<hostname>.yaml on top of it (where hostname =
#   `hostname -s`). Lane keys in the override replace the corresponding
#   lane block entirely. Use to avoid drift between machines without
#   forking the default config.

[[ -n "${LANE_CONFIG_RESOLVER_LOADED:-}" ]] && return 0
LANE_CONFIG_RESOLVER_LOADED=1

_LCR_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Project root for resolving relative `scripts/...` paths inside lane-config
# YAML. Source of truth, in priority order:
#   1. LANE_CONFIG_PROJECT_ROOT env var (set by loop-tmux to the caller's
#      --project-root so every project's lane-config resolves to its own tree)
#   2. PWD at source time (covers running the resolver standalone from inside
#      a project)
# The old "<repo>/scripts/lib/../.." assumption is removed — this lib lives at
# <loop-orchestrator>/lib/ and must not bind to its own repo root.
_LCR_PROJECT_ROOT="${LANE_CONFIG_PROJECT_ROOT:-$PWD}"

# shellcheck source=harness-registry.sh
source "$_LCR_LIB_DIR/harness-registry.sh"

# Module state (associative arrays keyed by "<lane>.<field>"). Bash 3.2 on
# macOS doesn't support associative arrays in the global scope cleanly via
# `declare -gA`, so we use plain variable indirection with sanitized keys.
LANE_CONFIG_LOADED_PATH=""
LANE_CONFIG_LANE_NAMES=()

_lcr_var() {
  # Map "<lane>.<field>" to LANE_CFG__<LANE>__<FIELD>, with hyphens -> underscores.
  local lane="$1"
  local field="$2"
  local lk="${lane//-/_}"
  local fk="${field//-/_}"
  echo "LANE_CFG__$(echo "$lk" | tr '[:lower:]' '[:upper:]')__$(echo "$fk" | tr '[:lower:]' '[:upper:]')"
}

# Resolve a relative `scripts/...` path against project root. Leaves
# absolute paths and non-script commands untouched. Set
# LANE_CONFIG_PRESERVE_PATHS=1 to disable resolution (for portable
# diff-against-config use cases).
_lcr_resolve_relative_path() {
  local val="$1"
  [[ -z "$val" ]] && return 0
  [[ -n "${LANE_CONFIG_PRESERVE_PATHS:-}" ]] && { printf '%s' "$val"; return 0; }
  # Only rewrite if first token is a relative path that looks like a
  # repo-rooted script (starts with "scripts/" or contains "./").
  local first="${val%% *}"
  case "$first" in
    scripts/*|./*)
      local rest="${val#"$first"}"
      printf '%s%s' "$_LCR_PROJECT_ROOT/$first" "$rest"
      ;;
    *)
      printf '%s' "$val"
      ;;
  esac
}

# Verify the python3 + PyYAML runtime dependency once, with an actionable
# message. Without this, a missing interpreter or module surfaces only as a
# raw traceback on stderr while the load below silently yields zero lanes.
_lcr_check_deps() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "lane-config: python3 not found on PATH (required to parse lane-config YAML)" >&2
    return 1
  fi
  if ! python3 -c 'import yaml' >/dev/null 2>&1; then
    echo "lane-config: Python module 'yaml' (PyYAML) is not installed —" >&2
    echo "             run: python3 -m pip install pyyaml" >&2
    return 1
  fi
  return 0
}

# Parse YAML via python3 (PyYAML). Output is a NUL-delimited stream of
# (lane TAB field TAB value) records — robust against newlines/quotes
# inside `cmd` values.
_lcr_parse_yaml() {
  local path="$1"
  python3 - "$path" <<'PY'
import re, sys, yaml
path = sys.argv[1]
with open(path, 'r') as f:
    data = yaml.safe_load(f) or {}
lanes = data.get('lanes') or {}
if not isinstance(lanes, dict):
    sys.stderr.write(f"lane-config: top-level 'lanes' must be a mapping in {path}\n")
    sys.exit(2)
LANE_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')
for lane_name, lane_block in lanes.items():
    # Lane names become shell variable keys downstream (hyphens -> underscores),
    # so reject anything that wouldn't be a safe identifier rather than let the
    # bash side crash on an invalid `printf -v` target.
    if not isinstance(lane_name, str) or not LANE_RE.match(lane_name):
        sys.stderr.write(f"lane-config: invalid lane name {lane_name!r} "
                         f"(use letters, digits, hyphens; must start with a letter)\n")
        sys.exit(2)
    if lane_block is None:
        lane_block = {}
    if not isinstance(lane_block, dict):
        sys.stderr.write(f"lane-config: lane '{lane_name}' must be a mapping\n")
        sys.exit(2)
    for field in ('harness', 'model', 'repo', 'role', 'cmd', 'kind'):
        value = lane_block.get(field, '')
        if value is None:
            value = ''
        elif isinstance(value, bool):
            # PyYAML coerces unquoted yes/no/on/off/true/false to booleans; warn
            # and emit the lowercase literal so `model: no` doesn't silently
            # become the flag string "False". Quote the value to keep it verbatim.
            sys.stderr.write(f"lane-config: lane '{lane_name}' field '{field}': value parsed as a "
                             f"YAML boolean; quote it to keep it a string\n")
            value = str(value).lower()
        else:
            value = str(value)
        # Emit: lane \t field \t value \0
        sys.stdout.write(f"{lane_name}\t{field}\t{value}\0")
PY
}

lane_config_load() {
  local primary="$1"
  if [[ -z "$primary" ]]; then
    echo "lane_config_load: missing path" >&2
    return 1
  fi
  if [[ ! -f "$primary" ]]; then
    echo "lane_config_load: file not found: $primary" >&2
    return 1
  fi

  _lcr_check_deps || return 1

  # Reset previous state.
  local v
  for v in $(compgen -v LANE_CFG__ 2>/dev/null); do
    unset "$v"
  done
  LANE_CONFIG_LANE_NAMES=()
  LANE_CONFIG_LOADED_PATH="$primary"

  # Load primary YAML.
  _lcr_load_into_state "$primary" || return 1

  # Host override (lane-config.<short-hostname>.yaml beside the primary).
  local hostname_short
  hostname_short="$(hostname -s 2>/dev/null || hostname || echo unknown)"
  local primary_dir primary_base primary_ext override
  primary_dir="$(dirname "$primary")"
  # Strip whichever YAML extension the primary uses so the host override is
  # found for a `.yml` primary too, and keep the same extension on the override.
  case "$primary" in
    *.yaml) primary_ext=".yaml" ;;
    *.yml)  primary_ext=".yml" ;;
    *)      primary_ext="" ;;
  esac
  primary_base="$(basename "$primary" "$primary_ext")"
  override="${primary_dir}/${primary_base}.${hostname_short}${primary_ext}"
  if [[ -f "$override" ]]; then
    # Only claim the merge in LANE_CONFIG_LOADED_PATH AFTER the override
    # actually loads — otherwise a failed/ignored override would be reported
    # as applied while the primary values silently win.
    _lcr_load_into_state "$override" || return 1
    LANE_CONFIG_LOADED_PATH="$primary + $override"
  fi

  return 0
}

_lcr_load_into_state() {
  local path="$1"
  # Run the parser into a temp file rather than a process substitution so we
  # can observe python's exit status. A `done < <(_lcr_parse_yaml ...)` loop
  # discards that status, silently turning a parse failure (missing PyYAML,
  # malformed YAML, non-mapping `lanes:`) into an empty — but "successful" —
  # load. The temp file preserves the NUL framing that protects embedded
  # newlines/quotes in `cmd` values.
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/lcr-parse.XXXXXX")" || {
    echo "lane-config: could not create temp file for $path" >&2
    return 1
  }
  if ! _lcr_parse_yaml "$path" > "$tmp"; then
    rm -f "$tmp"
    echo "lane-config: failed to parse $path" >&2
    return 1
  fi

  local rec lane field value rest var
  while IFS= read -r -d '' rec; do
    # rec format: lane TAB field TAB value. Split on the FIRST TWO tabs via
    # parameter expansion, NOT `read <<<"$rec"` — a here-string read stops at
    # the first newline and would truncate a multi-line block-scalar `cmd:`
    # to its first line, defeating the NUL framing above.
    lane="${rec%%$'\t'*}"
    rest="${rec#*$'\t'}"
    field="${rest%%$'\t'*}"
    value="${rest#*$'\t'}"
    [[ -z "$lane" ]] && continue
    var="$(_lcr_var "$lane" "$field")"
    printf -v "$var" '%s' "$value"
    # Track lane in append-only order, dedup.
    local seen=0
    local n
    for n in ${LANE_CONFIG_LANE_NAMES[@]+"${LANE_CONFIG_LANE_NAMES[@]}"}; do
      [[ "$n" == "$lane" ]] && seen=1 && break
    done
    if (( seen == 0 )); then
      LANE_CONFIG_LANE_NAMES+=("$lane")
    fi
  done < "$tmp"
  rm -f "$tmp"
  return 0
}

lane_config_lanes() {
  local n
  for n in ${LANE_CONFIG_LANE_NAMES[@]+"${LANE_CONFIG_LANE_NAMES[@]}"}; do
    printf '%s\n' "$n"
  done
}

lane_config_field() {
  local lane="$1"
  local field="$2"
  local var
  var="$(_lcr_var "$lane" "$field")"
  printf '%s' "${!var-}"
}

# Resolve a lane to its launch command at the level requested.
#
#   lane_config_launch <lane>                    # bare launch (matches today's apply_preset)
#   lane_config_launch <lane> with-auto-approve  # bare launch + auto_approve_flag for the harness
#
# The two-form split lets a caller bring a lane up with the bare launch
# (e.g. "claude") and later upgrade it to the auto-approved form
# (e.g. "claude --dangerously-skip-permissions") without re-resolving. The
# bare form matches the AUTO_INFRA_CMD/AUTO_WEB_CMD strings produced by
# apply_preset.
lane_config_launch() {
  local lane="$1"
  local mode="${2:-bare}"
  local harness model cmd
  harness="$(lane_config_field "$lane" harness)"
  model="$(lane_config_field "$lane" model)"
  cmd="$(lane_config_field "$lane" cmd)"

  if [[ -z "$harness" ]]; then
    echo "lane_config_launch: lane '$lane' has no harness" >&2
    return 1
  fi
  if ! harness_known "$harness"; then
    echo "lane_config_launch: lane '$lane' references unknown harness '$harness'" >&2
    return 1
  fi

  # Shell harness: the lane's cmd (if any) IS the launch.
  if [[ "$harness" == "shell" ]]; then
    printf '%s' "$(_lcr_resolve_relative_path "$cmd")"
    return 0
  fi

  # mprocs harness: cmd if set, else default mprocs binary.
  if [[ "$harness" == "mprocs" ]]; then
    if [[ -n "$cmd" ]]; then
      printf '%s' "$(_lcr_resolve_relative_path "$cmd")"
    else
      printf '%s' "$(harness_field mprocs launch_cmd)"
    fi
    return 0
  fi

  # LLM harness: registry-resolved with optional model + optional auto-approve flag.
  local base
  base="$(harness_resolve_launch "$harness" "$model")" || return 1
  if [[ "$mode" == "with-auto-approve" ]]; then
    local flag
    flag="$(harness_field "$harness" auto_approve_flag)"
    if [[ -n "$flag" ]]; then
      printf '%s %s' "$base" "$flag"
      return 0
    fi
  fi
  printf '%s' "$base"
}

# ─── Validation ──────────────────────────────────────────────────────────

lane_config_validate() {
  local errors=0
  local lane harness

  # A config that resolves to zero lanes is almost always a failure (an empty
  # `lanes:` mapping, or a parse that produced nothing). Surface it instead of
  # reporting a vacuous success.
  if [[ ${#LANE_CONFIG_LANE_NAMES[@]} -eq 0 ]]; then
    echo "lane-config: no lanes found in ${LANE_CONFIG_LOADED_PATH:-<none>}" >&2
    return 1
  fi

  local kind
  for lane in ${LANE_CONFIG_LANE_NAMES[@]+"${LANE_CONFIG_LANE_NAMES[@]}"}; do
    harness="$(lane_config_field "$lane" harness)"
    if [[ -z "$harness" ]]; then
      echo "lane '$lane': missing 'harness' field" >&2
      errors=$((errors+1))
      continue
    fi
    if ! harness_known "$harness"; then
      echo "lane '$lane': unknown harness '$harness' (known: ${HARNESS_REGISTRY_NAMES[*]})" >&2
      errors=$((errors+1))
    fi
    # T0019: optional declared kind. Absent = inferred (base/dynamic); when set
    # it must be standing|worker.
    kind="$(lane_config_field "$lane" kind)"
    if [[ -n "$kind" && "$kind" != "standing" && "$kind" != "worker" ]]; then
      echo "lane '$lane': invalid kind '$kind' (use standing|worker, or omit)" >&2
      errors=$((errors+1))
    fi
  done
  if (( errors > 0 )); then
    echo "lane-config validation failed: $errors error(s)" >&2
    return 1
  fi
  return 0
}

# ─── CLI mode ────────────────────────────────────────────────────────────

_lcr_cli() {
  local cmd="${1:-}"
  shift || true

  # Common flag: --lane-config <path>. No default — loop-orchestrator ships
  # without an opinionated default config (the caller's project owns that).
  local config_path=""
  local positional=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lane-config)
        config_path="$2"
        shift 2
        ;;
      -h|--help)
        _lcr_cli help
        return 0
        ;;
      *)
        positional+=("$1")
        shift
        ;;
    esac
  done
  set -- "${positional[@]}"

  # All sub-commands need a config path. Enforce up front so users get a
  # clear error instead of a confusing "file not found" on an empty path.
  if [[ -z "$config_path" && "$cmd" != "help" && "$cmd" != "-h" && "$cmd" != "--help" && -n "$cmd" ]]; then
    echo "lane-config-resolver: --lane-config <path> is required" >&2
    return 2
  fi

  case "$cmd" in
    print-resolved)
      lane_config_load "$config_path" || return 1
      lane_config_validate || return 1
      printf '# lane-config: %s\n' "$LANE_CONFIG_LOADED_PATH"
      printf '# bare = bootstrap launch; audit = bare + harness auto-approve flag\n'
      local lane harness model bare audit
      for lane in ${LANE_CONFIG_LANE_NAMES[@]+"${LANE_CONFIG_LANE_NAMES[@]}"}; do
        harness="$(lane_config_field "$lane" harness)"
        model="$(lane_config_field "$lane" model)"
        bare="$(lane_config_launch "$lane")"
        audit="$(lane_config_launch "$lane" with-auto-approve)"
        if [[ -n "$model" ]]; then
          printf '%-16s harness=%-12s model=%-22s\n  bare:  %s\n  audit: %s\n' "$lane" "$harness" "$model" "$bare" "$audit"
        else
          printf '%-16s harness=%-12s\n  bare:  %s\n  audit: %s\n' "$lane" "$harness" "$bare" "$audit"
        fi
      done
      ;;
    lane-launch)
      local lane="${1:?usage: lane-launch <lane>}"
      lane_config_load "$config_path" || return 1
      lane_config_launch "$lane"
      printf '\n'
      ;;
    lane-field)
      local lane="${1:?usage: lane-field <lane> <field>}"
      local field="${2:?usage: lane-field <lane> <field>}"
      lane_config_load "$config_path" || return 1
      lane_config_field "$lane" "$field"
      printf '\n'
      ;;
    validate)
      lane_config_load "$config_path" || return 1
      lane_config_validate || return 1
      echo "lane-config OK ($LANE_CONFIG_LOADED_PATH)"
      ;;
    lanes)
      lane_config_load "$config_path" || return 1
      lane_config_lanes
      ;;
    -h|--help|help|"")
      cat <<EOF
lane-config-resolver — read lane-config YAML and resolve each lane via the harness registry.

Usage:
  lane-config-resolver.sh print-resolved --lane-config <path>
  lane-config-resolver.sh lane-launch    <lane>  --lane-config <path>
  lane-config-resolver.sh lane-field     <lane> <field> --lane-config <path>
  lane-config-resolver.sh validate       --lane-config <path>
  lane-config-resolver.sh lanes          --lane-config <path>

Host override (auto-merged): <path-base>.\$(hostname -s).yaml beside the primary.

Env vars:
  LANE_CONFIG_PROJECT_ROOT  Project root for resolving relative scripts/...
                            paths inside the YAML. loop-tmux sets this to
                            --project-root.
  LANE_CONFIG_PRESERVE_PATHS  Non-empty disables relative-path rewriting.

When sourced exposes:
  lane_config_load <path>
  lane_config_lanes
  lane_config_field <lane> <field>
  lane_config_launch <lane>
  lane_config_validate
EOF
      ;;
    *)
      echo "unknown command: $cmd" >&2
      _lcr_cli help >&2
      return 1
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  _lcr_cli "$@"
fi
