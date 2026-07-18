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
# seed step (up.sh) and the token claims (mint-token.sh) so they always agree.
export KDIVE_PROJECT="${KDIVE_PROJECT:-local}"

# Quota/budget seeded for the project. Generous defaults for a single-developer box.
export KDIVE_LIMIT_KCU="${KDIVE_LIMIT_KCU:-1000000}"
export KDIVE_MAX_ALLOC="${KDIVE_MAX_ALLOC:-4}"
export KDIVE_MAX_SYS="${KDIVE_MAX_SYS:-4}"

# KDIVE_TOKEN_TTL (token lifetime) is inherited from the live-stack env sourced above — one
# default (30d) shared with `just onboard`, not a second one that drifts. Override in the
# caller's environment to change it.

# The local-libvirt provider drives system-scope QEMU/KVM domains. qemu:///system is the
# provider default; exported here for visibility because it is the core of this setup.
export KDIVE_LIBVIRT_URI="${KDIVE_LIBVIRT_URI:-qemu:///system}"

# The local-disk rootfs the System boots — the operator-built kdive-ready guest image. The
# scripts pass this path straight into the provision profile as `rootfs = {kind = "local",
# path = ...}`; it is a file on disk, not an image_catalog object (the catalog models only
# s3/build/staged sources, none of which describe a local-disk file).
export KDIVE_GUEST_IMAGE="${KDIVE_GUEST_IMAGE:-/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2}"

# The interpreter that runs `python -m kdive ...` and the three processes. Defaults to the
# repo venv; override for an installed deployment (e.g. /opt/kdive/.venv/bin/python).
export KDIVE_PYTHON="${KDIVE_PYTHON:-${repo_root}/.venv/bin/python}"

# Runtime state (pid file + per-process logs) for the processes up.sh/down.sh manage. This is
# state, not config: per the XDG base-dir spec it belongs under $XDG_STATE_HOME, and that is
# already where the kdive login token cache lives (kdive.cli.login). Never inside the repo.
# KDIVE_STACK_PID_FILE is consumed by examples/local-libvirt/up.sh (written) and down.sh (read).
# KDIVE_STACK_LOG_DIR is consumed by examples/local-libvirt/up.sh and scripts/live-stack/lib.sh.
state_home="${XDG_STATE_HOME:-${HOME}/.local/state}/kdive"
export KDIVE_STACK_PID_FILE="${KDIVE_STACK_PID_FILE:-${state_home}/local-stack.pid}"
export KDIVE_STACK_LOG_DIR="${KDIVE_STACK_LOG_DIR:-${state_home}/local-stack-logs}"
