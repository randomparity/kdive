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

The storage-double family (#2164) adds two requirements to that discipline. Its proof defines a
``dir`` pool whose target is a ``tmp_path`` on the client machine, so it accepts only a **local
session URI**: ``KDIVE_LIBVIRT_URI`` is the shared override for every local family and defaults to
``qemu:///system`` for two of them, so an override set for another family would otherwise silently
retarget this one at a mode where the comparison is meaningless. And its probe calls
``listStoragePools()`` as well as ``libvirt.open``, because modern libvirt packages the storage
driver separately from the qemu driver — a host can answer ``open`` and have no storage backend.

That probe **latches once per process, per resolved URI (ADR-0580)**, and latches in both
directions: what is cached is the probe outcome alone, never the resolution, because the
ABSENT-versus-MISCONFIGURED split is decided by whether ``KDIVE_LIBVIRT_URI`` is *set* and that is
not part of the key. On the CI live job there is no skip path by design:
``.github/workflows/live.yml`` always exports ``KDIVE_LIBVIRT_URI``, so a failed probe there is a
mis-provisioned runner and fails loud rather than skipping.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import libvirt
import pytest

LIVE_VM_ROOTFS_ENV = "KDIVE_LIVE_VM_ROOTFS"
LIVE_VM_BZIMAGE_ENV = "KDIVE_LIVE_VM_BZIMAGE"
LIVE_VM_VMLINUX_ENV = "KDIVE_LIVE_VM_VMLINUX"
LIVE_VM_SYSTEM_ID_ENV = "KDIVE_LIVE_VM_SYSTEM_ID"
LIBVIRT_URI_ENV = "KDIVE_LIBVIRT_URI"

# The remote-libvirt family's trigger + required-companion env. The trigger is the qemu+tls:// URI
# (there is no default remote host, so — unlike the local families — KDIVE_LIBVIRT_URI is not the
# lever; a remote run must name its own host). Remote mandates verified mutual TLS: a non-qemu+tls://
# URI, or one carrying no_verify, is a misconfiguration, not "no environment" (ADR-0076, the
# remote-live-stack runbook: no_verify is forbidden). The base-image volume + KDIVE_S3_* + a running
# reconciler are the required companions a remote provider-op proof cannot run without (ADR-0084
# two-phase vmcore, ADR-0095 reconciler-resident console collector).
LIVE_VM_REMOTE_URI_ENV = "KDIVE_LIVE_VM_REMOTE_URI"
LIVE_VM_REMOTE_BASE_IMAGE_ENV = "KDIVE_LIVE_VM_REMOTE_BASE_IMAGE"
LIVE_VM_REMOTE_RECONCILER_ENV = "KDIVE_LIVE_VM_REMOTE_RECONCILER"
_REMOTE_TLS_SCHEME = "qemu+tls://"

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
    """The gdbstub debug family's resolved env: a directly bootable bzImage + a URI.

    The preserve test boots it against an empty scratch disk to force an early VFS panic; the
    stepping proof boots the same image against the throwaway rootfs overlay. Both attach kdive's
    gdbstub and pair this image with ``KDIVE_LIVE_VM_VMLINUX``.
    """

    bzimage: Path
    libvirt_uri: str


@dataclass(frozen=True, slots=True)
class VmlinuxContract:
    """The matching kernel debuginfo used by live gdb-MI and vmcore tests."""

    vmlinux: Path


@dataclass(frozen=True, slots=True)
class ProvisionedContract:
    """The provisioned-System family's resolved environment: a System id + a libvirt URI."""

    system_id: str
    libvirt_uri: str


@dataclass(frozen=True, slots=True)
class RemoteContract:
    """The remote-libvirt family's resolved env: a qemu+tls:// host + the deps a proof needs.

    Remote is the fourth live_vm family and the only one that drives a genuinely *remote*
    ``qemu+tls://`` host the worker shares no filesystem with (ADR-0076). A remote provider-op proof
    needs more than the control URI: the operator-staged base-image volume the provision profile
    feeds into ``base_image_volume`` (ADR-0112), the object store the two-phase vmcore retrieve
    flows through (``s3_endpoint_url`` must be guest-routable, ADR-0084/ADR-0110), and a running
    reconciler, since remote's console collector is reconciler-resident (ADR-0095, ADR-0235). The
    ``reconciler`` value is the operator's presence marker for that daemon (an endpoint or ``"1"``),
    presence-checked — not probed — as ``s3_endpoint_url`` is checked present, not reachable.
    """

    libvirt_uri: str
    base_image: str
    s3_endpoint_url: str
    s3_bucket: str
    reconciler: str


@dataclass(frozen=True, slots=True)
class StorageDoubleContract:
    """The storage-double fidelity family's resolved environment: a local session URI."""

    libvirt_uri: str


@dataclass(frozen=True, slots=True)
class EnvResolution[T]:
    """A resolved env contract: ``state`` plus either ``contract`` (AVAILABLE) or a ``reason``."""

    state: LiveVmEnvState
    contract: T | None = None
    reason: str = ""


def _resolved_uri(default_uri: str) -> str:
    return os.environ.get(LIBVIRT_URI_ENV) or default_uri


def _is_local_session_uri(uri: str) -> bool:
    try:
        parsed = urlsplit(uri)
        query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    if parsed.netloc or parsed.path != "/session" or parsed.fragment:
        return False
    if parsed.scheme == "qemu":
        return not query
    if parsed.scheme != "qemu+unix" or len(query) != 1 or query[0][0] != "socket":
        return False
    return Path(query[0][1]).is_absolute()


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
    """Resolve the gdbstub debug family's env: a directly bootable bzImage + a URI.

    Skip discipline mirrors ``resolve_throwaway_contract``: env unset → ABSENT (skip); set but not a
    readable file → MISCONFIGURED (fail loud). No writable-parent check — callers boot this file
    directly, so its own directory need not be writable.
    """
    raw = os.environ.get(LIVE_VM_BZIMAGE_ENV)
    if not raw:
        return EnvResolution(
            LiveVmEnvState.ABSENT,
            reason=f"{LIVE_VM_BZIMAGE_ENV} unset; point it at the kernel image under test",
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


def resolve_vmlinux_contract() -> EnvResolution[VmlinuxContract]:
    """Resolve the shared matching-vmlinux env gate used by live debug consumers."""
    raw = os.environ.get(LIVE_VM_VMLINUX_ENV)
    if not raw:
        return EnvResolution(
            LiveVmEnvState.ABSENT,
            reason=f"{LIVE_VM_VMLINUX_ENV} unset; point it at matching vmlinux debuginfo",
        )
    vmlinux = Path(raw)
    if not vmlinux.is_file():
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=f"{LIVE_VM_VMLINUX_ENV}={raw} does not point at a readable file",
        )
    return EnvResolution(LiveVmEnvState.AVAILABLE, VmlinuxContract(vmlinux=vmlinux))


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


def resolve_remote_contract() -> EnvResolution[RemoteContract]:
    """Resolve the remote-libvirt family's env: qemu+tls:// URI + base image + S3 + reconciler.

    The trigger is ``KDIVE_LIVE_VM_REMOTE_URI`` (no default remote host exists). Once set, every
    companion is required — a declared intent to run remote with a partial env is a misconfig, not
    "no environment", so it fails loud (mirrors the provisioned family, where a set SYSTEM_ID with
    partial ``KDIVE_S3_*`` is MISCONFIGURED). The URI must be a verified-TLS ``qemu+tls://`` URI: a
    plain ``qemu:///`` URI here, or one carrying ``no_verify``, is the remote-mandates-mutual-TLS
    misconfiguration (ADR-0076; the remote-live-stack runbook forbids ``no_verify``).
    """
    uri = os.environ.get(LIVE_VM_REMOTE_URI_ENV)
    if not uri:
        return EnvResolution(
            LiveVmEnvState.ABSENT,
            reason=(
                f"{LIVE_VM_REMOTE_URI_ENV} unset; point it at a qemu+tls:// host "
                "(see docs/operating/runbooks/remote-live-stack.md)"
            ),
        )
    if not uri.startswith(_REMOTE_TLS_SCHEME):
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=(
                f"{LIVE_VM_REMOTE_URI_ENV}={uri!r} is not a {_REMOTE_TLS_SCHEME} URI; the remote "
                "family mandates verified mutual TLS (ADR-0076)"
            ),
        )
    if "no_verify" in uri:
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=(
                f"{LIVE_VM_REMOTE_URI_ENV}={uri!r} carries no_verify; the remote family forbids it "
                "(the CA must verify the libvirtd server cert — remote-live-stack runbook)"
            ),
        )
    base_image = os.environ.get(LIVE_VM_REMOTE_BASE_IMAGE_ENV)
    reconciler = os.environ.get(LIVE_VM_REMOTE_RECONCILER_ENV)
    companions = {
        LIVE_VM_REMOTE_BASE_IMAGE_ENV: base_image,
        LIVE_VM_REMOTE_RECONCILER_ENV: reconciler,
        **{name: os.environ.get(name) for name in _S3_REQUIRED_ENV},
    }
    missing = [name for name, value in companions.items() if not value]
    if missing:
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=(
                f"{LIVE_VM_REMOTE_URI_ENV} is set but the required remote companions are "
                f"incomplete (missing: {', '.join(sorted(missing))}); the base-image volume, the "
                "guest-routable object store, and a running reconciler are all needed for a remote "
                "provider-op proof (S3 credentials are file-based under KDIVE_SECRETS_ROOT)"
            ),
        )
    assert base_image is not None
    assert reconciler is not None
    return EnvResolution(
        LiveVmEnvState.AVAILABLE,
        RemoteContract(
            libvirt_uri=uri,
            base_image=base_image,
            s3_endpoint_url=companions["KDIVE_S3_ENDPOINT_URL"] or "",
            s3_bucket=companions["KDIVE_S3_BUCKET"] or "",
            reconciler=reconciler,
        ),
    )


def _storage_driver_answers(uri: str) -> bool:
    """Open the URI and list its storage pools, closing the probe connection on every path.

    Only ``libvirt.libvirtError`` counts as a failed probe; anything else is a defect in the test
    environment and propagates. ``listStoragePools()`` returns ``[]`` rather than raising when the
    driver is present with no pools, so this catches a storage driver that *errors*, not one that
    is merely silent — a host that lists pools but cannot define one still errors in the test body,
    and no cheap probe short of running the proof would catch that.
    """
    conn = None
    try:
        conn = libvirt.open(uri)
        conn.listStoragePools()
    except libvirt.libvirtError:
        return False
    finally:
        if conn is not None:
            conn.close()
    return True


# ADR-0580: a skip gate probing a live resource probes once per process and latches the verdict in
# both directions. Keyed by resolved URI so the gate's own tests can vary KDIVE_LIBVIRT_URI, and
# holding the probe outcome ONLY — see resolve_storage_double_contract for why the resolution
# itself must not be cached.
_STORAGE_DOUBLE_PROBE: dict[str, bool] = {}


def resolve_storage_double_contract(default_uri: str) -> EnvResolution[StorageDoubleContract]:
    """Resolve the storage-double family's env: a local session URI whose storage driver answers.

    Three checks, in order. Check 1 (``_is_local_session_uri``) is pure and runs on every call,
    before the latch is consulted. Checks 2 and 3 are the latched probe.

    The ABSENT/MISCONFIGURED choice is made from ``LIBVIRT_URI_ENV`` on **every** call rather than
    cached with the probe outcome. ``_resolved_uri`` returns the same string for an unset variable
    defaulting to ``qemu:///session`` and for one explicitly set to it, so a skip latched under the
    unset case would otherwise be served back under the set case — turning a mis-provisioned runner
    into a silent skip instead of a loud failure.
    """
    uri = _resolved_uri(default_uri)
    if not _is_local_session_uri(uri):
        return EnvResolution(
            LiveVmEnvState.MISCONFIGURED,
            reason=(
                f"the storage-double fidelity family requires a local session URI; {uri!r} was "
                f"resolved from {LIBVIRT_URI_ENV}. Its pool target is a client-side tmp_path, so "
                "the comparison is meaningless in system mode"
            ),
        )
    if uri not in _STORAGE_DOUBLE_PROBE:
        _STORAGE_DOUBLE_PROBE[uri] = _storage_driver_answers(uri)
    if not _STORAGE_DOUBLE_PROBE[uri]:
        if os.environ.get(LIBVIRT_URI_ENV):
            return EnvResolution(
                LiveVmEnvState.MISCONFIGURED,
                reason=(
                    f"{LIBVIRT_URI_ENV}={uri!r} is set but its libvirt storage driver did not "
                    "answer; that is a mis-provisioned runner, not an absent environment"
                ),
            )
        return EnvResolution(
            LiveVmEnvState.ABSENT,
            reason=(
                f"no libvirt session daemon with a storage driver answered at {uri!r}; "
                f"set {LIBVIRT_URI_ENV} to point at one"
            ),
        )
    return EnvResolution(LiveVmEnvState.AVAILABLE, StorageDoubleContract(libvirt_uri=uri))


def require_live_vm_throwaway(
    default_uri: str = "qemu:///system", *, session_required: bool = False
) -> ThrowawayContract:
    """Skip if the throwaway env is absent, fail loud if misconfigured, else return the contract.

    When ``session_required`` is set, accept only a local session URI: either ``qemu:///session``
    or ``qemu+unix:///session`` with one absolute socket query. Fail loud rather than boot a
    session-only test (#1258 root-readback) into the wrong mode.
    """
    resolution = resolve_throwaway_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    assert resolution.contract is not None
    contract = resolution.contract
    if session_required and not _is_local_session_uri(contract.libvirt_uri):
        pytest.fail(
            "this test requires a local qemu session URI (#1258 root-readback); "
            f"{contract.libvirt_uri!r} was resolved from KDIVE_LIBVIRT_URI"
        )
    return contract


def require_live_vm_bzimage(default_uri: str = "qemu:///session") -> BzimageContract:
    """Skip if the bzImage env is absent, fail loud if it is set-but-not-a-file, else return it.

    The default URI is ``qemu:///session`` so the non-root live runner owns the domain and its
    readback artifacts; ``KDIVE_LIBVIRT_URI`` is the operator override, as for every family.
    """
    resolution = resolve_bzimage_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    assert resolution.contract is not None
    return resolution.contract


def require_live_vm_vmlinux() -> VmlinuxContract:
    """Skip if matching debuginfo is absent, fail loud if misconfigured, else return it."""
    resolution = resolve_vmlinux_contract()
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


def require_live_vm_storage_double(
    default_uri: str = "qemu:///session",
) -> StorageDoubleContract:
    """Skip if no session daemon answers, fail loud if a declared one does not, else return it.

    Returns the URI rather than the open connection: handing back a live connection would put its
    lifetime in the caller's hands across a ``pytest.skip`` boundary and would not fit the pure
    ``EnvResolution[T]`` shape the other five families share. The second open costs nothing in a
    live-tier test.
    """
    resolution = resolve_storage_double_contract(default_uri)
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    assert resolution.contract is not None
    return resolution.contract


def require_live_vm_remote() -> RemoteContract:
    """Skip if the remote env is absent, fail loud if it is set-but-incomplete, else return it.

    The gate the remote ``live_vm`` family threads: an unset ``KDIVE_LIVE_VM_REMOTE_URI`` means
    this host is not set up for the remote tier (skip); a set URI with a wrong scheme,
    ``no_verify``, or a missing companion (base image / ``KDIVE_S3_*`` / reconciler) means a
    mis-provisioned host that must not masquerade as "no environment" (fail loud).
    """
    resolution = resolve_remote_contract()
    if resolution.state is LiveVmEnvState.ABSENT:
        pytest.skip(resolution.reason)
    if resolution.state is LiveVmEnvState.MISCONFIGURED:
        pytest.fail(resolution.reason)
    assert resolution.contract is not None
    return resolution.contract
