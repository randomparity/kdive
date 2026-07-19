"""The live_vm skip/fail gates — the live_vm analogue of require_issuer / require_stack.

Thin pytest wrappers over the pytest-free resolvers in kdive.testing.live_vm: env unset skips,
env set-but-wrong fails loud. Kept in tests/ (not src/) so the shipped mechanism stays pytest-free.
"""

from __future__ import annotations

import pytest

from kdive.testing.live_vm import (
    LiveVmEnvState,
    ProvisionedContract,
    ThrowawayContract,
    resolve_provisioned_contract,
    resolve_throwaway_contract,
)


def require_live_vm_throwaway(
    default_uri: str = "qemu:///system", *, session_required: bool = False
) -> ThrowawayContract:
    """Skip if the throwaway env is absent, fail loud if misconfigured, else return the contract.

    When ``session_required`` is set and the resolved URI is not a ``qemu:///session`` URI, fail
    loud rather than boot a session-only test (#1258 root-readback) into the wrong mode.
    """
    resolution = resolve_throwaway_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    assert resolution.contract is not None
    contract = resolution.contract
    if session_required and not contract.libvirt_uri.startswith("qemu:///session"):
        pytest.fail(
            "this test requires a qemu:///session URI (#1258 root-readback); "
            f"{contract.libvirt_uri!r} was resolved from KDIVE_LIBVIRT_URI"
        )
    return contract


def require_live_vm_provisioned(default_uri: str = "qemu:///system") -> ProvisionedContract:
    """Skip if the provisioned-System env is absent, fail loud if misconfigured, else return it."""
    resolution = resolve_provisioned_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    assert resolution.contract is not None
    return resolution.contract
