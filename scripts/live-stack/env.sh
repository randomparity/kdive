#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
default_database_url="postgresql://kdive:kdive@localhost:5432/kdive" # pragma: allowlist secret

export KDIVE_DATABASE_URL="${KDIVE_DATABASE_URL:-${default_database_url}}"
export KDIVE_OIDC_ISSUER="${KDIVE_OIDC_ISSUER:-http://localhost:8090/default}"
export KDIVE_OIDC_JWKS_URI="${KDIVE_OIDC_JWKS_URI:-http://localhost:8090/default/jwks}"
export KDIVE_OIDC_AUDIENCE="${KDIVE_OIDC_AUDIENCE:-kdive}"
# On QEMU-emulated ppc64le hosts the maven builder stage's JVM TLS is unreliable
# (reproducible bad_record_mac / Tag mismatch fetching runtime jars from Maven Central,
# with `curl` from the same container succeeding — a JDK-on-emulated-POWER crypto path
# defect, not a network or compose issue). Real POWER hosts build fine; only emulation
# is affected. Default KDIVE_OIDC_IMAGE to the published ppc64le+amd64 mirror (ADR-0358)
# just for that case so the stack comes up without hitting the broken build path.
# Explicit KDIVE_OIDC_IMAGE (including empty) overrides this and is honored verbatim.
if [[ -z "${KDIVE_OIDC_IMAGE+set}" && "$(uname -m 2>/dev/null || true)" == "ppc64le" ]] &&
  grep -q 'emulated by qemu' /sys/firmware/devicetree/base/model 2>/dev/null; then
  export KDIVE_OIDC_IMAGE="ghcr.io/randomparity/mock-oauth2-server@sha256:e11ba633538714499356765720c05ef57ecb0ac70db4ca780f6a44d2e49a070a"
fi
export KDIVE_S3_ENDPOINT_URL="${KDIVE_S3_ENDPOINT_URL:-http://localhost:9000}"
export KDIVE_S3_BUCKET="${KDIVE_S3_BUCKET:-kdive-artifacts}"
export KDIVE_S3_REGION="${KDIVE_S3_REGION:-us-east-1}"
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-minioadmin}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-minioadmin}"
export KDIVE_HTTP_HOST="${KDIVE_HTTP_HOST:-127.0.0.1}"
export KDIVE_HTTP_PORT="${KDIVE_HTTP_PORT:-8000}"
export KDIVE_STACK_BASE_URL="${KDIVE_STACK_BASE_URL:-http://${KDIVE_HTTP_HOST}:${KDIVE_HTTP_PORT}/mcp}"
export KDIVE_BUILD_WORKSPACE="${KDIVE_BUILD_WORKSPACE:-${repo_root}/.live-build}"
export KDIVE_BUILD_COMPONENT_ROOTS="${KDIVE_BUILD_COMPONENT_ROOTS:-${repo_root}/fixtures/local-libvirt:${repo_root}/.live-components}"
export KDIVE_INSTALL_STAGING="${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"
# KDIVE_KERNEL_SRC: warm-tree kernel source for local builds. An explicit value is honored
# verbatim. The convenience default ${HOME}/src/linux is HOME-relative, so a privileged restart
# ($HOME -> /root) would silently re-point it to a nonexistent /root/src/linux and every build would
# fail configuration_error with no signal until attempted (#701). So the default is only exported
# when it resolves to an existing directory; otherwise KDIVE_KERNEL_SRC is left unset, which
# ops.diagnostics surfaces as an honest local_kernel_src FAIL instead of a misleading green.
if [[ -n "${KDIVE_KERNEL_SRC:-}" ]]; then
  export KDIVE_KERNEL_SRC
elif [[ -n "${HOME:-}" && -d "${HOME}/src/linux" ]]; then
  export KDIVE_KERNEL_SRC="${HOME}/src/linux"
fi
