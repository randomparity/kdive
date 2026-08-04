"""Per-provider advertised capture-method coverage: a drift guard on the pinned table.

The table used to live in ``scripts/m2_portability_gate.py``, which rendered it into the
committed M2 portability report. ADR-0543 retired that gate and its report; this guard
outlived them because its subject is different — whether a provider's advertised capture
methods match what the repository claims — rather than diff scope.

The contract is narrower than the module name suggests, and worth stating plainly: changing
an existing provider's advertised set reddens this test, because it asserts two hardcoded keys
against ``build_local_runtime`` and ``build_remote_runtime``. Nothing enumerates the resolver's
registered kinds, so a newly registered provider with no row here stays green and undetected.
Closing that is the registered-kinds completeness assertion #1820 owns.
"""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod
from kdive.providers.assembly.composition import build_local_runtime, build_remote_runtime
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
    remote = build_remote_runtime(secret_registry=registry).support.capture_methods
    local = build_local_runtime(secret_registry=registry).support.capture_methods
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
