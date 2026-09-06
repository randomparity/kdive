#!/usr/bin/env python3
"""Move one explicitly selected disposable live fixture onto the private authority daemon."""

from __future__ import annotations

# ruff: noqa: E402, I001 -- provider imports must follow the authority config snapshot below.

import json
import os
import pwd
import shutil
import stat
import sys
from pathlib import Path
from uuid import UUID

# This script is the one explicitly selected disposable authority fixture. Configuration takes a
# process-local snapshot as provider runtime paths import, so all fixed authority settings must
# precede every provider import.
_AUTHORITY = "kdive-provider-authority"
_WORKER_URI = "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"
_AUTHORITY_URI = "qemu+unix:///system?socket=/run/kdive/provider-authority/libvirt/libvirt-sock"
_WORKER_ROOTFS_ROOT = Path("/var/lib/kdive/rootfs")
_AUTHORITY_ROOTFS_ROOT = Path("/var/lib/kdive/provider-authority/rootfs")
_FIXTURE_BASE_NAME = "live-vm-provisioned-rootfs.qcow2"
_MAX_FIXTURE_BASE_BYTES = 16 * 1024**3
_COPY_CHUNK_BYTES = 1024 * 1024
os.environ.update(
    {
        "KDIVE_LIBVIRT_URI": _AUTHORITY_URI,
        "KDIVE_LIBVIRT_ROOTFS_ROOT": str(_AUTHORITY_ROOTFS_ROOT),
        "KDIVE_LIBVIRT_CONSOLE_ROOT": "/var/lib/kdive/provider-authority/console",
    }
)

import libvirt

from kdive.components.references import LocalComponentRef
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.local_libvirt.lifecycle.storage import baseline_dir, overlay_path
from kdive.providers.shared.runtime_paths import console_log_path, domain_name_for, overlay_name


def _validate_standalone_qcow2(descriptor: int, size: int) -> None:
    """Reject a malformed image or one whose bytes depend on another caller-selected file."""
    header = os.pread(descriptor, 104, 0)
    if len(header) < 72 or header[:4] != b"QFI\xfb":
        raise ValueError("authority fixture base is not a qcow2 image")
    version = int.from_bytes(header[4:8], "big")
    minimum = 104 if version == 3 else 72
    if version not in (2, 3) or size < minimum or len(header) < minimum:
        raise ValueError("authority fixture base has an unsupported qcow2 header")
    backing_offset = int.from_bytes(header[8:16], "big")
    backing_size = int.from_bytes(header[16:20], "big")
    if backing_offset != 0 or backing_size != 0:
        raise ValueError("authority fixture base must not name a qcow2 backing file")
    if version == 3:
        incompatible_features = int.from_bytes(header[72:80], "big")
        if incompatible_features & 4:
            raise ValueError("authority fixture base must not use an external data file")


def _copy_exact(source: int, destination: int, size: int) -> None:
    remaining = size
    while remaining:
        chunk = os.read(source, min(remaining, _COPY_CHUNK_BYTES))
        if not chunk:
            raise ValueError("authority fixture base changed while it was copied")
        remaining -= len(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(destination, view)
            if written <= 0:
                raise OSError("authority fixture base copy made no progress")
            view = view[written:]
    if os.read(source, 1):
        raise ValueError("authority fixture base changed while it was copied")


def _profile_with_private_base(
    profile: ProvisioningProfile, destination: Path
) -> ProvisioningProfile:
    section = profile.provider.local_libvirt_section
    if section is None or not isinstance(section.rootfs, LocalComponentRef):
        raise ValueError("authority fixture requires the fixed local rootfs input")
    rewritten = profile.model_copy(
        update={
            "provider": profile.provider.model_copy(
                update={
                    "local_libvirt_section": section.model_copy(
                        update={
                            "rootfs": section.rootfs.model_copy(update={"path": str(destination)})
                        }
                    )
                }
            )
        }
    )
    return ProvisioningProfile.model_validate(
        rewritten.model_dump(mode="json", by_alias=True, exclude_none=True)
    )


def _stage_fixture_base(
    system_id: UUID,
    profile: ProvisioningProfile,
    *,
    authority_uid: int,
    authority_gid: int,
    source_root: Path = _WORKER_ROOTFS_ROOT,
    private_root: Path = _AUTHORITY_ROOTFS_ROOT,
    maximum_bytes: int = _MAX_FIXTURE_BASE_BYTES,
) -> ProvisioningProfile:
    """Copy the fixed worker fixture base into one exclusive authority-owned System path."""
    section = profile.provider.local_libvirt_section
    expected_source = source_root / _FIXTURE_BASE_NAME
    if (
        section is None
        or not isinstance(section.rootfs, LocalComponentRef)
        or section.rootfs.path != str(expected_source)
    ):
        raise ValueError("authority fixture profile does not name the fixed worker rootfs input")
    destination_name = f"{system_id}-fixture-base.qcow2"
    staged_profile = _profile_with_private_base(profile, private_root / destination_name)

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    source_root_fd = os.open(source_root, directory_flags)
    try:
        private_root_fd = os.open(private_root, directory_flags)
        try:
            root_metadata = os.fstat(private_root_fd)
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or root_metadata.st_uid != authority_uid
                or root_metadata.st_gid != authority_gid
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
            ):
                raise PermissionError("authority fixture root must be authority-owned mode 0700")
            source_fd = os.open(
                _FIXTURE_BASE_NAME,
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=source_root_fd,
            )
            try:
                source_metadata = os.fstat(source_fd)
                if not stat.S_ISREG(source_metadata.st_mode):
                    raise ValueError("authority fixture base must be a regular file")
                if not 0 < source_metadata.st_size <= maximum_bytes:
                    raise ValueError(
                        f"authority fixture base exceeds its {maximum_bytes}-byte bound"
                    )
                _validate_standalone_qcow2(source_fd, source_metadata.st_size)
                destination_fd = os.open(
                    destination_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=private_root_fd,
                )
                destination_metadata = os.fstat(destination_fd)
                completed = False
                try:
                    os.fchown(destination_fd, authority_uid, authority_gid)
                    os.fchmod(destination_fd, 0o600)
                    _copy_exact(source_fd, destination_fd, source_metadata.st_size)
                    after_copy = os.fstat(source_fd)
                    stable_fields = (
                        "st_dev",
                        "st_ino",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                    if any(
                        getattr(source_metadata, name) != getattr(after_copy, name)
                        for name in stable_fields
                    ):
                        raise ValueError("authority fixture base changed while it was copied")
                    os.fsync(destination_fd)
                    os.fsync(private_root_fd)
                    completed = True
                finally:
                    os.close(destination_fd)
                    if not completed:
                        try:
                            current = os.stat(
                                destination_name,
                                dir_fd=private_root_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            pass
                        else:
                            if (current.st_dev, current.st_ino) == (
                                destination_metadata.st_dev,
                                destination_metadata.st_ino,
                            ):
                                os.unlink(destination_name, dir_fd=private_root_fd)
                                os.fsync(private_root_fd)
                return staged_profile
            finally:
                os.close(source_fd)
        finally:
            os.close(private_root_fd)
    finally:
        os.close(source_root_fd)


def _remove_regular(path: Path) -> None:
    try:
        opened = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise ValueError(f"refusing unexpected fixture artifact at {path}")
    path.unlink()


def _remove_directory(path: Path) -> None:
    try:
        opened = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(opened.st_mode) or stat.S_ISLNK(opened.st_mode):
        raise ValueError("refusing unexpected fixture baseline")
    shutil.rmtree(path)


def _remove_baseline(system_id: UUID) -> None:
    _remove_directory(Path(baseline_dir(system_id)))


def _refuse_existing_authority_domain(system_id: UUID) -> None:
    conn = libvirt.open(_AUTHORITY_URI)
    if conn is None:
        raise RuntimeError("authority fixture daemon connection returned no handle")
    try:
        try:
            conn.lookupByName(domain_name_for(system_id))
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return
            raise
        raise ValueError("refusing to replace an existing private authority fixture domain")
    finally:
        conn.close()


def _undefine_worker_domain(system_id: UUID) -> None:
    conn = libvirt.open(_WORKER_URI)
    if conn is None:
        raise RuntimeError("worker fixture daemon connection returned no handle")
    try:
        try:
            domain = conn.lookupByName(domain_name_for(system_id))
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return
            raise
        if domain.isActive() == 1:
            domain.destroy()
        domain.undefine()
    finally:
        conn.close()


def main() -> None:
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise SystemExit("run as root with one exact System UUID")
    system_id = UUID(sys.argv[1])
    profile = ProvisioningProfile.model_validate(json.load(sys.stdin))
    if profile.provider.local_libvirt_section is None:
        raise ValueError("authority fixture requires a local-libvirt profile")
    _refuse_existing_authority_domain(system_id)
    identity = pwd.getpwnam(_AUTHORITY)
    profile = _stage_fixture_base(
        system_id,
        profile,
        authority_uid=identity.pw_uid,
        authority_gid=identity.pw_gid,
    )
    _undefine_worker_domain(system_id)
    _remove_regular(Path("/var/lib/kdive/rootfs") / overlay_name(system_id))
    _remove_regular(Path("/var/lib/kdive/console") / f"{system_id}.log")
    legacy_baseline = Path("/var/lib/kdive/rootfs") / f"{system_id}-baseline"
    _remove_directory(legacy_baseline)
    _remove_regular(Path(overlay_path(system_id)))
    _remove_regular(console_log_path(system_id))
    _remove_baseline(system_id)

    os.initgroups(_AUTHORITY, identity.pw_gid)
    os.setgid(identity.pw_gid)
    os.setuid(identity.pw_uid)
    result = LocalLibvirtProvisioning.from_env().provision(system_id, profile)
    if result != domain_name_for(system_id):
        raise RuntimeError("authority fixture provision returned a different domain")
    for path in (Path(overlay_path(system_id)), console_log_path(system_id)):
        opened = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != identity.pw_uid:
            raise RuntimeError(f"authority fixture did not own {path}")
    conn = libvirt.open(_AUTHORITY_URI)
    if conn is None:
        raise RuntimeError("authority fixture daemon connection returned no handle")
    try:
        if conn.lookupByName(domain_name_for(system_id)).isActive() != 1:
            raise RuntimeError("authority fixture domain is not running on the private daemon")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
