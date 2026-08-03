#!/usr/bin/env bash
# Read-only probe of a Beaker fleet for the issue #1792 evaluation.
#
# Answers the questions that stage 1 (x86_64 lab) could not: the fleet's Beaker
# version, whether ppc64le distro trees are already imported, which power types
# are registered, and whether console capture is deployed. Issues no POST, PATCH
# or DELETE and reserves nothing. See README.md for the stage-1 findings this
# builds on.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/beaker-eval/beaker-api.sh
source "$SCRIPT_DIR/beaker-api.sh"

LAB_CONTROLLER_SSH=""
SAMPLE_SYSTEM=""

usage() {
  cat <<'EOF'
Usage: fleet-probe.sh [options]

Options:
  --lab-controller-ssh USER@HOST  Also inspect the lab controller over SSH
                                  (power scripts, conserver, console log dir).
                                  Read-only; runs cat/ls/rpm -q only.
  --sample-system FQDN            Report one system's capabilities and power
                                  configuration in detail.
  -h, --help                      Show this help.

Required environment:
  BEAKER_URL            e.g. https://beaker.example.com/bkr  (no trailing slash)
  BEAKER_COOKIE_JAR     path to a curl cookie jar

Authentication is either an existing authenticated BEAKER_COOKIE_JAR, or:
  BEAKER_USERNAME       Beaker account name
  BEAKER_PASSWORD_FILE  file containing that account's password

Optional:
  BEAKER_EVIDENCE       path to append redacted request/response records to
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --lab-controller-ssh)
    LAB_CONTROLLER_SSH="${2:?--lab-controller-ssh requires an argument}"
    shift 2
    ;;
  --sample-system)
    SAMPLE_SYSTEM="${2:?--sample-system requires an argument}"
    shift 2
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

: "${BEAKER_COOKIE_JAR:=$(mktemp)}"
export BEAKER_COOKIE_JAR
beaker_require_env
beaker_login

section() { printf '\n=== %s ===\n' "$1"; }

section "Q8: Beaker server version"
# There is no version endpoint; 29.1 renders it in the page footer as
# `<a href="https://beaker-project.org/">Beaker</a> 29.1`.
version=$(curl --silent --cookie "$BEAKER_COOKIE_JAR" "$BEAKER_URL/" |
  grep -oE 'beaker-project\.org/">Beaker</a>[[:space:]]*[0-9][^<]*' |
  grep -oE '[0-9][0-9.]*' | head -1 || true)
if [[ -n "$version" ]]; then
  echo "server version (page footer): $version"
else
  echo "server version: NOT FOUND in the page footer; check the UI manually"
fi
if command -v bkr >/dev/null 2>&1; then
  echo "local bkr client version:     $(bkr --version 2>&1 | head -1)"
fi
echo "upstream for comparison:      29.3 (May 2026 release); default branch python-3"

section "Q2: lab controllers"
beaker_get "/labcontrollers/" |
  jq -r '.entries[]? | "  \(.fqdn)  disabled=\(.disabled)  removed=\(.is_removed)"'

section "Q3: registered power types"
# Stage 1 established that the bundled lpar script refuses `interrupt` outright,
# identically at 29.1, 29.3, python-3 and master. What matters here is whether
# this fleet has registered any additional or replacement type.
beaker_get "/powertypes/" | jq -r '.power_types[]? | "  \(.id)\t\(.name)"'
echo
echo "  Stage-1 result: the bundled 'lpar' script exits 1 on power_mode=interrupt"
echo "  before invoking fence_lpar. A type beyond the bundled 15-17 would be the"
echo "  only way this fleet already has a diagnostic-interrupt path."

# The collection endpoints -- /distrotrees/, /systems/, /free/ -- answer HTML
# even with Accept: application/json. Only per-system /systems/{fqdn}/ is JSON,
# so both sections below need the bkr client.
if command -v bkr >/dev/null 2>&1; then
  section "Q7: ppc64le distro trees"
  bkr distro-trees-list --arch ppc64le --limit 40 2>&1 | head -60 ||
    echo "  bkr distro-trees-list failed; check ~/.beaker_client/config"

  section "Q7/Q15: ppc64le systems visible to this account"
  bkr list-systems --arch ppc64le 2>&1 | head -40 ||
    echo "  bkr list-systems failed; check ~/.beaker_client/config"
else
  section "Q7: ppc64le distro trees and systems"
  echo "  bkr client not on PATH, and Beaker's collection endpoints"
  echo "  (/distrotrees/, /systems/, /free/) return HTML rather than JSON."
  echo "  Install beaker-client, or read both lists from the web UI:"
  echo "    $BEAKER_URL/distrotrees/?tg_param_arch=ppc64le"
  echo "    $BEAKER_URL/systems/?q=arch:ppc64le"
fi

if [[ -n "$SAMPLE_SYSTEM" ]]; then
  section "Sample system: $SAMPLE_SYSTEM"
  beaker_state "$SAMPLE_SYSTEM" | jq .
  echo
  echo "capabilities for the authenticated account:"
  beaker_get "/systems/$SAMPLE_SYSTEM/" |
    jq -c '{can_reserve, can_borrow, can_power, can_view_power,
            can_configure_netboot, can_lend, active_access_policy}'
  echo
  echo "recent command history (the Q6 state machine):"
  beaker_get "/systems/$SAMPLE_SYSTEM/commands/?page_size=5" |
    jq -r '.entries[]? | "  \(.id)\t\(.action)\t\(.status)\t\(.message // "")"'
fi

if [[ -n "$LAB_CONTROLLER_SSH" ]]; then
  section "Lab controller inspection (read-only): $LAB_CONTROLLER_SSH"
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$LAB_CONTROLLER_SSH" 'bash -s' <<'REMOTE' || true
echo "--- beaker packages ---"
rpm -q beaker-lab-controller beaker-common 2>&1 | head -4

echo "--- power scripts present ---"
for d in /etc/beaker/power-scripts /usr/lib/python*/site-packages/bkr/labcontroller/power-scripts; do
  [ -d "$d" ] && { echo "  $d:"; ls "$d" | tr '\n' ' '; echo; }
done

echo "--- the lpar script as deployed (Q3) ---"
for d in /etc/beaker/power-scripts /usr/lib/python*/site-packages/bkr/labcontroller/power-scripts; do
  [ -f "$d/lpar" ] && { echo "  == $d/lpar =="; cat "$d/lpar"; }
done

echo "--- fence_lpar diagnostic-interrupt support (Q3 replacement path) ---"
if command -v fence_lpar >/dev/null 2>&1; then
  fence_lpar --help 2>&1 | grep -iE "dump|nmi|diag|restart|action|-o " | head -12
else
  echo "  fence_lpar not on PATH"
fi

echo "--- conserver / console capture (Q2) ---"
rpm -q conserver 2>&1 | head -1
systemctl is-active conserver 2>&1 | head -1
grep -iE "^CONSOLE_LOGS" /etc/beaker/labcontroller.conf 2>/dev/null || echo "  CONSOLE_LOGS not overridden (default /var/consoles)"
for d in /var/consoles /var/log/beaker/consoles; do
  [ -d "$d" ] && { echo "  $d exists:"; ls "$d" 2>/dev/null | head -5; }
done
REMOTE
fi

section "Summary"
cat <<'EOF'
Stage-1 results that are arch-independent and need no re-testing here:
  Q1  scheduler-free provisioning omits the harness (gated on {% if recipe %})
  Q4  pool access policies enforce; minimum grant set is view, view_power,
      reserve, control_system, loan_self
  Q5  a manual reservation on an Automated system requires a loan first, and
      neither primitive has any expiry or reaper
  Q6  command states are Queued/Running/Completed/Failed/Aborted, with no
      idempotency key and no server-side timeout

Still to confirm here, by hand or via fleet-exercise.sh:
  Q3  whether any HMC operation delivers a diagnostic interrupt, given that the
      bundled lpar script cannot
  Q2  conserver behaviour under contention, if conserver is deployed at all
EOF
