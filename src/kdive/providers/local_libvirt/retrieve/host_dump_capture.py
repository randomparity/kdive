"""Local-libvirt live-domain host-dump capture (ADR-0211)."""

from __future__ import annotations

import grp
import os
import stat
from pathlib import Path
from uuid import UUID, uuid4

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve.guestfs import _libvirt_uri, _remove_spool
from kdive.providers.shared.debug_common.core_file import MAX_CORE_BYTES
from kdive.providers.shared.runtime_paths import domain_name_for

_HOST_DUMP_ROOT = Path("/var/lib/kdive/host-dump")
_HOST_DUMP_GROUP = "kdive-live-libvirt"


def _open_host_dump_root(group_id: int) -> int:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("filesystem root must be root-owned without group/world write")
        for part in _HOST_DUMP_ROOT.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != 0:
                raise ValueError("host-dump path must be root-owned")
            if part == _HOST_DUMP_ROOT.name:
                if info.st_gid != group_id or stat.S_IMODE(info.st_mode) != 0o3770:
                    raise ValueError("host-dump root requires shared group and mode 3770")
                if {"system.posix_acl_access", "system.posix_acl_default"} & set(
                    os.listxattr(descriptor)
                ):
                    raise ValueError("host-dump root must not have access or default ACLs")
            elif info.st_mode & 0o022:
                raise ValueError("host-dump ancestor must not be group/world writable")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _prepare_host_dump() -> Path:
    root_fd = directory_fd = -1
    dest: Path | None = None
    try:
        group_id = grp.getgrnam(_HOST_DUMP_GROUP).gr_gid
        root_fd = _open_host_dump_root(group_id)
        name = f"capture-{uuid4().hex}"
        os.mkdir(name, mode=0o700, dir_fd=root_fd)
        dest = _HOST_DUMP_ROOT / name / "vmcore"
        directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        os.fchmod(directory_fd, 0o2710)
        info = os.fstat(directory_fd)
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (
            os.geteuid(),
            group_id,
            0o2710,
        ):
            raise ValueError("host-dump capture directory has unexpected ownership or mode")
        core_fd = os.open(
            "vmcore",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o660,
            dir_fd=directory_fd,
        )
        try:
            os.fchmod(core_fd, 0o660)
            info = os.fstat(core_fd)
            if not stat.S_ISREG(info.st_mode) or (
                info.st_uid,
                info.st_gid,
                stat.S_IMODE(info.st_mode),
            ) != (os.geteuid(), group_id, 0o660):
                raise ValueError("host-dump destination has unexpected ownership or mode")
        finally:
            os.close(core_fd)
        return dest
    except (KeyError, OSError, ValueError) as exc:
        if dest is not None:
            _remove_spool(dest)
        raise CategorizedError(
            "host_dump spool preparation failed; rerun local host preparation or "
            "deploy/systemd/install-live-worker-lifecycle.sh and verify host-dump permissions",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from exc
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _real_host_dump_capture(system_id: UUID) -> Path | None:  # pragma: no cover - live_vm (libvirt)
    """Dump active domain memory to the prepared shared-group spool (ADR-0211, ADR-0774).

    Return a caller-owned core path for streaming and removal, or None for a missing/inactive
    domain. The root must be installed as root:kdive-live-libvirt 3770 through local host
    preparation. Worker-owned 2710 directories and precreated 0660 cores let the separate daemon
    write while preserving worker read/removal rights. The daemon/worker group is trusted with
    core contents; this is not tenant isolation. Missing or unsafe preparation and oversized cores
    raise CONFIGURATION_ERROR; libvirt dump failures retain INFRASTRUCTURE_FAILURE.
    """
    conn = libvirt.open(_libvirt_uri())
    try:
        try:
            domain = conn.lookupByName(domain_name_for(system_id))
        except libvirt.libvirtError:
            return None
        if not domain.isActive():
            return None
        dest = _prepare_host_dump()
        completed = False
        try:
            try:
                domain.coreDumpWithFormat(
                    str(dest),
                    libvirt.VIR_DOMAIN_CORE_DUMP_FORMAT_RAW,
                    libvirt.VIR_DUMP_MEMORY_ONLY,
                )
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    "local host_dump core-dump failed",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    details={"system_id": str(system_id)},
                ) from exc
            size_bytes = dest.stat().st_size
            if size_bytes > MAX_CORE_BYTES:
                raise CategorizedError(
                    "host_dump core exceeds the single-object ceiling",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"size_bytes": size_bytes, "max_bytes": MAX_CORE_BYTES},
                )
            completed = True
            return dest
        finally:
            if not completed:
                _remove_spool(dest)
    finally:
        conn.close()
