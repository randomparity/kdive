#!/usr/bin/env bash
# Environment for the local-libvirt developer setup example.
#
# Sources the canonical live-stack defaults (DB / OIDC / S3 / HTTP / kernel-src), then
# layers the few values this example needs. Source it, don't execute it:
#
#   source examples/local-libvirt/env.sh
#
# Every value is overridable from the caller's environment.
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${example_dir}/../.." && pwd)"

# Reuse the live-stack env so this example tracks the same defaults the rest of the project
# documents. It already exports KDIVE_KERNEL_SRC=~/src/linux, KDIVE_INSTALL_STAGING=
# /var/lib/kdive/install, and the OIDC issuer on :8090 (the host-published mock issuer).
# shellcheck source=scripts/live-stack/env.sh disable=SC1091
source "${repo_root}/scripts/live-stack/env.sh"

# The project this example onboards and mints a token for. One name, threaded through the
# seed step (up.sh) and the token claims (mint-token.sh) so they always agree. `demo` matches
# the walkthrough, `just onboard`, and the Kubernetes demo chart.
export KDIVE_PROJECT="${KDIVE_PROJECT:-demo}"

# Quota/budget seeded for the project. Generous defaults for a single-developer box.
export KDIVE_LIMIT_KCU="${KDIVE_LIMIT_KCU:-1000000}"
export KDIVE_MAX_ALLOC="${KDIVE_MAX_ALLOC:-4}"
export KDIVE_MAX_SYS="${KDIVE_MAX_SYS:-4}"

# KDIVE_TOKEN_TTL (token lifetime) is inherited from the live-stack env sourced above — one
# default (30d) shared with `just onboard`, not a second one that drifts. Override in the
# caller's environment to change it.

# The libvirt endpoint. The fixed worker lifecycle (install-host.sh runs its root installer)
# publishes one operator-owned session daemon in /etc/kdive/live-worker-libvirt.env; every
# libvirt consumer on the host — build-fs, the preflight, the daemons, the workers — must share
# that daemon, so read it the way scripts/live-stack/worker-lifecycle.sh does (parsed as data,
# never sourced as shell). Before the contract is installed the file is absent and the
# live-stack default (qemu:///system) stands; up.sh then fails at the lifecycle witness with the
# fix. An explicit KDIVE_LIBVIRT_URI in the caller's environment wins either way.
# shellcheck source=scripts/live-stack/libvirt-uri.sh
source "${repo_root}/scripts/live-stack/libvirt-uri.sh"
if [[ -z "${KDIVE_LIBVIRT_URI:-}" && -f "${LIBVIRT_ENV}" ]]; then
  KDIVE_LIBVIRT_URI="$(load_published_libvirt_uri)"
  export KDIVE_LIBVIRT_URI
fi
# Session-mode libvirt clients want a runtime dir; an interactive login has one, a bare ssh
# command or nohup may not (the shape .github/workflows/live.yml uses).
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# The local-disk rootfs the System boots — the operator-built kdive-ready guest image. The
# scripts pass this path straight into the provision profile as `rootfs = {kind = "local",
# path = ...}`; it is a file on disk, not an image_catalog object (the catalog models only
# s3/build/staged sources, none of which describe a local-disk file).
export KDIVE_GUEST_IMAGE="${KDIVE_GUEST_IMAGE:-/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2}"

# The interpreter that runs `python -m kdive ...` and the three processes. Defaults to the
# repo venv; override for an installed deployment (e.g. /opt/kdive/.venv/bin/python).
export KDIVE_PYTHON="${KDIVE_PYTHON:-${repo_root}/.venv/bin/python}"

# Per-process logs for the server/reconciler daemons scripts/live-stack/lib.sh starts (the
# workers log to their systemd units). This is state, not config: per the XDG base-dir spec it
# belongs under $XDG_STATE_HOME, and that is already where the kdive login token cache lives
# (kdive.cli.login). Never inside the repo (the lib.sh default is <repo>/.live-stack-logs).
state_home="${XDG_STATE_HOME:-${HOME}/.local/state}/kdive"
export KDIVE_STACK_LOG_DIR="${KDIVE_STACK_LOG_DIR:-${state_home}/local-stack-logs}"
