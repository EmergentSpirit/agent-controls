#!/usr/bin/env bash
# launch-agent.example.sh -- EXAMPLE launcher: one pane, one role, one agent.
#
#   ./launch-agent.example.sh builder             # write-heavy role
#   ./launch-agent.example.sh researcher          # read-heavy role
#   ./launch-agent.example.sh builder --resume    # extra arguments reach the CLI
#
# The chain has exactly three links, and the order IS the design:
#
#   VAULT                ENVIRONMENT              CLI
#   decrypted ONCE  -->  values live in memory -->  exec, never fork
#   at boot              only, never on disk        (the CLI inherits the env)
#
# This file is a TEMPLATE. It is meant to be copied and adapted, so every
# block below is commented for someone who has never seen this repository.
# Nothing here is armed by default, and nothing here is site-specific.
#
# Reasoning behind the shape (one role per pane, zero inter-agent
# communication, why isolation is the mechanism): docs/launchers.md.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: launch-agent.example.sh <role> [extra CLI arguments...]

Roles shipped as examples:
  builder     write-heavy: the full mutation gate battery
  researcher  read-heavy: a write perimeter + a response ceiling

Role <r> loads launchers/settings.<r>.example.json, with one deliberate
exception: "builder" loads settings.example.json, the canonical file the
test suites check.

Environment (all optional, all with a default):
  HARNESS_HOME              agent-controls checkout (default: parent of this file)
  HARNESS_STATE_DIR         state directory (default: ~/.harness)
  HARNESS_WORKSPACE         directory the agent starts in (default: $PWD)
  HARNESS_WRITE_SCOPE       write perimeter (default: the workspace)
  HARNESS_SETTINGS          settings file to use verbatim (skips rendering)
  HARNESS_CLI               CLI binary to exec (default: claude)
  HARNESS_VAULT             encrypted env file (default: ~/.harness-secrets.env.age)
  HARNESS_VAULT_IDENTITY    age identity file (default: ~/.config/age/identity.txt)
  HARNESS_VAULT_IDENTITY_CMD  command PRINTING an identity, for a hardware-backed
                              key (see launchers/vault.example.sh)
  HARNESS_VAULT_KEYS        colon-separated allowlist of names to export
  HARNESS_ROLE_PROMPT       file holding this role's system prompt
  HARNESS_BOOT_PROMPT       first prompt handed to the agent at boot
  HARNESS_MCP_CONFIG        connector config; unset = no external connectors
EOF
}

# ---------------------------------------------------------------------------
# 1. The role. It is an argument, not a copy of this file per agent: one
#    template, N panes. The role ends up in a filename, so it is validated.
# ---------------------------------------------------------------------------
case "${1:-}" in
  "")        usage; exit 2 ;;
  -h|--help) usage; exit 0 ;;
esac
role="$1"
shift

if [[ ! "$role" =~ ^[a-z][a-z0-9-]*$ ]]; then
  echo "launch-agent: invalid role '$role' (lowercase letters, digits, dashes)" >&2
  exit 2
fi

# The agent CLI usually lives in a per-user bin directory. Prepend it HERE
# rather than assuming the caller already has it: the same omission inside a
# systemd unit is what silently killed three scheduled runs, which is why
# every example unit in launchers/systemd/ carries an explicit PATH.
export PATH="$HOME/.local/bin:$PATH"

# ---------------------------------------------------------------------------
# 2. Where things live. HARNESS_HOME is the checkout; the settings examples
#    reference it, so it must be exported BEFORE the CLI starts.
# ---------------------------------------------------------------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export HARNESS_HOME="${HARNESS_HOME:-$(dirname "$HERE")}"
export HARNESS_AGENT="$role"
export HARNESS_STATE_DIR="${HARNESS_STATE_DIR:-$HOME/.harness}"
mkdir -p "$HARNESS_STATE_DIR"

# The directory the agent starts in. A dedicated directory per role is the
# cheapest isolation there is: two agents that never share a working tree
# cannot silently overwrite each other.
workspace="${HARNESS_WORKSPACE:-$PWD}"
cd "$workspace" || {
  echo "launch-agent: workspace '$workspace' unreachable" >&2
  exit 1
}

# The write perimeter the scope gate enforces. Default = the workspace, so a
# role that forgets to set it is narrow, never wide.
export HARNESS_WRITE_SCOPE="${HARNESS_WRITE_SCOPE:-$workspace}"

# ---------------------------------------------------------------------------
# 3. The vault. ONE decryption at boot; the values are exported into this
#    process and inherited by the CLI through exec. They are never written
#    back to disk, never echoed, never logged. If the vault is absent or the
#    decryption is refused, the session CONTINUES with no keys and says so
#    out loud: a launcher that dies on an absent optional file is a launcher
#    nobody can debug at 6 in the morning.
#
#    See launchers/vault.example.sh for how the file is produced.
# ---------------------------------------------------------------------------
vault_file="${HARNESS_VAULT:-$HOME/.harness-secrets.env.age}"
vault_identity="${HARNESS_VAULT_IDENTITY:-$HOME/.config/age/identity.txt}"
vault_keys="${HARNESS_VAULT_KEYS:-}"

if [[ ! -r "$vault_file" ]]; then
  echo "launch-agent: no vault at '$vault_file' -- starting with NO injected keys" >&2
elif ! command -v age >/dev/null 2>&1; then
  echo "launch-agent: 'age' not installed -- vault ignored, NO injected keys" >&2
else
  vault_plain=""
  if [[ -n "${HARNESS_VAULT_IDENTITY_CMD:-}" ]]; then
    # Hardware-key-backed identity (for example a PIV token): the identity is
    # PRINTED by a command and consumed through a file descriptor, so it never
    # lands in a file either. Split on spaces so the command may carry flags.
    read -r -a identity_cmd <<< "$HARNESS_VAULT_IDENTITY_CMD"
    vault_plain="$(age --decrypt -i <("${identity_cmd[@]}") "$vault_file")" \
      || vault_plain=""
  else
    vault_plain="$(age --decrypt -i "$vault_identity" "$vault_file")" \
      || vault_plain=""
  fi

  if [[ -z "$vault_plain" ]]; then
    echo "launch-agent: vault decryption failed or was cancelled --" \
      "keys are EMPTY for this session" >&2
  else
    exported=0
    while IFS= read -r line; do
      line="${line#export }"                                   # tolerate 'export NAME=...'
      [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue     # skip blanks and comments
      name="${line%%=*}"
      value="${line#*=}"
      case "$value" in                                          # drop balanced quotes
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
      esac
      # Optional allowlist: export only what this role actually needs. An
      # empty allowlist means "everything the vault carries".
      if [[ -n "$vault_keys" && ":$vault_keys:" != *":$name:"* ]]; then
        continue
      fi
      export "${name}=${value}"
      exported=$((exported + 1))
    done <<< "$vault_plain"
    # Count only. Names and values never reach a terminal or a log file.
    echo "launch-agent: vault loaded, $exported variable(s) in memory" >&2
    unset vault_plain line name value
  fi
fi

# ---------------------------------------------------------------------------
# 4. The settings file for this role. The shipped examples reference
#    $HARNESS_HOME inside their hook commands; rendering a resolved copy into
#    the state directory removes the "edit the JSON by hand" step and keeps
#    the repository file as the single source of truth.
#
#    Point HARNESS_SETTINGS at your own file to skip all of this.
# ---------------------------------------------------------------------------
case "$role" in
  builder) settings_src="$HARNESS_HOME/launchers/settings.example.json" ;;
  *)       settings_src="$HARNESS_HOME/launchers/settings.$role.example.json" ;;
esac

if [[ -n "${HARNESS_SETTINGS:-}" ]]; then
  settings="$HARNESS_SETTINGS"
elif [[ -r "$settings_src" ]]; then
  settings="$HARNESS_STATE_DIR/settings.$role.json"
  # '|' as the sed delimiter: a path may hold '/', it almost never holds '|'.
  sed "s|\$HARNESS_HOME|$HARNESS_HOME|g" "$settings_src" > "$settings"
else
  echo "launch-agent: no settings for role '$role' (looked for $settings_src)" >&2
  echo "launch-agent: available examples:" >&2
  ls -1 "$HARNESS_HOME"/launchers/settings*.example.json >&2 || true
  exit 1
fi

if [[ ! -r "$settings" ]]; then
  echo "launch-agent: settings '$settings' unreadable -- refusing to start an" \
    "agent with NO wiring" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 5. The CLI invocation. Built as an array so an unset option adds nothing
#    at all, instead of an empty string the CLI would have to interpret.
# ---------------------------------------------------------------------------
cli="${HARNESS_CLI:-${HARNESS_LLM_CLI_NAMES:-claude}}"
cli="${cli%%:*}"                       # HARNESS_LLM_CLI_NAMES is a colon list
if ! command -v "$cli" >/dev/null 2>&1; then
  echo "launch-agent: CLI '$cli' not found in PATH" >&2
  exit 1
fi

cli_args=(--settings "$settings")

# The role's own system prompt (its job, its voice, its standing constraints).
# Deliberately NOT shipped here: that file is yours, and it belongs outside
# this repository.
if [[ -n "${HARNESS_ROLE_PROMPT:-}" ]]; then
  if [[ -r "$HARNESS_ROLE_PROMPT" ]]; then
    cli_args+=(--append-system-prompt "$(cat "$HARNESS_ROLE_PROMPT")")
  else
    echo "launch-agent: role prompt '$HARNESS_ROLE_PROMPT' unreadable -- ignored" >&2
  fi
fi

# Connectors are OFF unless you name a config: an agent that reaches nothing
# by default is an agent whose perimeter you can actually describe.
if [[ -n "${HARNESS_MCP_CONFIG:-}" ]]; then
  cli_args+=(--strict-mcp-config --mcp-config "$HARNESS_MCP_CONFIG")
fi

# The boot prompt is the first thing the agent reads. A useful one points at
# the continuity file and stops, for example:
#   HARNESS_BOOT_PROMPT='Read the handoff file at ~/state/handoff.md, summarize
#   it in three lines, then wait.'
if [[ -n "${HARNESS_BOOT_PROMPT:-}" ]]; then
  cli_args+=("$HARNESS_BOOT_PROMPT")
fi

# exec, never a plain call: the launcher is REPLACED by the CLI. No parent
# shell survives holding the decrypted values, and no orphan process is left
# behind when the pane is closed.
exec "$cli" "${cli_args[@]}" "$@"
