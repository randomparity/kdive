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

kdive_label_svirt_image() {
  local directory="$1" pattern="${1}(/.*)?"

  command -v getenforce >/dev/null 2>&1 || return 0
  [[ "$(getenforce)" == "Enforcing" ]] || return 0

  if ! command -v semanage >/dev/null 2>&1; then
    echo "SELinux is enforcing but semanage is missing; install policycoreutils-python-utils" >&2
    echo "and label ${directory} svirt_image_t before provisioning." >&2
    return 0
  fi

  # -m modifies an existing rule, -a adds a missing one, and each fails when the other case
  # applies. Trying -m first reaches the same state on a fresh host and on one installed before
  # ADR-0639 (whose rule is still virt_image_t), without parsing `semanage fcontext -l` output.
  sudo semanage fcontext -m -t svirt_image_t "${pattern}" 2>/dev/null ||
    sudo semanage fcontext -a -t svirt_image_t "${pattern}"
  sudo restorecon -R "${directory}"
}
