#!/usr/bin/env bash
# Regression harness for the guest_base_image build-host admission gate (issue #1629).
#
# A catalog entry may declare `host_distros` — the ansible_distribution values whose base
# repos can actually produce it. bare-kdive-remote-base needs busybox, which RHEL/Rocky/Alma
# do not ship, so a Rocky host must SKIP that entry and still build its other images rather
# than failing the whole play (ADR-0481). The two failure directions both matter: a gate that
# under-admits silently costs a host an image it could have built, and one that over-admits
# puts the original "Unable to find a match: busybox" failure back.
#
# This drives the REAL admission tasks in isolation (ansible-playbook --tags) against the
# REAL catalog in inventory/group_vars/all.yml — no fixture copy of the catalog to drift.
#
# Per passing case, all three signals must hold:
#   1. ansible-playbook exits 0,
#   2. the role's admission assert actually ran (its TASK header is in the log) — so a
#      matching decision is a real evaluation, not a tag slice that skipped the role,
#   3. the recorded buildable/skipped name lists are exactly as expected.
# Per failing case, the run must exit non-zero AND the log must carry the expected message,
# so the failure is the intended assert and not an unrelated error.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../../.." && pwd)"
playbook="$here/guest_base_image_admission.yml"

# The task header that proves the gate itself evaluated under the tag.
gate_task='TASK [guest_base_image : Assert the default image is buildable on this host]'

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

export ANSIBLE_ROLES_PATH="$repo_root/deploy/ansible/roles"
export ANSIBLE_PYTHON_INTERPRETER="${ANSIBLE_PYTHON_INTERPRETER:-$(command -v python3)}"
export ANSIBLE_NOCOWS=1
export ANSIBLE_LOCALHOST_WARNING=False
export ANSIBLE_INVENTORY_UNPARSED_WARNING=False

fail=0

# run_play <dir> <distro> <arch> <images-json> <default> -> rc
run_play() {
  local dir="$1" distro="$2" arch="$3" images="$4" default="$5"
  mkdir -p "$dir"
  # host_images must arrive as a real list: `-e key=[a,b]` would hand the role a string.
  cat >"$dir/vars.json" <<JSON
{
  "ansible_distribution": "$distro",
  "ansible_architecture": "$arch",
  "host_images": $images,
  "host_default_image": "$default",
  "admission_out": "$dir/decision.env"
}
JSON
  local rc=0
  ansible-playbook "$playbook" -i localhost, \
    --tags guest_base_image_admission \
    -e "@$dir/vars.json" \
    >"$dir/out.log" 2>&1 || rc=$?
  return "$rc"
}

# admits <name> <distro> <arch> <images-json> <default> <expected-buildable> <expected-skipped>
admits() {
  local name="$1" distro="$2" arch="$3" images="$4" default="$5"
  local want_build="$6" want_skip="$7"
  local dir="$work/$name" rc=0
  run_play "$dir" "$distro" "$arch" "$images" "$default" || rc=$?

  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$name]: ansible-playbook exited $rc"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  if ! grep -qF "$gate_task" "$dir/out.log"; then
    echo "FAIL [$name]: the admission gate never ran (tag-sliced or role not loaded)"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  local got_build got_skip
  got_build="$(sed -n 's/^buildable=//p' "$dir/decision.env")"
  got_skip="$(sed -n 's/^skipped=//p' "$dir/decision.env")"
  if [ "$got_build" != "$want_build" ] || [ "$got_skip" != "$want_skip" ]; then
    echo "FAIL [$name]: buildable=[$got_build] skipped=[$got_skip]," \
      "expected buildable=[$want_build] skipped=[$want_skip]"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  echo "ok   [$name]: buildable=[$got_build] skipped=[$got_skip]"
  return 0
}

# rejects <name> <distro> <arch> <images-json> <default> <expected-substring>
rejects() {
  local name="$1" distro="$2" arch="$3" images="$4" default="$5" want_msg="$6"
  local dir="$work/$name" rc=0
  run_play "$dir" "$distro" "$arch" "$images" "$default" || rc=$?

  if [ "$rc" -eq 0 ]; then
    echo "FAIL [$name]: expected a non-zero exit, got 0 (the guard did not bite)"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  if ! grep -qF "$want_msg" "$dir/out.log"; then
    echo "FAIL [$name]: exited $rc but without the expected message [$want_msg]"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  echo "ok   [$name]: rejected (exit $rc) with the expected message"
  return 0
}

rocky_pair='["rocky-10-kdive-remote-base","bare-kdive-remote-base"]'
fedora_pair='["fedora-kdive-remote-base-43","bare-kdive-remote-base"]'
ubuntu_pair='["ubuntu-2404-kdive-remote-base","bare-kdive-remote-base"]'

# The issue itself: Rocky cannot produce the bare image (no busybox), so it is skipped and
# the rocky image still builds. Catalog order is preserved in the buildable list.
admits rocky_skips_bare Rocky x86_64 "$rocky_pair" rocky-10-kdive-remote-base \
  "rocky-10-kdive-remote-base" "bare-kdive-remote-base"
# Fedora and Debian-family hosts DO ship busybox — the gate must not over-restrict them.
admits fedora_builds_bare Fedora x86_64 "$fedora_pair" fedora-kdive-remote-base-43 \
  "fedora-kdive-remote-base-43,bare-kdive-remote-base" ""
admits ubuntu_builds_bare Ubuntu x86_64 "$ubuntu_pair" ubuntu-2404-kdive-remote-base \
  "ubuntu-2404-kdive-remote-base,bare-kdive-remote-base" ""
# An entry with no host_distros is unconstrained anywhere.
admits unconstrained_entry_anywhere Rocky x86_64 \
  '["rocky-10-kdive-remote-base"]' rocky-10-kdive-remote-base \
  "rocky-10-kdive-remote-base" ""

# A host whose DEFAULT image it cannot build has no usable [[remote_libvirt]] block at all,
# so that stays fail-fast rather than skipping into an empty catalog.
rejects rocky_bare_default Rocky x86_64 "$rocky_pair" bare-kdive-remote-base \
  "cannot be built on"
# Regression: architecture stays a hard failure, not a skip (a property of the product).
rejects arch_mismatch_still_fails Fedora ppc64le "$fedora_pair" fedora-kdive-remote-base-43 \
  "does not support arch ppc64le"

if [ "$fail" -ne 0 ]; then
  echo "guest_base_image admission harness: FAILED"
  exit 1
fi
echo "guest_base_image admission harness: all cases passed"
