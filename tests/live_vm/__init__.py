"""The live_vm environment contract + skip/fail gates (test-environment config).

The ``KDIVE_LIVE_VM_*`` / ``KDIVE_LIBVIRT_URI`` / ``KDIVE_S3_*`` reads live here in ``tests/`` — not
in the shipped ``kdive.testing.live_vm`` mechanism — so the ADR-0087 config-env guard (which
reserves ``KDIVE_*`` reads in ``src/`` for ``kdive.config``) is not tripped by test-only env vars.
This module resolves each family's env into a typed contract and exposes the ``require_live_vm_*``
gates, the live_vm analogue of ``require_issuer`` / ``require_stack`` / ``require_guest_arch``.

Skip-vs-fail discipline (a skip must be distinguishable from a pass): required env unset → the gate
skips; env **set but wrong** (missing rootfs file, non-writable parent dir, partial ``KDIVE_S3_*``)
→ the gate fails loud, because a mis-provisioned runner must not masquerade as "no environment".
``KDIVE_LIBVIRT_URI`` is the operator escape hatch — the resolved ``contract.libvirt_uri`` is the
single source of truth a test threads into ``boot_throwaway_domain(mode=...)``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest

LIVE_VM_ROOTFS_ENV = "KDIVE_LIVE_VM_ROOTFS"
LIVE_VM_BZIMAGE_ENV = "KDIVE_LIVE_VM_BZIMAGE"
LIVE_VM_SYSTEM_ID_ENV = "KDIVE_LIVE_VM_SYSTEM_ID"
LIBVIRT_URI_ENV = "KDIVE_LIBVIRT_URI"

# The object-store env a provisioned-System live run needs. Verified against
# src/kdive/config/core_settings.py: KDIVE_S3_ENDPOINT_URL and KDIVE_S3_BUCKET are the required
# env settings; KDIVE_S3_REGION is defaulted (not required). S3 *credentials* are NOT env vars —
# they are file-based under KDIVE_SECRETS_ROOT (ADR-0089), so credential completeness is out of
# this resolver's env scope; the resolver checks only that the endpoint + bucket env is present.
_S3_REQUIRED_ENV = ("KDIVE_S3_ENDPOINT_URL", "KDIVE_S3_BUCKET")


class LiveVmEnvState(Enum):
    """Whether a live_vm family's required environment is present, absent, or set-but-wrong."""

    AVAILABLE = "available"
    ABSENT = "absent"
    MISCONFIGURED = "misconfigured"


@dataclass(frozen=True, slots=True)
class ThrowawayContract:
    """The throwaway-domain family's resolved environment: a bootable rootfs + a libvirt URI."""

    rootfs: Path
    libvirt_uri: str


@dataclass(frozen=True, slots=True)
class BzimageContract:
    """The gdbstub-preserve debug family's resolved env: an early-panicking bzImage + a URI.

    The debug tests (#747/#1255) boot this bare kernel against an empty scratch disk to force an
    early VFS panic, then attach kdive's gdbstub — so they key off a raw ``bzImage``, not the
    bootable rootfs the throwaway family stages an overlay on (ADR-0392).
    """

    bzimage: Path
    libvirt_uri: str


@dataclass(frozen=True, slots=True)
class ProvisionedContract:
    """The provisioned-System family's resolved environment: a System id + a libvirt URI."""

    system_id: str
    libvirt_uri: str


@dataclass(frozen=True, slots=True)
class EnvResolution[T]:
    """A resolved env contract: ``state`` plus either ``contract`` (AVAILABLE) or a ``reason``."""

    state: LiveVmEnvState
    contract: T | None = None
    reason: str = ""


def _resolved_uri(default_uri: str) -> str:
    return os.environ.get(LIBVIRT_URI_ENV) or default_uri


def resolve_throwaway_contract(default_uri: str) -> EnvResolution[ThrowawayContract]:
    """Resolve the throwaway-domain family's env: rootfs + libvirt URI (see module docstring)."""
    raw = os.environ.get(LIVE_VM_ROOTFS_ENV)
    if not raw:
        return EnvResolution(
            LiveVmEnvState.ABSENT,
            reason=f"{LIVE_VM_ROOTFS_ENV} unset; point it at a bootable rootfs qcow2",
        )
    rootfs = Path(raw)
    if not rootfs.is_file():
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=f"{LIVE_VM_ROOTFS_ENV}={raw} does not point at a readable file",
        )
    if not os.access(rootfs.parent, os.W_OK):
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=(
                f"{LIVE_VM_ROOTFS_ENV}'s parent dir {rootfs.parent} is not writable — the boot "
                "stages a qcow2 overlay beside the rootfs (which must also be virt_image_t-labeled "
                "under system mode); use a writable, correctly-labeled staging dir"
            ),
        )
    return EnvResolution(
        LiveVmEnvState.AVAILABLE,
        ThrowawayContract(rootfs=rootfs, libvirt_uri=_resolved_uri(default_uri)),
    )


def resolve_bzimage_contract(default_uri: str) -> EnvResolution[BzimageContract]:
    """Resolve the gdbstub-preserve debug family's env: an early-panicking bzImage + a URI.

    Skip discipline mirrors ``resolve_throwaway_contract``: env unset → ABSENT (skip); set but not a
    readable file → MISCONFIGURED (fail loud). No writable-parent check — this family boots the
    kernel directly and the caller stages its scratch disk under the pytest ``tmp_path``, so the
    bzImage's own directory need not be writable.
    """
    raw = os.environ.get(LIVE_VM_BZIMAGE_ENV)
    if not raw:
        return EnvResolution(
            LiveVmEnvState.ABSENT,
            reason=f"{LIVE_VM_BZIMAGE_ENV} unset; point it at an early-panicking kernel image",
        )
    bzimage = Path(raw)
    if not bzimage.is_file():
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=f"{LIVE_VM_BZIMAGE_ENV}={raw} does not point at a readable file",
        )
    return EnvResolution(
        LiveVmEnvState.AVAILABLE,
        BzimageContract(bzimage=bzimage, libvirt_uri=_resolved_uri(default_uri)),
    )


def resolve_provisioned_contract(default_uri: str) -> EnvResolution[ProvisionedContract]:
    """Resolve the provisioned-System family's env: System id + S3 (see module docstring)."""
    system_id = os.environ.get(LIVE_VM_SYSTEM_ID_ENV)
    if not system_id:
        return EnvResolution(
            LiveVmEnvState.ABSENT,
            reason=f"{LIVE_VM_SYSTEM_ID_ENV} unset; provision a System and export its id",
        )
    missing = [name for name in _S3_REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=(
                f"{LIVE_VM_SYSTEM_ID_ENV} is set but the required object store env is incomplete "
                f"(missing: {', '.join(missing)}); S3 credentials themselves are file-based under "
                "KDIVE_SECRETS_ROOT, not env"
            ),
        )
    return EnvResolution(
        LiveVmEnvState.AVAILABLE,
        ProvisionedContract(system_id=system_id, libvirt_uri=_resolved_uri(default_uri)),
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


def require_live_vm_bzimage(default_uri: str = "qemu:///session") -> BzimageContract:
    """Skip if the bzImage env is absent, fail loud if it is set-but-not-a-file, else return it.

    The default URI is ``qemu:///session`` because the gdbstub-preserve debug boot needs no root
    (ADR-0223 root-readback); ``KDIVE_LIBVIRT_URI`` is the operator override, as for every family.
    """
    resolution = resolve_bzimage_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    assert resolution.contract is not None
    return resolution.contract


def require_live_vm_provisioned(default_uri: str = "qemu:///system") -> ProvisionedContract:
    """Skip if the provisioned-System env is absent, fail loud if misconfigured, else return it."""
    resolution = resolve_provisioned_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    assert resolution.contract is not None
    return resolution.contract
