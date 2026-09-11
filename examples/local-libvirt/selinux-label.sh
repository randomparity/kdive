#!/usr/bin/env bash
# Label a kdive image directory so a confined QEMU domain can use it (ADR-0639, #2424).
#
# svirt_t may read virt_image_t but may not write or map it, and the unprivileged session
# libvirt daemon never performs the dynamic relabel that closes that gap on a privileged
# daemon. So the static label has to be one the confined domain can use: svirt_image_t:s0,
# which every domain reaches by MCS dominance whatever categories libvirt draws for it. A
# privileged daemon relabels from svirt_image_t exactly as it did from virt_image_t, so this
# label is correct under both.
#
# Sourced by install-host.sh and build-image.sh; sourcing it runs nothing.
#
# kdive_label_svirt_image <directory>
#   No-ops (returns 0) off SELinux-enforcing hosts, and when semanage is missing (reporting the
#   fix on stderr instead). Aborts non-zero if semanage or restorecon fails, so a caller under
#   `set -euo pipefail` does not continue as though the label had been applied.

kdive_label_svirt_image() {
  local directory="${1%/}" pattern
  pattern="${directory}(/.*)?"

  command -v getenforce >/dev/null 2>&1 || return 0
  [[ "$(getenforce)" == "Enforcing" ]] || return 0

  if ! command -v semanage >/dev/null 2>&1; then
    echo "SELinux is enforcing but semanage is missing; install policycoreutils-python-utils" >&2
    echo "and label ${directory} svirt_image_t before provisioning (walkthrough Step 6)." >&2
    return 0
  fi

  # -a both adds a missing rule and rewrites an existing one: seobject.fcontextRecords.add()
  # checks the base and local stores first and prints "already defined, modifying instead" before
  # delegating to the modify path, exiting 0 either way. So one call converges a fresh host, a
  # host carrying kdive's pre-ADR-0639 virt_image_t rule, and a host whose rule someone else
  # wrote — no `semanage fcontext -l` parsing and no migrate-then-add split. Verified against the
  # installed implementation on both target families: policycoreutils-python-utils 3.11 (Fedora
  # 44) and 3.10 (Rocky 10.2) carry that branch identically. A non-zero exit therefore means the
  # policy store itself refused the write, which is not something to continue past.
  sudo semanage fcontext -a -t svirt_image_t "${pattern}" || return 1
  sudo restorecon -R "${directory}"
}
