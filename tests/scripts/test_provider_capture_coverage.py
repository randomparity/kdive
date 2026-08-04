"""Per-provider advertised capture-method coverage: a drift guard on the pinned table.

The table used to live in ``scripts/m2_portability_gate.py``, which rendered it into the
committed M2 portability report. ADR-0543 retired that gate and its report; this guard
outlived them because its subject is different — whether a provider's advertised capture
methods match what the repository claims — rather than diff scope.

Registering a provider whose advertised set is absent from, or disagrees with, the table
fails here. That is the check epic #1814's exit criterion 8 and #1820 rely on.
"""

from __future__ import annotations

from kdive.providers.assembly.composition import build_local_runtime, build_remote_runtime
from kdive.security.secrets.secret_registry import SecretRegistry

# The CaptureMethod vocabulary: console/host_dump/gdbstub/kdump/fadump (fadump added by
# ADR-0349).
CAPTURE_VOCABULARY = ("console", "host_dump", "gdbstub", "kdump", "fadump")

# Remote advertises console/host_dump/gdbstub/kdump (M2.5 exit, ADR-0084; no fadump — that is
# a local pseries opt-in). Local advertises {kdump, fadump, host_dump}: ADR-0208 narrows its
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


def test_every_advertised_method_is_in_the_known_vocabulary() -> None:
    # A provider advertising a method outside the vocabulary means CaptureMethod grew and this
    # module was not revisited — the same drift the table guards, one level up.
    for provider, methods in CAPTURE_COVERAGE.items():
        unknown = methods - set(CAPTURE_VOCABULARY)
        assert not unknown, f"{provider} advertises methods outside the vocabulary: {unknown}"
