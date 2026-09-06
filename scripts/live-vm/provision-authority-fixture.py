#!/usr/bin/env python3
"""Move one explicitly selected disposable live fixture onto the private authority daemon."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import stat
import sys
from pathlib import Path
from uuid import UUID

# This script is the one explicitly selected disposable authority fixture. Set its fixed
# authority-owned roots before importing provider modules, which bind runtime paths at import.
os.environ["KDIVE_LIBVIRT_ROOTFS_ROOT"] = "/var/lib/kdive/provider-authority/rootfs"
os.environ["KDIVE_LIBVIRT_CONSOLE_ROOT"] = "/var/lib/kdive/provider-authority/console"

import libvirt

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.local_libvirt.lifecycle.storage import baseline_dir, overlay_path
from kdive.providers.shared.runtime_paths import console_log_path, domain_name_for, overlay_name

_AUTHORITY = "kdive-provider-authority"
_WORKER_URI = "qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"
_AUTHORITY_URI = "qemu+unix:///system?socket=/run/kdive/provider-authority/libvirt/libvirt-sock"


def _remove_regular(path: Path) -> None:
    try:
        opened = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise ValueError(f"refusing unexpected fixture artifact at {path}")
    path.unlink()


def _remove_baseline(system_id: UUID) -> None:
    path = Path(baseline_dir(system_id))
    try:
        opened = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(opened.st_mode) or stat.S_ISLNK(opened.st_mode):
        raise ValueError("refusing unexpected fixture baseline")
    shutil.rmtree(path)


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
    _undefine_worker_domain(system_id)
    _remove_regular(Path("/var/lib/kdive/rootfs") / overlay_name(system_id))
    _remove_regular(Path("/var/lib/kdive/console") / f"{system_id}.log")
    legacy_baseline = Path("/var/lib/kdive/rootfs") / f"{system_id}-baseline"
    if legacy_baseline.exists():
        shutil.rmtree(legacy_baseline)
    _remove_regular(Path(overlay_path(system_id)))
    _remove_regular(console_log_path(system_id))
    _remove_baseline(system_id)

    identity = pwd.getpwnam(_AUTHORITY)
    os.initgroups(_AUTHORITY, identity.pw_gid)
    os.setgid(identity.pw_gid)
    os.setuid(identity.pw_uid)
    os.environ["KDIVE_LIBVIRT_URI"] = _AUTHORITY_URI
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
