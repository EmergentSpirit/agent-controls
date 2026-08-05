#!/usr/bin/env bash
# vault.example.sh -- EXAMPLE vault helper: create it, encrypt it, inspect it,
# prove it still opens. The launcher READS the vault; this file BUILDS it.
#
#   ./vault.example.sh template ~/secrets.env   # write an example plaintext file
#   ./vault.example.sh encrypt  ~/secrets.env   # encrypt it into the vault
#   ./vault.example.sh names                    # the NAMES it carries, no values
#   ./vault.example.sh check                    # does it still open at all
#
# THE MODEL, in one sentence: one age-encrypted env file, decrypted ONCE at
# boot by the launcher, values exported into the process environment, never
# written back to disk in clear and never printed.
#
# Why not a plaintext file with mode 600?
#   - Mode 600 protects against another user, not against a backup, a sync
#     client, a snapshot, a crash dump or a grep across the home directory.
#     Plaintext at rest quietly multiplies: the copies are what leak.
#   - The vault is opened ONCE per pane, at a moment a human is present.
#     Every later read is a read of process memory, which dies with the pane.
#   - The identity can be backed by hardware (for example a PIV token) so that
#     the encrypted file alone is worth nothing without the physical key.
#
# What NEVER goes near the vault:
#   - a value printed to a terminal, a log, or a journal entry;
#   - a value passed on a command line (it would show up in the process list);
#   - a decrypted copy on disk, even briefly, even in a temporary directory.
#
# Environment:
#   HARNESS_VAULT               vault path (default: ~/.harness-secrets.env.age)
#   HARNESS_VAULT_IDENTITY      age identity file (default: ~/.config/age/identity.txt)
#   HARNESS_VAULT_IDENTITY_CMD  command PRINTING an identity, for a hardware-backed
#                               key; when set it wins over HARNESS_VAULT_IDENTITY
set -euo pipefail

VAULT="${HARNESS_VAULT:-$HOME/.harness-secrets.env.age}"
IDENTITY="${HARNESS_VAULT_IDENTITY:-$HOME/.config/age/identity.txt}"

usage() {
  cat >&2 <<'EOF'
usage: vault.example.sh <template|encrypt|names|check> [path]

  template <path>  write an EXAMPLE plaintext env file (never overwrites)
  encrypt  <path>  encrypt that file into $HARNESS_VAULT, then tell you to
                   destroy the plaintext
  names            list the variable NAMES the vault carries (never values)
  check            decrypt in memory and report how many names were found

The identity comes from HARNESS_VAULT_IDENTITY (a file) or, when it is set,
from HARNESS_VAULT_IDENTITY_CMD (a command that PRINTS an identity, which is
how a hardware-backed key is used without ever writing it down).
EOF
}

require_age() {
  if ! command -v age >/dev/null 2>&1; then
    echo "vault: 'age' is not installed (https://github.com/FiloSottile/age)" >&2
    exit 1
  fi
}

# Prints the decrypted vault on stdout. Callers keep it in a variable or pipe
# it; it is never redirected to a file anywhere in this repository.
decrypt_vault() {
  require_age
  if [[ ! -r "$VAULT" ]]; then
    echo "vault: no vault at '$VAULT'" >&2
    return 1
  fi
  if [[ -n "${HARNESS_VAULT_IDENTITY_CMD:-}" ]]; then
    local identity_cmd
    read -r -a identity_cmd <<< "$HARNESS_VAULT_IDENTITY_CMD"
    age --decrypt -i <("${identity_cmd[@]}") "$VAULT"
  else
    if [[ ! -r "$IDENTITY" ]]; then
      echo "vault: no identity at '$IDENTITY' (set HARNESS_VAULT_IDENTITY" \
        "or HARNESS_VAULT_IDENTITY_CMD)" >&2
      return 1
    fi
    age --decrypt -i "$IDENTITY" "$VAULT"
  fi
}

# The launcher parses exactly these two shapes, one per line:
#   NAME=value            NAME="value"            NAME='value'
#   export NAME=value     (the 'export ' prefix is tolerated and stripped)
# Anything else -- comments, blank lines, multi-line values -- is ignored.
cmd_template() {
  local out="${1:-}"
  if [[ -z "$out" ]]; then
    echo "vault: template needs a destination path" >&2
    exit 2
  fi
  if [[ -e "$out" ]]; then
    echo "vault: '$out' already exists, refusing to overwrite it" >&2
    exit 1
  fi
  ( umask 077; cat > "$out" <<'EOF'
# EXAMPLE plaintext vault -- these names are placeholders, replace them with
# the ones your own connectors and helpers read. One assignment per line.
#
# This file is the ONLY moment your secrets exist in clear. Encrypt it, then
# destroy it (shred -u, or the equivalent on your filesystem).

EXAMPLE_API_KEY=replace-me
EXAMPLE_SERVICE_TOKEN=replace-me
EXAMPLE_WEBHOOK_SECRET=replace-me

# Quotes are optional and are stripped when balanced:
EXAMPLE_QUOTED_VALUE="replace me, spaces are fine"
EOF
  )
  echo "vault: wrote $out (mode 600). Edit it, then: $0 encrypt $out" >&2
}

cmd_encrypt() {
  local plain="${1:-}"
  require_age
  if [[ -z "$plain" || ! -r "$plain" ]]; then
    echo "vault: encrypt needs a readable plaintext env file" >&2
    exit 2
  fi
  mkdir -p "$(dirname "$VAULT")"
  local tmp="$VAULT.tmp.$$"
  if [[ -n "${HARNESS_VAULT_IDENTITY_CMD:-}" ]]; then
    local identity_cmd
    read -r -a identity_cmd <<< "$HARNESS_VAULT_IDENTITY_CMD"
    ( umask 077; age --encrypt -i <("${identity_cmd[@]}") -o "$tmp" "$plain" )
  else
    if [[ ! -r "$IDENTITY" ]]; then
      echo "vault: no identity at '$IDENTITY' -- generate one with" \
        "'age-keygen -o $IDENTITY', or set HARNESS_VAULT_IDENTITY_CMD" >&2
      exit 1
    fi
    ( umask 077; age --encrypt -i "$IDENTITY" -o "$tmp" "$plain" )
  fi
  # Rename only once the encryption succeeded: a half-written vault must never
  # replace a vault that still opens.
  mv -f "$tmp" "$VAULT"
  echo "vault: wrote $VAULT" >&2
  echo "vault: now DESTROY the plaintext -- e.g. 'shred -u $plain'" >&2
}

cmd_names() {
  # Names only. This is the command you run when the launcher says it exported
  # fewer variables than you expected: it answers the question without ever
  # showing a value.
  decrypt_vault \
    | sed -n 's/^\(export \)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)=.*/\2/p' \
    | sort -u
}

cmd_check() {
  local count
  count="$(cmd_names | wc -l)"
  echo "vault: $VAULT opens, $count name(s) inside" >&2
}

case "${1:-}" in
  template) shift; cmd_template "${1:-}" ;;
  encrypt)  shift; cmd_encrypt "${1:-}" ;;
  names)    cmd_names ;;
  check)    cmd_check ;;
  -h|--help) usage ;;
  *)        usage; exit 2 ;;
esac
