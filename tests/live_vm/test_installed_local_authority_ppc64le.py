"""Installed local external-boot authority native carrier — ppc64le (#2152).

Machine-checkable carrier gate: the test asserts the host is ppc64le with native KVM
acceleration before running the authority proof.  TCG is explicitly rejected — ppc64le
authority proof requires real KVM-HV silicon, not emulation.

Skip discipline:
- ``KDIVE_LIVE_VM_POWER_AUTHORITY_CONFIG`` unset → skip (POWER host not configured).
- Host arch is not ``ppc64le`` → skip (wrong host; use the x86_64 carrier instead).
- Host is ppc64le but ``/dev/kvm`` absent → fail loud (POWER host without KVM-HV is a
  mis-provisioned runner, not "no environment").
"""

import pytest

from tests.live_vm.installed_local_authority_support import (
    run_installed_local_authority_ppc64le_normal_operations,
)


@pytest.mark.live_vm
def test_installed_local_authority_ppc64le_normal_operations() -> None:
    """Prove installed activate, release, and cleanup through MCP and fixed workers on ppc64le."""
    run_installed_local_authority_ppc64le_normal_operations()
