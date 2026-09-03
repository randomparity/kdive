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

# guestfish stands in for libguestfs, which CI does not have and which needs a real bootable
# image to say anything. The double reproduces guestfish's three observed outcome shapes from a
# sidecar verdict file; see fake-guestfish for how each was established against the real tool.
install -m 0755 "$here/fake-guestfish" "$work/guestfish"
export PATH="$work:$PATH"

export ANSIBLE_ROLES_PATH="$repo_root/deploy/ansible/roles"
export ANSIBLE_PYTHON_INTERPRETER="${ANSIBLE_PYTHON_INTERPRETER:-$(command -v python3)}"
export ANSIBLE_NOCOWS=1
export ANSIBLE_LOCALHOST_WARNING=False
export ANSIBLE_INVENTORY_UNPARSED_WARNING=False

fail=0

# setup_case <name> <staged-volumes-csv|"">
# Builds a fresh pool/artifacts/cache dir and the playbook vars, and returns with $dir, $pool,
# $artifacts and $rendered set. A staged volume is `<catalog-name>` or `<catalog-name>:<verdict>`,
# where verdict is a fake-guestfish sidecar value (conformant | missing | broken); bare names are
# conformant, which is what the pre-#2160 cases mean.
setup_case() {
  local name="$1" staged="$2"
  dir="$work/$name"
  pool="$dir/pool"
  artifacts="$dir/artifacts"
  rendered="$artifacts/localhost-systems.toml"
  # $dir/cache is deliberately NOT created: a fresh managed host has no verdict cache, and the
  # role must create it rather than fail on the first run that would write a verdict.
  mkdir -p "$pool" "$artifacts"

  export FAKE_GUESTFISH_LOG="$dir/guestfish.log"
  : >"$FAKE_GUESTFISH_LOG"

  if [ -n "$staged" ]; then
    local spec vol verdict
    for spec in ${staged//,/ }; do
      vol="${spec%%:*}"
      verdict="${spec#"$vol"}"
      verdict="${verdict#:}"
      : >"$pool/$vol.qcow2"
      [ -n "$verdict" ] && printf '%s\n' "$verdict" >"$pool/$vol.qcow2.verdict"
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
  "remote_libvirt_facts_userland_cache_dir": "$dir/cache",
  "remote_host_fqdn": "host-a.example.test",
  "gdb_addr": "192.168.12.4"
}
JSON
}

play_case() {
  local dir="$1" rc=0
  ansible-playbook "$playbook" -i localhost, -e "@$dir/vars.json" \
    >"$dir/out.log" 2>&1 || rc=$?
  return "$rc"
}

# run_case <name> <staged-volumes-csv|""> <expected-declared-csv|""> <expected-markers-csv|"">
run_case() {
  local name="$1" staged="$2" want_declared="$3" want_markers="$4"
  local dir pool artifacts rendered
  setup_case "$name" "$staged"

  local rc=0
  play_case "$dir" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$name]: ansible-playbook exited $rc"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi

  if [ ! -f "$rendered" ]; then
    echo "FAIL [$name]: no artifact rendered at $rendered"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi

  # Omitting a volume must make the fragment SHORTER, never malformed: several issues consume
  # this file, and a comment block that swallowed a delimiter would surface as an unrelated
  # parse error somewhere downstream.
  if ! python3 -c 'import sys,tomllib; tomllib.load(open(sys.argv[1],"rb"))' "$rendered" \
    2>"$dir/toml.err"; then
    echo "FAIL [$name]: rendered fragment is not valid TOML"
    sed 's/^/    /' "$dir/toml.err"
    sed 's/^/    /' "$rendered"
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

# --- The external-boot guest userland contract (ADR-0590, #2160). ---
#
# A stat proves the volume exists; these prove the role also refuses to declare one whose
# contents cannot satisfy external-boot identity proof. The build-time check cannot cover this:
# it carries the staging copy's guard, so a volume staged before ADR-0590 — or built anywhere
# else — reaches the fragment having never been inspected.

# Staged, inspected, non-conformant. It must be OMITTED exactly like an absent volume: the
# fragment stays valid TOML, just shorter. The rocky default is fine, so no INCOMPLETE.
run_case userland_missing \
  "rocky-10-kdive-remote-base,bare-kdive-remote-base:missing" \
  "rocky-10-kdive-remote-base" "OMITTED"

# The same failure on the DEFAULT image: base_image would name an undeclared [[image]], which
# InventoryDoc.parse rejects at load, so the fragment must say so rather than look complete.
run_case userland_missing_default \
  "rocky-10-kdive-remote-base:missing,bare-kdive-remote-base" \
  "bare-kdive-remote-base" "OMITTED,INCOMPLETE"

# run_failing_case <name> <staged-volumes-csv> <expected-stderr-substring>
# An uninspectable volume is NOT an absent one. Folding it into the omitted set would let a host
# with a broken libguestfs emit an empty-but-valid fragment and break provisioning everywhere
# with nothing naming the cause, so the play must stop and say which volume and why.
run_failing_case() {
  local name="$1" staged="$2" want="$3"
  local dir pool artifacts rendered
  setup_case "$name" "$staged"

  local rc=0
  play_case "$dir" || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "FAIL [$name]: ansible-playbook exited 0; an uninspectable volume must fail the play"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  if ! grep -qF "$want" "$dir/out.log"; then
    echo "FAIL [$name]: failure output does not name the cause (wanted: $want)"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  if [ -f "$rendered" ]; then
    echo "FAIL [$name]: a fragment was rendered despite the volume being uninspectable"
    sed 's/^/    /' "$rendered"
    fail=1
    return 0
  fi
  echo "ok   [$name]: play failed naming the uninspectable volume"
  return 0
}

run_failing_case userland_uninspectable \
  "rocky-10-kdive-remote-base,bare-kdive-remote-base:broken" \
  "could not inspect"

# The gate. An appliance launch per image per site.yml run is not free, so a verdict is cached
# against the volume's size and mtime. Run twice over the same pool and assert the second run
# launched nothing: without this the cache could be written and never read and nothing would say
# so, because both runs would render an identical fragment.
run_cache_case() {
  local name="cache_gates_the_appliance"
  local dir pool artifacts rendered
  setup_case "$name" "rocky-10-kdive-remote-base,bare-kdive-remote-base"

  local rc=0
  play_case "$dir" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$name]: first run exited $rc"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  local first
  first="$(wc -l <"$FAKE_GUESTFISH_LOG")"
  if [ "$first" -ne 2 ]; then
    echo "FAIL [$name]: first run inspected $first volume(s), expected 2"
    fail=1
    return 0
  fi

  : >"$FAKE_GUESTFISH_LOG"
  rc=0
  play_case "$dir" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$name]: second run exited $rc"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  local second
  second="$(wc -l <"$FAKE_GUESTFISH_LOG")"
  if [ "$second" -ne 0 ]; then
    echo "FAIL [$name]: second run inspected $second volume(s); the cached verdict did not gate it"
    sed 's/^/    /' "$FAKE_GUESTFISH_LOG"
    fail=1
    return 0
  fi

  # And the cache must not outlive the volume it describes: restaging changes size and mtime,
  # which is what the key is built from, so the appliance must run again.
  printf 'restaged' >"$pool/bare-kdive-remote-base.qcow2"
  rc=0
  play_case "$dir" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "FAIL [$name]: third run exited $rc"
    sed 's/^/    /' "$dir/out.log"
    fail=1
    return 0
  fi
  local third
  third="$(grep -c 'bare-kdive-remote-base' "$FAKE_GUESTFISH_LOG" || true)"
  if [ "$third" -ne 1 ]; then
    echo "FAIL [$name]: a restaged volume was not re-inspected (saw $third invocation(s))"
    fail=1
    return 0
  fi
  echo "ok   [$name]: 2 inspections, then 0 cached, then 1 after restaging"
  return 0
}

run_cache_case

if [ "$fail" -ne 0 ]; then
  echo "remote_libvirt_facts render harness: FAILED"
  exit 1
fi
echo "remote_libvirt_facts render harness: all cases passed"
