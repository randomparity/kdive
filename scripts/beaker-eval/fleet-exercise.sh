#!/usr/bin/env bash
# Gated, mutating exercise of Beaker's allocation and power path against one
# named system, for the issue #1792 evaluation.
#
# Runs loan -> reserve -> power off -> power on -> interrupt -> release, which is
# the sequence stage 1 ran against the x86_64 lab. Its purpose here is to confirm
# the same behaviour through the `lpar` power type, and in particular to record
# how `interrupt` fails on real ppc64le hardware.
#
# This reserves and power-cycles the target. It refuses to run without both
# --target and --confirm-destructive. It does NOT provision: reinstalling a real
# LPAR is a larger commitment than this script should make on its own, so that
# step stays a hand-driven decision (see README.md).
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/beaker-eval/beaker-api.sh
source "$SCRIPT_DIR/beaker-api.sh"

TARGET=""
CONFIRMED=0
SKIP_INTERRUPT=0

usage() {
  cat <<'EOF'
Usage: fleet-exercise.sh --target FQDN --confirm-destructive [options]

Required:
  --target FQDN           The system to exercise. Must be one you may reserve
                          and power-cycle at will.
  --confirm-destructive   Acknowledge that this reserves and power-cycles the
                          target. Without it the script refuses to run.

Options:
  --skip-interrupt        Skip the diagnostic-interrupt step. Use on hardware
                          where an unexpected NMI would be disruptive.
  -h, --help              Show this help.

Required environment: see fleet-probe.sh --help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --target)
    TARGET="${2:?--target requires an FQDN}"
    shift 2
    ;;
  --confirm-destructive)
    CONFIRMED=1
    shift
    ;;
  --skip-interrupt)
    SKIP_INTERRUPT=1
    shift
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "unknown argument: $1" >&2
    usage >&2
    exit 2
    ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  echo "refusing to run: --target is required" >&2
  usage >&2
  exit 2
fi
if [[ "$CONFIRMED" -ne 1 ]]; then
  echo "refusing to run against $TARGET without --confirm-destructive." >&2
  echo "This reserves and power-cycles the system." >&2
  exit 2
fi

: "${BEAKER_COOKIE_JAR:=$(mktemp)}"
export BEAKER_COOKIE_JAR
beaker_require_env
beaker_login

step() { printf '\n--- %s ---\n' "$1"; }

step "Baseline state of $TARGET"
baseline=$(beaker_state "$TARGET")
echo "$baseline" | jq .

# Refuse to disturb a system someone else is holding.
held_by=$(jq -r '.reservation.user // empty' <<<"$baseline")
me=$(beaker_get "/users/+self" 2>/dev/null | jq -r '.user_name // empty')
if [[ -n "$held_by" && "$held_by" != "$me" ]]; then
  echo "refusing: $TARGET is currently reserved by '$held_by' (you are '${me:-unknown}')" >&2
  exit 1
fi

power_type=$(jq -r '.power_type // "unknown"' <<<"$baseline")
echo
echo "power type: $power_type"
if [[ "$power_type" != "lpar" ]]; then
  echo "NOTE: this target is not on the 'lpar' power type, so the Q3 result here"
  echo "      will not speak to the PowerVM path."
fi

released=0
release_hold() {
  [[ "$released" -eq 1 ]] && return 0
  released=1
  step "Releasing hold on $TARGET"
  beaker_patch "/systems/$TARGET/reservations/+current" '{"finish_time":"now"}' >/dev/null 2>&1 ||
    echo "  (no open reservation to release)"
  # NB: loans use the key `finish`, reservations use `finish_time`. Sending the
  # wrong one returns 400 and silently keeps the loan.
  beaker_patch "/systems/$TARGET/loans/+current" '{"finish":"now"}' >/dev/null 2>&1 ||
    echo "  (no open loan to return)"
  beaker_state "$TARGET" | jq .
}
trap release_hold EXIT

step "Q5: borrow, then reserve"
# A manual reservation on an Automated system requires a loan first; the reserve
# endpoint alone returns 400 regardless of ownership.
beaker_post "/systems/$TARGET/loans/" \
  "{\"recipient\":\"$me\",\"comment\":\"KDIVE issue 1792 evaluation\"}" >/dev/null
echo "loan granted"
beaker_post "/systems/$TARGET/reservations/" '{}' |
  jq -c '{type, recipe_id, user: .user.user_name, finish_time}'
echo "expected: type=manual, recipe_id=null, finish_time=null (no expiry)"

step "Q6: power off, and watch the command state machine"
cmd_id=$(beaker_post "/systems/$TARGET/commands/" '{"action":"off"}' | jq -r '.id')
echo "command $cmd_id queued"
beaker_await_command "$TARGET" "$cmd_id" 300 || echo "  -> did not reach Completed"

step "Q6: power on"
cmd_id=$(beaker_post "/systems/$TARGET/commands/" '{"action":"on"}' | jq -r '.id')
echo "command $cmd_id queued"
beaker_await_command "$TARGET" "$cmd_id" 600 || echo "  -> did not reach Completed"

if [[ "$SKIP_INTERRUPT" -eq 1 ]]; then
  step "Q3: interrupt SKIPPED by request"
else
  step "Q3: diagnostic interrupt (expected to fail on the bundled lpar script)"
  echo "Stage 1 showed the API accepts this with HTTP 200 and no capability check,"
  echo "then the lab controller retries 5 times over ~28s before reporting Failed."
  cmd_id=$(beaker_post "/systems/$TARGET/commands/" '{"action":"interrupt"}' | jq -r '.id')
  echo "command $cmd_id queued"
  if beaker_await_command "$TARGET" "$cmd_id" 300; then
    echo
    echo "  RESULT: interrupt COMPLETED. This fleet has a power path that delivers"
    echo "          a diagnostic interrupt -- contradicting the bundled lpar script."
    echo "          Capture the power type and script; it answers req 21."
  else
    echo
    echo "  RESULT: interrupt did not complete, as expected from the bundled script."
    echo "          The message field above carries the script's stderr verbatim."
  fi
fi

step "Q23: audit trail for this exercise"
beaker_get "/systems/$TARGET/activity/?page_size=15" |
  jq -r '.entries[]? | "  \(.created)  \(.user.user_name // "-")  \(.service)  \(.field_name)  \(.action)"'

# release_hold runs via the EXIT trap.
