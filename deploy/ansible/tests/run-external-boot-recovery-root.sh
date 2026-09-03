#!/bin/sh
# Clean-host regression harness for the local external-boot recovery roots (#2210, ADR-0586).
#
# Drives the REAL live_vm_host tasks against localhost, unprivileged, by overriding the two
# role variables naming the root and its owner plus the account list -- the same technique
# run-remote-module-appliance.sh uses for its install dir and owner. It proves the created
# shape is exactly what RecoveryMetadataStore's guard accepts, that creation is idempotent,
# and that the verify gate rejects each way the shape can be wrong.
#
# Why the idempotence check greps the PLAY RECAP rather than counting tasks: a --tags value
# that matches nothing produces an EMPTY recap -- no "localhost : ok=..." line at all -- so a
# mistyped tag makes the grep FAIL rather than pass on zero changes. That is the property
# that stops this harness degrading into a silent no-op, which is the usual failure of
# tag-scoped Ansible tests. Verified: a non-matching tag against the real role prints a play
# header and an empty recap. Do not "simplify" the grep to a task count or a bare exit-status
# check -- both pass vacuously when the tag stops matching.
set -eu

test_root=$(mktemp -d)
trap 'chmod -R u+w "$test_root" 2>/dev/null || :; rm -rf "$test_root"' EXIT HUP INT TERM

recovery_root="$test_root/external-boot-recovery"
me=$(id -un)

# The role names each slot's owner AND its group after the account (main.yml does the same
# for the fixed slot directories), so substituting the invoking account for a worker account
# needs a user-private group. Fail loudly rather than skipping: a silent skip here would let
# the whole harness disappear from CI without anyone noticing.
if [ "$(id -gn)" != "$me" ]; then
  echo "FAIL: this harness substitutes the invoking account for a worker account and needs" >&2
  echo "  a user-private group; id -un is '$me' but id -gn is '$(id -gn)'." >&2
  exit 1
fi

# The parent is created root-owned by the role and asserted against the literal root, so an
# unprivileged harness cannot create it. It makes its own 0711 parent and drives only the
# per-slot creation; the parent's own create task is pinned by the deploy contract test, and
# the parent-owner gate is exercised NEGATIVELY below, which proves the arm bites rather
# than merely that it passes.
mkdir -p "$recovery_root"
chmod 0711 "$recovery_root"

cat >"$test_root/vars.json" <<JSON
{
  "live_vm_host_worker_recovery_root": "$recovery_root",
  "live_vm_host_worker_accounts": ["$me"]
}
JSON

ANSIBLE_CONFIG=deploy/ansible/ansible.cfg
ANSIBLE_ROLES_PATH=deploy/ansible/roles
export ANSIBLE_CONFIG ANSIBLE_ROLES_PATH
playbook=deploy/ansible/tests/external_boot_recovery_root.yml
create_tags=external_boot_recovery_root_slots
verify_tags=external_boot_recovery_root_verify

run() {
  ansible-playbook "$playbook" -i localhost, --tags "$1" \
    -e "@$test_root/vars.json" >"$2" 2>&1
}

fail() {
  cat "$2" 2>/dev/null || :
  echo "FAIL: $1" >&2
  exit 1
}

# 1. Creation produces exactly the shape the recovery stores accept.
run "$create_tags" "$test_root/create.log" ||
  fail "creation run failed" "$test_root/create.log"
[ "$(stat -c '%a' "$recovery_root")" = "711" ] ||
  fail "parent mode is $(stat -c '%a' "$recovery_root"), not 711" "$test_root/create.log"
[ "$(stat -c '%a' "$recovery_root/$me")" = "700" ] ||
  fail "slot mode is $(stat -c '%a' "$recovery_root/$me"), not 700" "$test_root/create.log"
[ "$(stat -c '%U' "$recovery_root/$me")" = "$me" ] ||
  fail "slot owner is $(stat -c '%U' "$recovery_root/$me"), not $me" "$test_root/create.log"
echo "ok create: parent 0711, per-slot root 0700 owned by the slot account"

# 2. Idempotence across re-runs.
run "$create_tags" "$test_root/second.log" ||
  fail "second run failed" "$test_root/second.log"
grep -Eq 'changed=0([[:space:]].*)?failed=0([[:space:]]|$)' "$test_root/second.log" ||
  fail "re-run was not idempotent" "$test_root/second.log"
echo "ok idempotent: the second run reported changed=0"

# 3. The health gate accepts a provisioned tree.
run "$verify_tags" "$test_root/verify.log" ||
  fail "health gate rejected a provisioned tree" "$test_root/verify.log"
echo "ok verify: health gate accepts the provisioned tree"

# 4. It rejects a widened slot mode -- what the store refuses at open time.
chmod 0750 "$recovery_root/$me"
if run "$verify_tags" "$test_root/widened.log"; then
  fail "health gate accepted a mode-0750 recovery root" "$test_root/widened.log"
fi
chmod 0700 "$recovery_root/$me"
echo "ok verify: health gate rejects a widened slot mode"

# 5. It rejects a listable parent -- siblings must stay unenumerable.
chmod 0755 "$recovery_root"
if run "$verify_tags" "$test_root/parent.log"; then
  fail "health gate accepted a mode-0755 recovery parent" "$test_root/parent.log"
fi
chmod 0711 "$recovery_root"
echo "ok verify: health gate rejects a listable recovery parent"

# 6. It rejects an absent root, and says so rather than erroring on a missing attribute.
rm -rf "$recovery_root/${me:?}"
if run "$verify_tags" "$test_root/absent.log"; then
  fail "health gate accepted an absent recovery root" "$test_root/absent.log"
fi
grep -q 'must be a mode-0700 directory owned by' "$test_root/absent.log" ||
  fail "absent root did not produce the actionable message" "$test_root/absent.log"
echo "ok verify: health gate rejects an absent recovery root by name"

# 7. It rejects a root owned by someone other than its slot account. Constructed
#    unprivileged by naming a slot the invoking account does not own: the directory exists
#    and is a real 0700 directory, so only the pw_name/gr_name arm can reject it.
mkdir -p "$recovery_root/nobody"
chmod 0700 "$recovery_root/nobody"
cat >"$test_root/foreign.json" <<JSON
{
  "live_vm_host_worker_recovery_root": "$recovery_root",
  "live_vm_host_worker_accounts": ["nobody"]
}
JSON
if ansible-playbook "$playbook" -i localhost, --tags "$verify_tags" \
  -e "@$test_root/foreign.json" >"$test_root/foreign.log" 2>&1; then
  fail "health gate accepted a recovery root owned by the wrong account" \
    "$test_root/foreign.log"
fi
grep -q 'must be a mode-0700 directory owned by' "$test_root/foreign.log" ||
  fail "wrong-owner root did not produce the ownership message" "$test_root/foreign.log"
rmdir "$recovery_root/nobody"
echo "ok verify: health gate rejects a root owned by the wrong account"

# 8. It refuses a symlinked root rather than following it.
mkdir -p "$test_root/elsewhere"
chmod 0700 "$test_root/elsewhere"
ln -s "$test_root/elsewhere" "$recovery_root/$me"
if run "$verify_tags" "$test_root/symlink.log"; then
  fail "health gate followed a symlinked recovery root" "$test_root/symlink.log"
fi
echo "ok verify: health gate refuses a symlinked recovery root"

# 9. Provisioning must REFUSE a symlinked slot root rather than reporting it converged.
#    ansible.builtin.file with state=directory treats a symlink to a directory as already
#    satisfied, so without the pre-create stat/assert the create run exits 0 with changed=0,
#    the health gate stays red, and the fail_msg's advice to rerun provisioning can never
#    clear it. The symlink from case 8 is still in place.
if run "$create_tags" "$test_root/create-symlink.log"; then
  fail "provisioning reported success over a symlinked slot root" \
    "$test_root/create-symlink.log"
fi
grep -q 'must be a real directory, not a symlink' "$test_root/create-symlink.log" ||
  fail "symlinked slot root did not produce the pre-create message" \
    "$test_root/create-symlink.log"
rm -f "$recovery_root/$me"
echo "ok create: provisioning refuses a symlinked slot root instead of converging over it"

# 10. The parent-owner arm is a real constraint, not a restatement of the variable that set
#     the owner. This harness's parent is owned by the invoking account rather than root, so
#     the arm must REJECT it. If that assertion were ever rewritten to compare against a
#     configurable owner variable, this case would start passing and the arm would be dead.
if run "external_boot_recovery_root_verify_parent_owner" "$test_root/parent-owner.log"; then
  fail "parent-owner gate accepted a parent not owned by root" "$test_root/parent-owner.log"
fi
grep -q 'must be owned by root' "$test_root/parent-owner.log" ||
  fail "parent-owner rejection did not name the invariant" "$test_root/parent-owner.log"
echo "ok verify: parent-owner gate rejects a parent not owned by root"
