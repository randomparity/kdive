"""Per-provider advertised capture-method coverage: a drift guard on the pinned table.

The table used to live in ``scripts/m2_portability_gate.py``, which rendered it into the
committed M2 portability report. ADR-0543 retired that gate and its report; this guard
outlived them because its subject is different — whether a provider's advertised capture
methods match what the repository claims — rather than diff scope.

The contract is narrower than the module name suggests, and worth stating plainly: changing
an existing provider's advertised set reddens this test, because it asserts two hardcoded keys
against a production resolver explicitly enabling local and remote. The pinned scope remains
those two providers, so a new provider with no row here stays green and undetected.
Closing that is the registered-kinds completeness assertion #1820 owns.
"""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry

# Remote advertises console/host_dump/gdbstub/kdump (M2.5 exit; no fadump — that is a local
# pseries opt-in). Local advertises {kdump, fadump, host_dump}: ADR-0208 narrows its
# capture set to the core-producing methods it can actually fetch a vmcore for, dropping the
# non-core console/gdbstub half-truths; HOST_DUMP's seam landed in M2.8 B4 (ADR-0211, libvirt
# domain core dump); FADUMP shares the kdump overlay harvest (ADR-0349, host support gated at
# admission). Local stays the default and remote the opt-in provider (#198).
CAPTURE_COVERAGE: dict[str, frozenset[str]] = {
    "remote-libvirt": frozenset({"console", "host_dump", "gdbstub", "kdump"}),
    "local-libvirt": frozenset({"kdump", "fadump", "host_dump"}),
}


def test_capture_coverage_matches_the_real_advertised_provider_sets() -> None:
    registry = SecretRegistry()
    resolver = ProviderComposition(secret_registry=registry).build_provider_resolver(
        enable_local_libvirt=True,
        enable_remote_libvirt=True,
        enable_fault_inject=False,
        enable_byo_host=False,
    )
    remote = resolver.resolve(ResourceKind.REMOTE_LIBVIRT).support.capture_methods
    local = resolver.resolve(ResourceKind.LOCAL_LIBVIRT).support.capture_methods
    assert CAPTURE_COVERAGE["remote-libvirt"] == frozenset(m.value for m in remote)
    assert CAPTURE_COVERAGE["local-libvirt"] == frozenset(m.value for m in local)
    # Local advertises the core-producing methods it can fetch — kdump (#115/ADR-0203 overlay
    # harvest), fadump (ADR-0349, shares that harvest), and host_dump (B4/ADR-0211). fadump is
    # local-only (not remote).
    assert "kdump" in CAPTURE_COVERAGE["remote-libvirt"]
    assert "fadump" not in CAPTURE_COVERAGE["remote-libvirt"]
    assert "kdump" in CAPTURE_COVERAGE["local-libvirt"]
    assert "fadump" in CAPTURE_COVERAGE["local-libvirt"]
    assert "host_dump" in CAPTURE_COVERAGE["local-libvirt"]


def test_every_pinned_method_is_a_real_capture_method() -> None:
    # Against the enum, not a sibling literal: a pinned row naming a string CaptureMethod does
    # not define is a typo the equality assertion above would report as provider drift.
    vocabulary = {m.value for m in CaptureMethod}
    for provider, methods in CAPTURE_COVERAGE.items():
        unknown = methods - vocabulary
        assert not unknown, f"{provider} pins methods CaptureMethod does not define: {unknown}"
