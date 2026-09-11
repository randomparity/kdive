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
#   fix on stderr instead). Aborts non-zero if both the migrate and the add attempt fail — a
#   broken policy store — or if the final restorecon fails, so a caller under `set -euo pipefail`
#   does not continue as though the label had been applied.

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

  # -m modifies an existing rule, -a adds a missing one, and each fails when the other case
  # applies. Trying -m first reaches the same state on a fresh host and on one installed before
  # ADR-0639 (whose rule is still virt_image_t), without parsing `semanage fcontext -l` output.
  # A double failure means the policy store itself is broken (a lock, a concurrent semanage); -a's
  # own message alone ("already defined") would not explain that, so re-emit -m's message too.
  local modify_error
  if ! modify_error="$(sudo semanage fcontext -m -t svirt_image_t "${pattern}" 2>&1)"; then
    if ! sudo semanage fcontext -a -t svirt_image_t "${pattern}"; then
      echo "${modify_error}" >&2
      return 1
    fi
  fi
  sudo restorecon -R "${directory}"
}
