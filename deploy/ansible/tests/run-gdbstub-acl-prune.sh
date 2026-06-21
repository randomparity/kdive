#!/usr/bin/env bash
# Regression harness for the gdbstub_acl ufw-prune task (issue #616).
#
# The prune task detects stale worker-CIDR ALLOW rules on the raw-TCP gdbstub tier by
# parsing human-formatted `ufw status numbered` output, then `ufw --force delete`s them.
# On Debian/ufw this ACL is the ONLY authorization for those ports, so a regex slip that
# under-matches silently re-opens the over-permission, and one that over-matches drops the
# current worker. This harness drives the REAL prune task in isolation (ansible-playbook
# --tags) against canned fixtures with a fake `ufw`, asserting exactly which rules it deletes.
#
# Per case, all three signals must hold:
#   1. ansible-playbook exits 0,
#   2. the prune task actually ran and reached the pipeline (fake ufw touched the marker
#      on `status numbered`) — so an empty delete log is a real no-op, not a crash/skip,
#   3. the delete log equals the expected line numbers, in descending (highest-first) order.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../../.." && pwd)"
playbook="$here/gdbstub_acl_prune.yml"
fixtures="$here/fixtures"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
install -m 0755 "$here/fake-ufw" "$work/ufw"

export PATH="$work:$PATH"
export ANSIBLE_ROLES_PATH="$repo_root/deploy/ansible/roles"
export ANSIBLE_PYTHON_INTERPRETER="${ANSIBLE_PYTHON_INTERPRETER:-$(command -v python3)}"
export ANSIBLE_NOCOWS=1
export ANSIBLE_LOCALHOST_WARNING=False
export ANSIBLE_INVENTORY_UNPARSED_WARNING=False

fail=0

run_case() {
  local name="$1" fixture="$2" worker_cidr="$3" expected="$4"
  local dir="$work/$name"
  mkdir -p "$dir"
  export FAKE_UFW_FIXTURE="$fixtures/$fixture"
  export FAKE_UFW_DELETE_LOG="$dir/deletes.log"
  export FAKE_UFW_STATUS_MARKER="$dir/status.marker"
  : >"$FAKE_UFW_DELETE_LOG"
  rm -f "$FAKE_UFW_STATUS_MARKER"

  local rc=0
  ansible-playbook "$playbook" -i localhost, \
    --tags gdbstub_acl_prune \
    -e ansible_os_family=Debian \
    -e "worker_cidr=$worker_cidr" \
    -e gdbstub_range=47000:47099 \
    -e gdbstub_acl_tls_port=16514 \
    >"$dir/out.log" 2>&1 || rc=$?

  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$name]: ansible-playbook exited $rc"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  if [ ! -f "$FAKE_UFW_STATUS_MARKER" ]; then
    echo "FAIL [$name]: prune task never queried 'ufw status numbered' (skipped or wedged)"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  local actual
  actual="$(tr '\n' ' ' <"$FAKE_UFW_DELETE_LOG" | sed 's/  */ /g; s/^ //; s/ $//')"
  if [ "$actual" != "$expected" ]; then
    echo "FAIL [$name]: deleted [$actual], expected [$expected]"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  echo "ok   [$name]: deleted [$actual]"
  return 0
}

#         name                  fixture                       worker_cidr   expected(desc)
run_case stale_present "stale_present.numbered" "10.0.0.0/24" "7 6"
run_case steady_state "steady_state.numbered" "10.0.0.0/24" ""
run_case multiple_stale "multiple_stale.numbered" "10.0.0.0/24" "9 8 7 6"
run_case broader_mask "broader_mask.numbered" "10.0.0.0/24" "7 6"
run_case ufw_inactive "ufw_inactive.numbered" "10.0.0.0/24" ""
run_case non_protected_port "non_protected_port.numbered" "10.0.0.0/24" ""
# ADR-0201 (#648): the exclusion is now an exact source-field match, so a stale 110.0.0.0/24
# (which CONTAINS 10.0.0.0/24 as a substring) is pruned instead of surviving the old grep -vF.
run_case substring_collision "substring_collision.numbered" "10.0.0.0/24" "7 6"
# ADR-0201 regression guards. prefix_collision: 10.0.0.0/2 is a substring *of* the worker CIDR
# (symmetric direction). comment_column: a trailing ufw comment must not shift the matched
# source off the current allow — keys the matcher on the From column, not the last token.
run_case prefix_collision "prefix_collision.numbered" "10.0.0.0/24" "7 6"
run_case comment_column "comment_column.numbered" "10.0.0.0/24" "7 6"

if [ "$fail" -ne 0 ]; then
  echo "gdbstub_acl prune harness: FAILED"
  exit 1
fi
echo "gdbstub_acl prune harness: all cases passed"
