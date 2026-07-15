"""Tests for the guest_arch_accel worker-vantage diagnostic (ADR-0352, #1153).

The probe reports, per schedulable guest arch, whether it runs under KVM or is TCG-only,
and the check FAILs only when the host lacks its own native-arch emulator. Every host
seam (``which``, ``kvm_present``, ``host_arch``) is injected so the probe is unit-tested
with no real host; the URI-selected KVM probe is tested directly against injected
filesystem seams (never the real ``/dev/kvm``).
"""

from __future__ import annotations

import asyncio
import os

from kdive import config
from kdive.diagnostics.checks import CheckStatus
from kdive.diagnostics.guest_arch_accel import (
    _QEMU_SYSTEM_BINARY,
    default_guest_arch_accel_probe,
    kvm_probe_for_uri,
    qemu_system_binary,
    resolved_libvirt_uri,
    uri_is_local,
)
from kdive.diagnostics.multiarch_gdb import diagnostic_contribution as local_diagnostics
from kdive.diagnostics.provider_checks import GuestArchAccelCheck
from kdive.domain.errors import ErrorCategory
from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES
from kdive.providers.assembly.diagnostics import diagnostic_provider_contributions

_SUPPORTED = frozenset({"x86_64", "ppc64le"})


def _which(present: dict[str, str]):
    def _find(name: str) -> str | None:
        return present.get(name)

    return _find


def _run(probe):
    return asyncio.run(probe())


def _run_check(check: GuestArchAccelCheck):
    return asyncio.run(check.run())


# --- qemu_system_binary -------------------------------------------------------------


def test_qemu_system_binary_is_arch_asymmetric() -> None:
    # POWER has no qemu-system-ppc64le binary; the map must preserve the ppc64 name.
    assert qemu_system_binary("x86_64") == "qemu-system-x86_64"
    assert qemu_system_binary("ppc64le") == "qemu-system-ppc64"
    assert qemu_system_binary("aarch64") is None


def test_every_supported_arch_has_a_qemu_binary() -> None:
    # Invariant: a new arch added to arch_traits.SUPPORTED_ARCHES must also gain a qemu-binary
    # map entry here, or a native host of that arch would produce a nonsensical "None" FAIL.
    missing = SUPPORTED_ARCHES - _QEMU_SYSTEM_BINARY.keys()
    assert missing == set(), f"SUPPORTED_ARCHES without a qemu-binary map entry: {sorted(missing)}"


def test_arch_supported_but_unmapped_is_treated_unsupported_not_none_fail() -> None:
    # Defensive floor: if the maps ever diverge, a supported-but-unmapped native arch degrades to
    # "unsupported host" (PASS), never a FAIL rendering the literal string "None".
    probe = default_guest_arch_accel_probe(
        host_arch="s390x",
        supported=frozenset({"s390x"}),  # in `supported` but absent from the binary map
        which=_which({}),
        kvm_present=lambda: True,
    )
    result = _run_check(_check_for(probe))
    assert result.status is CheckStatus.PASS
    assert "None" not in result.detail


# --- kvm_probe_for_uri (the URI-selected filesystem seam) ---------------------------


def test_kvm_probe_session_uses_worker_uid_openability() -> None:
    access_calls: list[tuple[str, int]] = []
    exists_calls: list[str] = []
    probe = kvm_probe_for_uri(
        "qemu:///session",
        node="/fake/kvm",
        access=lambda node, mode: access_calls.append((node, mode)) or True,
        exists=lambda node: exists_calls.append(node) or True,
    )
    assert probe() is True
    assert access_calls == [("/fake/kvm", 6)]  # os.R_OK | os.W_OK == 6
    assert exists_calls == []  # session must not fall back to presence


def test_kvm_probe_system_uses_presence() -> None:
    access_calls: list[tuple[str, int]] = []
    exists_calls: list[str] = []
    probe = kvm_probe_for_uri(
        "qemu:///system",
        node="/fake/kvm",
        access=lambda node, mode: access_calls.append((node, mode)) or True,
        exists=lambda node: exists_calls.append(node) or True,
    )
    assert probe() is True
    assert exists_calls == ["/fake/kvm"]
    assert access_calls == []  # system must not require worker-uid openability


def test_kvm_probe_unknown_uri_defaults_to_presence() -> None:
    exists_calls: list[str] = []
    probe = kvm_probe_for_uri(
        "qemu+tls://host/system",
        node="/fake/kvm",
        access=lambda *_a: False,
        exists=lambda node: exists_calls.append(node) or False,
    )
    assert probe() is False
    assert exists_calls == ["/fake/kvm"]


# --- resolved_libvirt_uri (the config-snapshot glue) --------------------------------


def test_uri_is_local_distinguishes_transport_uris() -> None:
    assert uri_is_local("qemu:///system") is True
    assert uri_is_local("qemu:///session") is True
    assert uri_is_local("  qemu:///system  ") is True  # whitespace-tolerant
    assert uri_is_local("qemu+ssh://host/system") is False
    assert uri_is_local("qemu+tls://host/system") is False
    assert uri_is_local("qemu+tcp://host/system") is False


def test_uri_constants_mirror_the_provider_setting() -> None:
    # The env name + default are duplicated (to respect the provider import boundary); a test —
    # not boundary-gated — pins them to the source of truth so a provider default change is caught.
    from kdive.diagnostics.guest_arch_accel import _DEFAULT_URI, _LIBVIRT_URI_ENV
    from kdive.providers.local_libvirt.settings import LIBVIRT_URI

    assert LIBVIRT_URI.name == _LIBVIRT_URI_ENV
    assert LIBVIRT_URI.default == _DEFAULT_URI


def test_remote_target_does_not_fail_on_missing_local_emulator() -> None:
    # A transport URI runs guests on another host; a missing *local* emulator is not a real
    # schedulability failure, so the check must PASS and scope its detail — never a spurious FAIL.
    probe = default_guest_arch_accel_probe(
        host_arch="x86_64",
        supported=_SUPPORTED,
        which=_which({"qemu-system-x86_64": "/u/x"}),  # a local emulator that is the wrong host
        kvm_present=lambda: True,
        target_is_local=False,
    )
    result = _run_check(_check_for(probe))
    assert result.status is CheckStatus.PASS
    assert "targets a remote host" in result.detail
    # The machine-readable data must not present a confident (wrong-host) accel map for a remote
    # target — only the marker, so a --json consumer is not misled.
    assert result.data == {"target_is_local": "false"}


def test_resolved_libvirt_uri_reads_snapshot_then_restores() -> None:
    # Load a snapshot with the session URI set, then confirm the resolver returns it; an unset
    # URI falls back to the system default. Restore the real environment afterwards.
    try:
        config.load({"KDIVE_LIBVIRT_URI": "qemu:///session"})
        assert resolved_libvirt_uri() == "qemu:///session"
        config.load({})  # URI unset → default
        assert resolved_libvirt_uri() == "qemu:///system"
    finally:
        config.load(os.environ)


# --- default_guest_arch_accel_probe (the accel map) ---------------------------------


def test_both_emulators_and_kvm_yields_native_kvm_foreign_tcg() -> None:
    probe = default_guest_arch_accel_probe(
        host_arch="x86_64",
        supported=_SUPPORTED,
        which=_which({"qemu-system-x86_64": "/u/x", "qemu-system-ppc64": "/u/p"}),
        kvm_present=lambda: True,
    )
    report = _run(probe)
    assert report.accel_by_arch == {"ppc64le": "tcg", "x86_64": "kvm"}
    assert report.native_supported is True
    assert report.native_emulator_present is True


def test_native_without_kvm_maps_native_to_tcg() -> None:
    probe = default_guest_arch_accel_probe(
        host_arch="x86_64",
        supported=_SUPPORTED,
        which=_which({"qemu-system-x86_64": "/u/x", "qemu-system-ppc64": "/u/p"}),
        kvm_present=lambda: False,
    )
    report = _run(probe)
    assert report.accel_by_arch == {"ppc64le": "tcg", "x86_64": "tcg"}
    assert report.native_emulator_present is True


def test_native_emulator_absent_is_flagged() -> None:
    probe = default_guest_arch_accel_probe(
        host_arch="ppc64le",
        supported=_SUPPORTED,
        which=_which({"qemu-system-x86_64": "/u/x"}),  # only the foreign emulator
        kvm_present=lambda: True,
    )
    report = _run(probe)
    assert report.native_emulator_present is False
    assert report.native_qemu_binary == "qemu-system-ppc64"
    assert report.accel_by_arch == {"x86_64": "tcg"}  # foreign arch is not native → tcg


def test_unsupported_host_arch_records_only_found_emulators() -> None:
    probe = default_guest_arch_accel_probe(
        host_arch="aarch64",
        supported=_SUPPORTED,
        which=_which({"qemu-system-x86_64": "/u/x"}),
        kvm_present=lambda: True,
    )
    report = _run(probe)
    assert report.native_supported is False
    assert report.native_emulator_present is False
    # aarch64 is not the native match for x86_64, and unsupported anyway → tcg, no crash.
    assert report.accel_by_arch == {"x86_64": "tcg"}


# --- GuestArchAccelCheck (status mapping + detail) ----------------------------------


def _check_for(report_probe) -> GuestArchAccelCheck:
    return GuestArchAccelCheck(provider="local-libvirt", probe=report_probe)


def test_check_passes_and_maps_accel_in_data() -> None:
    probe = default_guest_arch_accel_probe(
        host_arch="x86_64",
        supported=_SUPPORTED,
        which=_which({"qemu-system-x86_64": "/u/x", "qemu-system-ppc64": "/u/p"}),
        kvm_present=lambda: True,
    )
    result = _run_check(_check_for(probe))
    assert result.status is CheckStatus.PASS
    assert result.data == {"ppc64le": "tcg", "x86_64": "kvm"}
    assert "x86_64 (KVM native)" in result.detail
    assert "ppc64le (TCG-only)" in result.detail


def test_check_flags_native_tcg_in_detail_but_still_passes() -> None:
    probe = default_guest_arch_accel_probe(
        host_arch="x86_64",
        supported=_SUPPORTED,
        which=_which({"qemu-system-x86_64": "/u/x"}),
        kvm_present=lambda: False,
    )
    result = _run_check(_check_for(probe))
    assert result.status is CheckStatus.PASS  # degraded, not broken — still provisions
    assert "native arch x86_64 is TCG-only (host KVM unavailable)" in result.detail


def test_check_fails_when_native_emulator_absent() -> None:
    probe = default_guest_arch_accel_probe(
        host_arch="ppc64le",
        supported=_SUPPORTED,
        which=_which({"qemu-system-x86_64": "/u/x"}),
        kvm_present=lambda: True,
    )
    result = _run_check(_check_for(probe))
    assert result.status is CheckStatus.FAIL
    assert result.failure_category is ErrorCategory.MISSING_DEPENDENCY
    assert "qemu-system-ppc64" in result.fix
    assert result.data == {"x86_64": "tcg"}  # the accel map rides along even on FAIL


def test_check_passes_for_unsupported_host_with_no_native_expectation() -> None:
    probe = default_guest_arch_accel_probe(
        host_arch="aarch64",
        supported=_SUPPORTED,
        which=_which({}),  # nothing schedulable
        kvm_present=lambda: True,
    )
    result = _run_check(_check_for(probe))
    assert result.status is CheckStatus.PASS
    assert "no guest arch is schedulable here" in result.detail


# --- contribution wiring ------------------------------------------------------------


def test_guest_arch_accel_is_in_the_single_local_contribution() -> None:
    contribution = local_diagnostics()
    assert contribution.provider == "local-libvirt"
    assert any(isinstance(c, GuestArchAccelCheck) for c in contribution.worker_checks())
    assert "guest_arch_accel" in {d.id for d in contribution.unavailable_worker_checks()}


def test_registered_in_assembly_without_duplicate_local_contribution() -> None:
    contributions = diagnostic_provider_contributions()
    assert [c.provider for c in contributions].count("local-libvirt") == 1
    ids = {d.id for c in contributions for d in c.unavailable_worker_checks()}
    assert "guest_arch_accel" in ids
