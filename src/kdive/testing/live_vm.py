"""Reusable ``live_vm`` throwaway-domain harness + environment contract (epic #1289, sub-issue A).

This module is the single reusable way to boot a throwaway libvirt domain, wait for a chosen
condition, and tear it down, with the environment quirks encoded once. It is **pytest-free** (the
mechanism ships in ``src/`` like ``kdive.mcp.dev_harness``; the ``pytest.skip`` gates live in
``tests/live_vm``), and imports ``libvirt`` lazily so it loads on a host without it.

Environment contract (what a runner must provide; read here, not per test module):

- ``KDIVE_LIVE_VM_ROOTFS`` — a bootable qcow2 the throwaway family overlays and boots.
- ``KDIVE_LIVE_VM_SYSTEM_ID`` + the ``KDIVE_S3_*`` backend — the provisioned-System family.
- ``KDIVE_LIBVIRT_URI`` — the operator escape hatch; ``resolve_*_contract`` returns it when set,
  else the caller's ``default_uri``. ``contract.libvirt_uri`` is the single source of truth for the
  URI; a test threads it into ``boot_throwaway_domain(mode=...)``.
- libvirt mode is **per test**, not a global pin: traffic-capture uses ``qemu:///session``
  (unprivileged, dodges the ADR-0223 root-readback wall, #1258); snapshot uses ``qemu:///system``.
- Session mode: ``prepare_session_runtime`` redirects ``XDG_CONFIG_HOME`` to a short ``/tmp`` path
  for the QMP UNIX-socket 108-byte limit and restores it in teardown. This mutation is
  process-global, so **one session-mode boot at a time per process** (pytest-xdist workers are
  separate processes with independent ``os.environ``, so xdist is unaffected; nested/threaded
  same-process session boots are not supported).
- Staged overlays are created **beside the rootfs** so they inherit its libvirt access + SELinux
  ``virt_image_t`` label (a rootfs under ``$HOME``/``data_home_t`` is blocked at domain start under
  system mode — name it, do not silently fail).

Skip-vs-fail discipline (a skip must be distinguishable from a pass): required env unset → the gate
skips; env **set but wrong** (missing rootfs file, non-writable parent dir, partial ``KDIVE_S3_*``)
→ the gate fails loud, because a mis-provisioned runner must not masquerade as "no environment".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

LIVE_VM_ROOTFS_ENV = "KDIVE_LIVE_VM_ROOTFS"
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
