"""Config checks over a Run's uploaded kernel config (ADR-0318, ADR-0322, ADR-0330, ADR-0544).

Three consumers share :func:`load_effective_config` here:

- the crash-capture arming seams (install crashkernel reservation, kdump vmcore fetch) **refuse**
  when the config provably lacks the crash-capture symbols;
- the drgn-live debug seams (``debug.start_session``, live ``introspect.*``) **warn** — never
  refuse — when the config provably lacks debuginfo and no host ``vmlinux`` was uploaded; and
- ``runs.complete_build`` **warns** — never refuses — when the config provably lacks the
  boot-required ``rootfs_mount`` symbols the guest needs to mount its root filesystem.

All fail open (an absent/unreadable/degenerate config yields ``None``: arm/attach/complete as
today). Each seam formats its own envelope from the returned payload.
"""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection

from kdive.artifacts.read_model import effective_config_key
from kdive.kernel_config.fetch import load_effective_config
from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import (
    CRASH_CAPTURE,
    ROOTFS_MOUNT,
    Clause,
    feature_requirement,
)
from kdive.kernel_config.support import (
    built_in_required_symbols,
    missing_symbols,
    unmet_advertised_clauses,
    unmet_clauses,
)
from kdive.serialization import JsonValue

_log = logging.getLogger(__name__)

_EXTERNAL_BUILD_CONTRACT_URI = "resource://kdive/contracts/external-build"

CRASH_CONFIG_REASON = "kernel_missing_crash_config"
_REMEDIATION = (
    "rebuild the kernel with the missing CONFIG_*; on a RHEL-family guest also build in the "
    "advisory crash_capture_rhel_guest set (XFS root, dracut squash/erofs initramfs, "
    "kexec_file_load) or the capture kernel loads and writes nothing "
    f"(see {_EXTERNAL_BUILD_CONTRACT_URI})"
)

MISSING_BOOT_CONFIG_REASON = "kernel_missing_boot_config"
# The prose half has to name =y, not just the symbols (#1860): a symbol listed under
# built_in_required is one the agent can already see in its own .config, so "rebuild with the
# missing CONFIG_*" alone reads as a kdive bug and is followed by re-adding a line that is
# already there. It also names the alternative, because uploading an initrd is the other way out.
_ROOTFS_REMEDIATION = (
    "rebuild the kernel with the missing CONFIG_* so the guest can mount its root filesystem and "
    "boot - EXT4_FS or XFS_FS, whichever your rootfs uses. Build them in (=y): any symbol listed "
    "under built_in_required is set to =m in your config, and the direct-kernel boot mounts root "
    "before a module can load. Uploading an initrd artifact with the build is the alternative "
    f"(see {_EXTERNAL_BUILD_CONTRACT_URI})"
)

NO_EFFECTIVE_CONFIG_REASON = "no_effective_config_uploaded"
_NO_EFFECTIVE_CONFIG_REMEDIATION = (
    "upload the built kernel's effective_config alongside the build (artifacts.create_run_upload) "
    "so runs.complete_build can check the boot-critical EXT4_FS-or-XFS_FS/VIRTIO_BLK symbols the "
    f"guest needs to mount its root filesystem (see {_EXTERNAL_BUILD_CONTRACT_URI})"
)

MISSING_DEBUGINFO_REASON = "missing_debuginfo"
# In-guest drgn-live resolves symbols from the running kernel's BTF (/sys/kernel/btf/vmlinux).
# DWARF built into the kernel .config does NOT help: the DWARF-carrying vmlinux is not on the guest
# rootfs. The only other in-guest source is a host vmlinux uploaded as the Run's debuginfo_ref, so
# the warning keys on BTF specifically and is suppressed when a vmlinux was uploaded.
_BTF_SYMBOL = "DEBUG_INFO_BTF"
# BTF is settable only behind a DWARF choice member, so naming it alone is advice a DEBUG_INFO=n
# kernel cannot follow: BTF has no Kconfig prompt outside `if DEBUG_INFO`, so olddefconfig drops
# CONFIG_DEBUG_INFO_BTF=y out of a fragment over such a config and the rebuild produces the same
# blind kernel (#1855). The symbol this warning *keys* on is unchanged - it asks whether in-guest
# drgn can read /sys/kernel/btf, not whether the kernel carries DWARF - only the advice is.
_DEBUGINFO_REMEDIATION = (
    "enable a DWARF choice member (CONFIG_DEBUG_INFO_DWARF5, CONFIG_DEBUG_INFO_DWARF4 or "
    "CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT) and then CONFIG_DEBUG_INFO_BTF - BTF is offered "
    "only once one of those is set, so olddefconfig drops it from a config that sets none of them "
    "(in-guest drgn reads BTF from /sys/kernel/btf); or upload a matching vmlinux, so drgn can "
    f"resolve symbols (see {_EXTERNAL_BUILD_CONTRACT_URI})"
)

# The static config check above proves BTF is *advertised*, not that the running guest's drgn can
# load it. A drgn-live runtime symbol probe can still find the session blind (guest drgn cannot load
# the kernel's BTF); this is the distinct reason it emits, keyed on the same BTF symbol.
DEBUGINFO_UNLOADABLE_REASON = "debuginfo_unloadable"
_DEBUGINFO_UNLOADABLE_REMEDIATION = (
    "the in-guest drgn could not load the running kernel's BTF even though the config advertised "
    "it (a known limitation of some guest drgn builds); boot a BTF-capable guest image with a "
    f"newer drgn, or upload a matching vmlinux (see {_EXTERNAL_BUILD_CONTRACT_URI})"
)


def _clause_payload(
    config: KernelConfig, unmet: tuple[Clause, ...], *, reason: str, remediation: str
) -> dict[str, JsonValue]:
    """The shared ``{reason, missing, remediation}`` payload, plus one optional key (#1860).

    ``missing`` stays the flat sorted union of every unmet clause's symbols — the ADR-0330 shape
    three seams and their clients share. ``built_in_required`` is the subset of it the config
    enables as ``=m``, and is **absent** when that subset is empty, so a client keying on the
    ADR-0330 shape is unaffected. Built here rather than at each seam so the key cannot appear on
    one payload and not the other.
    """
    payload: dict[str, JsonValue] = {
        "reason": reason,
        "missing": list(missing_symbols(unmet)),
        "remediation": remediation,
    }
    modular = built_in_required_symbols(config, unmet)
    if modular:
        payload["built_in_required"] = list(modular)
    return payload


async def crash_capture_refusal(
    conn: AsyncConnection, run_id: UUID, *, arch: str
) -> dict[str, JsonValue] | None:
    """Refusal ``details`` if the Run's uploaded config lacks crash-capture symbols, else ``None``.

    Returns ``None`` (arm as today) when no config was uploaded or it cannot be read/trusted
    (:func:`load_effective_config` fails open), and when the config fully supports crash capture.
    Otherwise returns ``{reason, missing, remediation}`` — the shared payload both crash seams
    spread into their own refusal envelope, so the reason code and remediation cannot drift.

    ``arch`` is the Run's System's provisioning-profile architecture (``services.runs.steps``'s
    ``system_arch``), and both seams hold a ``System`` to read it off. It is required and typed
    ``str`` rather than ``str | None`` (#1875): ``crash_capture`` now carries an arch-scoped gated
    clause, and a scoped clause the checks cannot place is *skipped*, so a call that omitted the
    arch would turn this refusal into a silent pass. A type error at the call site is the intended
    outcome for a seam that cannot resolve one.
    """
    config = await load_effective_config(conn, run_id)
    if config is None:
        return None
    unmet = unmet_clauses(config, feature_requirement(CRASH_CAPTURE), arch=arch)
    if not unmet:
        return None
    return _clause_payload(config, unmet, reason=CRASH_CONFIG_REASON, remediation=_REMEDIATION)


async def rootfs_mount_warning(
    conn: AsyncConnection,
    run_id: UUID,
    *,
    has_initrd: bool = False,
    guest_builds_initramfs: bool = False,
) -> dict[str, JsonValue] | None:
    """Non-fatal ``kernel_missing_boot_config`` warning for ``runs.complete_build``, or ``None``.

    Returns ``None`` (complete as today) when no ``effective_config`` was uploaded or it cannot be
    read/trusted (:func:`load_effective_config` fails open), and when the config satisfies every
    ``rootfs_mount`` boot clause. Otherwise returns ``{reason, missing, remediation}`` — the payload
    the complete_build handler spreads into its success ``data`` so a kernel that cannot mount its
    root filesystem is no longer silently completed. Keys on the ``rootfs_mount`` *advertised*
    clauses (the feature is never gated); warns, never refuses — the upload always succeeds.

    The root-filesystem clause is the OR-group ``{EXT4_FS, XFS_FS}``: kdive has no guest-family
    axis, so it cannot tell whether the Run targets local-libvirt's ext4 qcow2 or a RHEL-family
    XFS rootfs. The warning therefore means "this kernel can mount *no* root filesystem kdive
    boots", not "this kernel does not match your guest" — a deliberately weak predicate that never
    fires on a kernel carrying either filesystem.

    Both ``rootfs_mount`` clauses require ``=y`` unless something can load a module before root is
    mounted (#1860, #1881). Two facts answer that, and either alone relieves the clause (ADR-0545):
    ``has_initrd``, the build's own uploaded initrd artifact, and ``guest_builds_initramfs``, a
    target that boots through its own bootloader and runs dracut in-guest. Both are carried from
    the caller - the finalized ``BuildStepResult`` and the ``Run`` it already holds - rather than
    re-read here, and both default to the strict reading so a caller that forgets one over-warns.
    """
    config = await load_effective_config(conn, run_id)
    if config is None:
        return None
    unmet = unmet_advertised_clauses(
        config,
        feature_requirement(ROOTFS_MOUNT),
        has_initrd=has_initrd,
        guest_builds_initramfs=guest_builds_initramfs,
    )
    if not unmet:
        return None
    return _clause_payload(
        config, unmet, reason=MISSING_BOOT_CONFIG_REASON, remediation=_ROOTFS_REMEDIATION
    )


async def missing_effective_config_nudge(
    conn: AsyncConnection, run_id: UUID
) -> dict[str, JsonValue] | None:
    """Non-blocking ``no_effective_config_uploaded`` nudge for ``runs.complete_build`` (ADR-0398).

    Returns ``{reason, remediation}`` when the Run has no ``effective_config`` artifact at all —
    the case :func:`rootfs_mount_warning` fails open on, so the EXT4_FS/VIRTIO_BLK boot-config
    advisory could never fire and the agent gets no signal it skipped the check. Returns ``None``
    once a config is present (uploaded), whether or not it is readable or complete: the warning
    path (present but missing symbols) and a plain success (present and complete) already cover
    those. Keys on artifact *presence* — a present-but-unreadable config is treated as provided,
    not absent. If presence cannot be established, returns ``None`` rather than claiming absence.
    Task cancellation still propagates. Advisory only: the completion always succeeds.
    """
    try:
        key = await effective_config_key(conn, run_id)
    except Exception:  # noqa: BLE001 - this advisory must fail open for ordinary lookup faults
        _log.warning(
            "effective_config presence lookup failed for run %s; omitting nudge",
            run_id,
            exc_info=True,
        )
        return None
    if key is not None:
        return None
    return {
        "reason": NO_EFFECTIVE_CONFIG_REASON,
        "remediation": _NO_EFFECTIVE_CONFIG_REMEDIATION,
    }


async def debuginfo_warning(
    conn: AsyncConnection, run_id: UUID, *, has_uploaded_vmlinux: bool
) -> dict[str, JsonValue] | None:
    """Non-fatal ``missing_debuginfo`` warning for a drgn-live seam, or ``None`` (ADR-0322).

    Returns ``None`` (no warning) when a host ``vmlinux``/``debuginfo_ref`` was uploaded (drgn can
    resolve via that vmlinux), when no ``effective_config`` was uploaded or it cannot be
    read/trusted (:func:`load_effective_config` fails open), and when the config provably enables
    BTF (the in-guest symbol source). Otherwise returns ``{reason, missing, remediation}`` — the
    payload the drgn-live seams spread into their response ``data`` so a blind session is no longer
    silently successful. Warns, never refuses: the uploaded-``vmlinux`` path must keep working.
    """
    if has_uploaded_vmlinux:
        return None
    config = await load_effective_config(conn, run_id)
    if config is None or config.is_enabled(_BTF_SYMBOL):
        return None
    return {
        "reason": MISSING_DEBUGINFO_REASON,
        "missing": [_BTF_SYMBOL],
        "remediation": _DEBUGINFO_REMEDIATION,
    }


def debuginfo_unloadable_warning() -> dict[str, JsonValue]:
    """The runtime-probe ``debuginfo_unloadable`` warning payload (extends the static gate above).

    Emitted by the drgn-live introspect seams when a runtime symbol probe proves the in-guest drgn
    cannot resolve a stable kernel symbol, even though the static config check was silent (BTF
    advertised, or no config uploaded) and no host ``vmlinux`` was uploaded — the F1 case the
    ``.config``-based check cannot see. Names ``DEBUG_INFO_BTF`` and points at the same
    remediations, so a client keying on the ``{reason, missing, remediation}`` shape is unaffected.
    """
    return {
        "reason": DEBUGINFO_UNLOADABLE_REASON,
        "missing": [_BTF_SYMBOL],
        "remediation": _DEBUGINFO_UNLOADABLE_REMEDIATION,
    }
