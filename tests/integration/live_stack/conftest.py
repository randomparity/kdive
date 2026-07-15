"""Preflight helpers for the wire-harness smoke tiers (the ADR-0035 §4 skip idiom)."""

from __future__ import annotations

import os
import platform
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable

import pytest

from kdive.diagnostics.guest_arch_accel import (
    kvm_probe_for_uri,
    qemu_system_binary,
    resolved_libvirt_uri,
)
from kdive.mcp.dev_harness import OidcIssuer, oidc_issuer_from_env


def _issuer_reachable(issuer: OidcIssuer) -> bool:
    try:
        with urllib.request.urlopen(issuer.jwks_uri, timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError) as _exc:
        return False


def require_issuer() -> OidcIssuer:
    """Skip unless the mock-OIDC issuer is configured and its JWKS is reachable."""
    base_url = os.environ.get("KDIVE_OIDC_ISSUER")
    if not base_url:
        pytest.skip("KDIVE_OIDC_ISSUER unset; start the issuer (`docker compose up -d oidc`)")
    issuer = oidc_issuer_from_env()
    if not _issuer_reachable(issuer):
        pytest.skip(f"mock-OIDC issuer JWKS unreachable at {issuer.jwks_uri}")
    return issuer


def require_stack() -> str:
    """Skip unless a kdive server base URL is configured (the live_stack tier)."""
    base_url = os.environ.get("KDIVE_STACK_BASE_URL")
    if not base_url:
        pytest.skip("KDIVE_STACK_BASE_URL unset; bring up the stack (see the live-stack runbook)")
    return base_url


def expected_accel(
    arch: str,
    *,
    host_arch: str | None = None,
    kvm_present: Callable[[], bool] | None = None,
) -> str:
    """The accelerator admission persists for an ``arch`` guest on **this** host (#1156).

    Mirrors the production probe (``guest_arch_accel``): the native guest arch under an available
    KVM resolves to ``kvm``; a foreign arch — or a native arch with no ``/dev/kvm`` — resolves to
    ``tcg``. The #1144 proofs assert the *persisted* accel, so the same proof reads ``tcg`` on the
    x86_64 CI host (ppc64le is foreign → TCG) and ``kvm`` on a POWER host (ppc64le is native →
    KVM-HV). ``host_arch``/``kvm_present`` are injected for unit tests; the defaults are
    ``platform.machine()`` and the URI-selected ``/dev/kvm`` probe the worker actually uses.
    """
    resolved_host = host_arch if host_arch is not None else platform.machine()
    if arch != resolved_host:
        return "tcg"
    kvm = kvm_present if kvm_present is not None else kvm_probe_for_uri(resolved_libvirt_uri())
    return "kvm" if kvm() else "tcg"


def require_guest_arch(
    arch: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> None:
    """Skip unless this host can boot ``arch`` guests (its system emulator is on PATH).

    A pure skip gate (ADR-0353): it reuses the #1153 ``qemu_system_binary`` map (single source)
    and resolves **no** accelerator — the provider persists that from libvirt capabilities, and
    the #1144 proof asserts the persisted value. Skips (never errors) when the arch is unknown to
    the map or its emulator is not on PATH — the acceptance "skips cleanly when the host lacks the
    foreign qemu binary" gate.
    """
    binary = qemu_system_binary(arch)
    if binary is None:
        pytest.skip(f"no qemu system emulator known for guest arch {arch!r}")
    if which(binary) is None:
        pytest.skip(
            f"{binary} not on PATH; a {arch} guest boots under TCG emulation on a foreign-arch "
            f"host — install the {arch} qemu system emulator"
        )
