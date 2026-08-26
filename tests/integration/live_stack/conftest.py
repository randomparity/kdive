"""Preflight helpers for the wire-harness smoke tiers (the ADR-0035 §4 skip idiom)."""

from __future__ import annotations

import os
import platform
import shutil
import urllib.error
import urllib.request
import warnings
from collections.abc import Callable

import pytest

from kdive.diagnostics.guest_arch_accel import (
    kvm_probe_for_uri,
    qemu_system_binary,
    resolved_libvirt_uri,
)
from kdive.mcp.dev_harness import OidcIssuer, oidc_issuer_from_env
from tests.integration.live_stack.skew import (
    POLICY_ENV,
    ProcessSkew,
    SkewPolicy,
    partition,
    probe_stack_skew,
    skew_policy,
)


def _issuer_reachable(issuer: OidcIssuer) -> bool:
    try:
        with urllib.request.urlopen(issuer.jwks_uri, timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError) as _exc:
        return False


# Session-memoized per JWKS URI, for the same reason as _SKEW_CACHE below: require_issuer()
# is called from seven test modules and the fetch it gates on costs up to five seconds, so
# re-probing per test made a slow issuer under suite load indistinguishable from an absent
# one — one test would drop out of a green run and the skip count varied between runs on an
# unchanged tree (#2074, ADR-0580). One answer per process, latched both ways.
_ISSUER_REACHABLE: dict[str, bool] = {}


def require_issuer() -> OidcIssuer:
    """Skip unless the mock-OIDC issuer is configured and its JWKS answered this session.

    Reachability is probed once per JWKS URI and latched. An issuer that answered at
    session start stays reachable as far as this process is concerned, so a later outage
    fails the tests that need it rather than skipping them.
    """
    base_url = os.environ.get("KDIVE_OIDC_ISSUER")
    if not base_url:
        pytest.skip("KDIVE_OIDC_ISSUER unset; start the issuer (`docker compose up -d oidc`)")
    issuer = oidc_issuer_from_env()
    if issuer.jwks_uri not in _ISSUER_REACHABLE:
        _ISSUER_REACHABLE[issuer.jwks_uri] = _issuer_reachable(issuer)
    if not _ISSUER_REACHABLE[issuer.jwks_uri]:
        pytest.skip(f"mock-OIDC issuer JWKS unreachable at {issuer.jwks_uri}")
    return issuer


def require_stack() -> str:
    """Skip unless a kdive server base URL is configured and the stack is not stale.

    Beyond the URL check, this grades the build each app process reports on its aux
    ``/readyz`` against the working tree (ADR-0482 §3, issue #1630) — a stale stack is
    otherwise symptomatically identical to a defect. Only ``stale_restart`` skips by default;
    ``behind``/``diverged``/``unknown`` warn. ``KDIVE_STACK_SKEW_POLICY`` overrides.
    """
    base_url = os.environ.get("KDIVE_STACK_BASE_URL")
    if not base_url:
        pytest.skip("KDIVE_STACK_BASE_URL unset; bring up the stack (see the live-stack runbook)")
    _enforce_stack_freshness(base_url)
    return base_url


# Session-memoized per base URL: require_stack() is called from dozens of tests, and the skew
# answer cannot change while the same processes keep running.
_SKEW_CACHE: dict[str, list[ProcessSkew]] = {}


def _enforce_stack_freshness(base_url: str) -> None:
    """Skip or warn on a stack that predates the working tree (ADR-0482 §3)."""
    policy = skew_policy()
    if policy is SkewPolicy.OFF:
        return
    if base_url not in _SKEW_CACHE:
        _SKEW_CACHE[base_url] = probe_stack_skew(base_url)
    skip, warn = partition(_SKEW_CACHE[base_url], policy)
    for result in warn:
        warnings.warn(f"live-stack version skew — {result}", stacklevel=3)
    if skip:
        pytest.skip(
            "live-stack version skew: "
            + "; ".join(str(r) for r in skip)
            + f" [set {POLICY_ENV}=warn to run anyway]"
        )


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
