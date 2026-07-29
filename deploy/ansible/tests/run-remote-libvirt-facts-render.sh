#!/usr/bin/env bash
# Regression harness for the remote_libvirt_facts volume confirmation (issue #1629).
#
# `[image.source] kind = "staged"` is a claim that the named volume is already in the host's
# pool. The facts play runs in site.yml, which never builds images (that is the opt-in
# playbooks/image.yml), and guest_base_image skips entries the host cannot build — so the
# role used to declare staged volumes the host may never have produced. A phantom row looks
# like an available image right up until a System tries to boot it (ADR-0481).
#
# This drives the REAL role against a real pool directory: the fixture is just which
# .qcow2 files exist, so no fake binary is needed. Each case asserts on the rendered
# artifact — which [[image]] names it declares, and whether it flags itself incomplete.
#
# Per case, all three signals must hold:
#   1. ansible-playbook exits 0 (the role must not fail a legitimate fresh-host render),
#   2. the artifact was written,
#   3. the declared image names and the OMITTED/INCOMPLETE markers are exactly as expected.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../../.." && pwd)"
playbook="$here/remote_libvirt_facts_render.yml"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

export ANSIBLE_ROLES_PATH="$repo_root/deploy/ansible/roles"
export ANSIBLE_PYTHON_INTERPRETER="${ANSIBLE_PYTHON_INTERPRETER:-$(command -v python3)}"
export ANSIBLE_NOCOWS=1
export ANSIBLE_LOCALHOST_WARNING=False
export ANSIBLE_INVENTORY_UNPARSED_WARNING=False

fail=0

# run_case <name> <staged-volumes-csv|""> <expected-declared-csv|""> <expected-markers-csv|"">
# staged volumes are catalog names; the harness creates <name>.qcow2 in a fresh pool dir.
run_case() {
  local name="$1" staged="$2" want_declared="$3" want_markers="$4"
  local dir="$work/$name"
  local pool="$dir/pool" artifacts="$dir/artifacts"
  mkdir -p "$pool" "$artifacts"

  if [ -n "$staged" ]; then
    local vol
    for vol in ${staged//,/ }; do
      : >"$pool/$vol.qcow2"
    done
  fi

  cat >"$dir/vars.json" <<JSON
{
  "ansible_distribution": "Rocky",
  "ansible_architecture": "x86_64",
  "host_images": ["rocky-10-kdive-remote-base", "bare-kdive-remote-base"],
  "host_default_image": "rocky-10-kdive-remote-base",
  "storage_pool_target": "$pool",
  "pki_artifacts_dir": "$artifacts",
  "remote_host_fqdn": "host-a.example.test",
  "gdb_addr": "192.168.12.4"
}
JSON

  local rc=0
  ansible-playbook "$playbook" -i localhost, -e "@$dir/vars.json" \
    >"$dir/out.log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$name]: ansible-playbook exited $rc"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi

  local rendered="$artifacts/localhost-systems.toml"
  if [ ! -f "$rendered" ]; then
    echo "FAIL [$name]: no artifact rendered at $rendered"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi

  local got_declared got_markers=""
  got_declared="$(sed -n 's/^name = "\(.*-kdive-remote-base.*\)"$/\1/p' "$rendered" |
    paste -sd, -)"
  grep -q '^# OMITTED' "$rendered" && got_markers="OMITTED"
  if grep -q '^# INCOMPLETE' "$rendered"; then
    got_markers="${got_markers:+$got_markers,}INCOMPLETE"
  fi

  if [ "$got_declared" != "$want_declared" ] || [ "$got_markers" != "$want_markers" ]; then
    echo "FAIL [$name]: declared=[$got_declared] markers=[$got_markers]," \
      "expected declared=[$want_declared] markers=[$want_markers]"
    sed 's/^/    /' "$rendered"
    fail=1
    return 0
  fi
  echo "ok   [$name]: declared=[$got_declared] markers=[$got_markers]"
  return 0
}

#        name                staged volumes                        declared                              markers
run_case both_staged "rocky-10-kdive-remote-base,bare-kdive-remote-base" \
  "rocky-10-kdive-remote-base,bare-kdive-remote-base" ""
# The #1629 shape: Rocky skipped the bare build, so its volume is absent and must not be
# declared — but the rocky image is staged, so the fragment is still complete and loadable.
run_case bare_absent "rocky-10-kdive-remote-base" "rocky-10-kdive-remote-base" "OMITTED"
# The default image absent: nothing to declare it against, so the fragment says so.
run_case default_absent "bare-kdive-remote-base" "bare-kdive-remote-base" "OMITTED,INCOMPLETE"
# A fresh host after site.yml but before playbooks/image.yml: no [[image]] at all, and the
# render still succeeds (the documented usage order runs site.yml first).
run_case fresh_host "" "" "OMITTED,INCOMPLETE"

if [ "$fail" -ne 0 ]; then
  echo "remote_libvirt_facts render harness: FAILED"
  exit 1
fi
echo "remote_libvirt_facts render harness: all cases passed"
