"""Unit coverage for the expected_accel host-resolution helper (#1156).

The #1144 spine proofs assert the *persisted* accel a System records at provision (ADR-0339). That
value is host-resolved, so the assertion cannot hard-code ``tcg``: a ppc64le guest reads ``tcg`` on
the x86_64 CI host (foreign arch → TCG) and ``kvm`` on a POWER host (native arch → KVM-HV). These
tests pin the three branches — native+KVM, native+no-KVM, foreign — with injected host arch and KVM
probe so they run without a real ``/dev/kvm`` or a POWER host.
"""

from __future__ import annotations

from tests.integration.live_stack.conftest import expected_accel


def test_native_arch_with_kvm_is_kvm() -> None:
    # ppc64le guest on a POWER host with /dev/kvm → KVM-HV (the #1156 native case).
    assert expected_accel("ppc64le", host_arch="ppc64le", kvm_present=lambda: True) == "kvm"


def test_native_arch_without_kvm_is_tcg() -> None:
    # Native arch but no usable /dev/kvm → TCG, never a false KVM claim.
    assert expected_accel("ppc64le", host_arch="ppc64le", kvm_present=lambda: False) == "tcg"


def test_foreign_arch_is_tcg_regardless_of_kvm() -> None:
    # ppc64le guest on an x86_64 host → foreign arch → TCG even if the host has KVM for its own.
    assert expected_accel("ppc64le", host_arch="x86_64", kvm_present=lambda: True) == "tcg"


def test_foreign_arch_does_not_probe_kvm() -> None:
    # The foreign-arch branch short-circuits before the KVM probe: a probe that would raise proves
    # it is never called (the x86_64-host CI path must not depend on a KVM signal).
    def _boom() -> bool:
        raise AssertionError("kvm probe must not run for a foreign guest arch")

    assert expected_accel("x86_64", host_arch="ppc64le", kvm_present=_boom) == "tcg"
