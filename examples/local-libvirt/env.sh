#!/usr/bin/env bash
# Environment for the local-libvirt developer setup example.
#
# Sources the canonical live-stack defaults (DB / OIDC / S3 / HTTP / kernel-src), then
# layers the few values this example needs. Source it, don't execute it:
#
#   source examples/local-libvirt/env.sh
#
# Every value is overridable from the caller's environment.

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${example_dir}/../.." && pwd)"

# install-host.sh installs uv under ~/.local/bin, and the live-stack scripts this example wraps
# call `uv run` (apply-migrations.sh, onboard.sh). A non-interactive shell (ssh command, nohup,
# cron) skips the profile line the uv installer added, so put that directory on PATH here.
if ! command -v uv >/dev/null 2>&1 && [[ -x "${HOME}/.local/bin/uv" ]]; then
  export PATH="${HOME}/.local/bin:${PATH}"
fi

# Reuse the live-stack env so this example tracks the same defaults the rest of the project
# documents. It already exports KDIVE_KERNEL_SRC=~/src/linux, KDIVE_INSTALL_STAGING=
# /var/lib/kdive/install, and the OIDC issuer on :8090 (the host-published mock issuer).
# shellcheck source=scripts/live-stack/env.sh disable=SC1091
source "${repo_root}/scripts/live-stack/env.sh" || return $?

# The project this example onboards and mints a token for. One name, threaded through the
# seed step (demo-up.sh) and the token claims (mint-token.sh) so they always agree. `demo` matches
# the walkthrough, `just onboard`, and the Kubernetes demo chart.
export KDIVE_PROJECT="${KDIVE_PROJECT:-demo}"

# Quota/budget seeded for the project. Generous defaults for a single-developer box.
export KDIVE_LIMIT_KCU="${KDIVE_LIMIT_KCU:-1000000}"
export KDIVE_MAX_ALLOC="${KDIVE_MAX_ALLOC:-4}"
export KDIVE_MAX_SYS="${KDIVE_MAX_SYS:-4}"

# KDIVE_TOKEN_TTL (token lifetime) is inherited from the live-stack env sourced above — one
# default (30d) shared with `just onboard`, not a second one that drifts. Override in the
# caller's environment to change it.

# KDIVE_LIBVIRT_URI and LIBVIRT_ENV come from the live-stack env sourced above, which resolves
# the published session endpoint for every entry point since #2480 — this example is no longer
# the only path that does it. demo-up.sh still reads both: it refuses to run until the
# lifecycle contract is installed, where the live-stack default (qemu:///system) would stand.

# Session-mode libvirt clients want a runtime dir; an interactive login has one, a bare ssh
# command or nohup may not (the shape .github/workflows/live.yml uses).
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# The local-disk rootfs the System boots — the operator-built kdive-ready guest image. The
# scripts pass this path straight into the provision profile as `rootfs = {kind = "local",
# path = ...}`; it is a file on disk, not an image_catalog object (the catalog models only
# s3/build/staged sources, none of which describe a local-disk file).
#
# This example owns the DEFAULT; scripts/live-stack/env.sh deliberately sets none (#2518), because
# a general entry point cannot pick one family and architecture for every host. The value below is
# this walkthrough's image, so a host that built a different one must override it.
#
# The catalog (fixtures/local-libvirt/rootfs_catalog.toml) names an arch-specific build per host:
# fedora-kdive-ready-44 is x86_64, fedora-kdive-ready-44-ppc64le is ppc64le. Picking the x86_64
# name on every host pointed a ppc64le operator at an image for the wrong architecture (#2669).
# demo-up.sh sources this file and reuses guest_image_name in its "no guest image yet" hint, so
# the two names cannot drift apart.
guest_image_name="fedora-kdive-ready-44"
if [[ "$(uname -m 2>/dev/null || true)" == "ppc64le" ]]; then
  guest_image_name="fedora-kdive-ready-44-ppc64le"
fi
export KDIVE_GUEST_IMAGE="${KDIVE_GUEST_IMAGE:-/var/lib/kdive/rootfs/local/${guest_image_name}.qcow2}"

# The interpreter that runs `python -m kdive ...` and the three processes. Defaults to the
# repo venv; override for an installed deployment (e.g. /opt/kdive/.venv/bin/python).
export KDIVE_PYTHON="${KDIVE_PYTHON:-${repo_root}/.venv/bin/python}"

# Per-process logs for the server/reconciler daemons scripts/live-stack/lib.sh starts (the
# workers log to their systemd units). This is state, not config: per the XDG base-dir spec it
# belongs under $XDG_STATE_HOME, and that is already where the kdive login token cache lives
# (kdive.cli.login). Never inside the repo (the lib.sh default is <repo>/.live-stack-logs).
state_home="${XDG_STATE_HOME:-${HOME}/.local/state}/kdive"
export KDIVE_STACK_LOG_DIR="${KDIVE_STACK_LOG_DIR:-${state_home}/local-stack-logs}"
