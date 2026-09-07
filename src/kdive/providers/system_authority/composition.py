"""Production composition for fixed authority-owned System provider inputs."""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import libvirt

from kdive.components.references import CatalogComponentRef, LocalComponentRef
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle.boot.readiness import (
    ConsoleReadinessWindow,
    ConsoleVerdict,
    LocalExternalBootReadiness,
    _DomainExitProbe,
    classify_console,
    prepare_console_readiness_window,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    open_authority_system_teardown,
    prove_no_foreign_system_storage_references,
)
from kdive.providers.local_libvirt.lifecycle.provisioning import (
    LocalLibvirtProvisioning,
    _bind_probe_free_port,
)
from kdive.providers.local_libvirt.lifecycle.storage import ProvisioningFiles
from kdive.providers.local_libvirt.system_authority import (
    LocalAuthoritySystemProvider,
    LocalAuthoritySystemTopology,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.system_authority import RemoteAuthoritySystemProvider
from kdive.providers.shared.runtime_paths import console_log_path, read_console_log
from kdive.providers.system_authority.manifest import (
    LocalAuthoritySystemBaseV1,
    LocalAuthoritySystemManifestV1,
    RemoteAuthoritySystemManifestV1,
)
from kdive.providers.system_authority.protocol import AuthoritySystemProvisionSnapshot


def _require_private_directory(path: Path, uid: int, gid: int) -> None:
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("authority System private directory is unsafe")


@dataclass(frozen=True, slots=True)
class AuthoritySystemBaseFile:
    """A private base verified at startup without retaining its content in memory."""

    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def verify_current(self, *, owner_uid: int, owner_gid: int) -> None:
        try:
            metadata = os.stat(self.path, follow_symlinks=False)
        except OSError:
            raise ValueError("authority System staged base metadata changed") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
            or (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            != (self.device, self.inode, self.size, self.mtime_ns, self.ctime_ns)
        ):
            raise ValueError("authority System staged base metadata changed")


def _require_private_base(
    path: Path, uid: int, gid: int, root_identity: str
) -> AuthoritySystemBaseFile:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
            or metadata.st_uid != uid
            or metadata.st_gid != gid
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
        ):
            raise ValueError("authority System staged base is unsafe")
        digest = sha256()
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("authority System staged base changed while it was verified")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError("authority System staged base changed while it was verified")
        final_metadata = os.fstat(descriptor)
        if (
            final_metadata.st_dev,
            final_metadata.st_ino,
            final_metadata.st_size,
            final_metadata.st_mtime_ns,
            final_metadata.st_ctime_ns,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        ):
            raise ValueError("authority System staged base changed while it was verified")
        if f"sha256:{digest.hexdigest()}" != root_identity:
            raise ValueError("authority System staged base root identity is invalid")
        return AuthoritySystemBaseFile(
            path=path,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )
    finally:
        os.close(descriptor)


def validate_authority_system_installation(
    manifest: LocalAuthoritySystemManifestV1 | RemoteAuthoritySystemManifestV1,
    *,
    state_root: Path,
    local_root: Path,
    remote_root: Path,
    remote_pool_root: Path,
    owner_uid: int,
    owner_gid: int,
) -> tuple[AuthoritySystemBaseFile, ...]:
    """Validate every fixed private directory and staged base before advertisement."""
    _require_private_directory(state_root, owner_uid, owner_gid)
    _require_private_directory(state_root / "system-operations", owner_uid, owner_gid)
    if isinstance(manifest, LocalAuthoritySystemManifestV1):
        state = local_root / "state"
        rootfs = local_root / "rootfs"
        for path in (
            local_root,
            state,
            state / "intents",
            rootfs,
            rootfs / "systems",
            rootfs / "baselines",
            rootfs / "bases",
        ):
            _require_private_directory(path, owner_uid, owner_gid)
        bases = tuple(
            _require_private_base(
                rootfs / "bases" / entry.filename,
                owner_uid,
                owner_gid,
                entry.root_identity,
            )
            for entry in manifest.bases
        )
        return bases
    _require_private_directory(remote_root, owner_uid, owner_gid)
    _require_private_directory(remote_pool_root, owner_uid, owner_gid)
    return tuple(
        _require_private_base(
            remote_pool_root / entry.base_volume,
            owner_uid,
            owner_gid,
            entry.root_identity,
        )
        for entry in manifest.entries
    )


def _fixed_domain_exit(connect: Callable[[], Any], domain_name: str) -> _DomainExitProbe:
    connection = connect()
    domain = None
    try:
        try:
            domain = connection.lookupByName(domain_name)
        except libvirt.libvirtError as error:
            if error.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return _DomainExitProbe(True)
            return _DomainExitProbe(False)
        return _DomainExitProbe(domain.isActive() != 1)
    finally:
        if domain is not None:
            domain.free()
        connection.close()


class _LocalReadiness:
    """Retain the pre-start console inode and query exit on the fixed connection."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect
        self._windows: dict[UUID, ConsoleReadinessWindow] = {}
        self._lock = threading.Lock()

    def prepare(self, path: Path) -> None:
        system_id = UUID(path.stem)
        window = prepare_console_readiness_window(system_id)
        with self._lock:
            previous = self._windows.pop(system_id, None)
            self._windows[system_id] = window
        if previous is not None:
            previous.close()

    def __call__(self, system_id: UUID) -> bool:
        with self._lock:
            window = self._windows.pop(system_id, None)
        if window is None:
            try:
                return (
                    classify_console(read_console_log(console_log_path(system_id)))
                    is ConsoleVerdict.READY
                )
            except OSError, ValueError:
                return False
        try:
            result = LocalExternalBootReadiness(
                domain_exit_probe=lambda name: _fixed_domain_exit(self._connect, name)
            )(system_id, window)
            return result.ok
        finally:
            window.close()

    def close(self) -> None:
        with self._lock:
            windows, self._windows = tuple(self._windows.values()), {}
        for window in windows:
            window.close()


class _AuthorityProvisioner:
    """Narrow the concrete provisioner to the provider's deliberately small port."""

    def __init__(self, delegate: LocalLibvirtProvisioning) -> None:
        self._delegate = delegate

    def provision(self, system_id: UUID, profile: Any, **kwargs: Any) -> str:
        if not isinstance(profile, ProvisioningProfile):
            raise TypeError("authority System provisioning profile is invalid")
        return self._delegate.provision(system_id, profile, **kwargs)


def _local_base_selector(
    source: object, architecture: str, entries: tuple[LocalAuthoritySystemBaseV1, ...]
) -> LocalAuthoritySystemBaseV1:
    for entry in entries:
        if entry.architecture != architecture:
            continue
        if (
            entry.source_kind == "catalog"
            and isinstance(source, CatalogComponentRef)
            and source.provider == "local-libvirt"
            and source.name == entry.source_name
        ) or (
            entry.source_kind == "local"
            and isinstance(source, LocalComponentRef)
            and source.sha256 == entry.root_identity
        ):
            return entry
    raise ValueError("local authority root source is not installed")


def build_local_authority_system_provider(
    manifest: LocalAuthoritySystemManifestV1,
    *,
    base_files: tuple[AuthoritySystemBaseFile, ...],
    connect: Callable[[], Any],
    state_root: Path,
    rootfs_root: Path,
    owner_uid: int,
    owner_gid: int,
) -> tuple[LocalAuthoritySystemProvider, Callable[[], None]]:
    """Build local provisioning only from manifest-bound private roots and bases."""
    intent_root = state_root / "intents"
    overlay_root = rootfs_root / "systems"
    baseline_root = rootfs_root / "baselines"
    base_root = rootfs_root / "bases"
    for path in (state_root, intent_root, rootfs_root, overlay_root, baseline_root, base_root):
        _require_private_directory(path, owner_uid, owner_gid)
    staged_bases = {entry.root_identity: base_root / entry.filename for entry in manifest.bases}
    verified_bases = {base.path: base for base in base_files}
    if set(verified_bases) != set(staged_bases.values()):
        raise ValueError("local authority System verified bases do not match its manifest")
    readiness = _LocalReadiness(connect)

    def materialize(source: object, _system_id: UUID, architecture: str, **_kwargs: object) -> str:
        entry = _local_base_selector(source, architecture, manifest.bases)
        return str(staged_bases[entry.root_identity])

    files = ProvisioningFiles(
        prepare_console_log=readiness.prepare,
        overlay_path_for=lambda system_id: str(overlay_root / f"{system_id}.qcow2"),
        baseline_dir_for=lambda system_id: str(baseline_root / f"{system_id}-baseline"),
    )
    topology = LocalAuthoritySystemTopology(
        intent_root=intent_root,
        overlay_root=overlay_root,
        baseline_root=baseline_root,
        staged_bases=staged_bases,
        guest_egress=manifest.guest_egress,
        accel=manifest.accel,
        emulator=manifest.emulator,
    )

    def validate_snapshot(snapshot: AuthoritySystemProvisionSnapshot) -> None:
        section = snapshot.profile.provider.local_libvirt_section
        if section is None:
            raise ValueError("local authority snapshot has no local provider profile")
        entry = _local_base_selector(section.rootfs, snapshot.profile.arch, manifest.bases)
        if entry.root_identity != snapshot.root_identity:
            raise ValueError("local authority root source differs from its immutable snapshot")
        verified_bases[staged_bases[entry.root_identity]].verify_current(
            owner_uid=owner_uid, owner_gid=owner_gid
        )

    provisioner = LocalLibvirtProvisioning(
        connect=connect,
        files=files,
        allowed_roots=[base_root],
        materialize_rootfs=materialize,
        guest_egress=manifest.guest_egress,
    )
    provider = LocalAuthoritySystemProvider(
        provisioner=_AuthorityProvisioner(provisioner),
        topology=topology,
        readiness_probe=readiness,
        open_teardown=lambda system_id, overlay, baseline: open_authority_system_teardown(
            connect, system_id, overlay, baseline
        ),
        assert_no_sibling_attachment=lambda system_id, overlay, baseline: (
            prove_no_foreign_system_storage_references(connect, system_id, overlay, baseline)
        ),
        allocate_port=_bind_probe_free_port,
        validate_provision_snapshot=validate_snapshot,
        manifest_binding=(
            manifest.provider_kind,
            manifest.resource_name,
            manifest.authority_instance,
        ),
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )

    def close() -> None:
        readiness.close()
        provider.close()

    return provider, close


def build_remote_authority_system_provider(
    manifest: RemoteAuthoritySystemManifestV1,
    *,
    base_files: tuple[AuthoritySystemBaseFile, ...],
    connection: Any,
    pool_name: str,
    remote_pool_root: Path,
    state_root: Path,
    executor: RemoteModulePreparationExecutor,
    owner_uid: int,
    owner_gid: int,
) -> RemoteAuthoritySystemProvider:
    """Build remote provisioning over the already-open fixed private connection and pool."""
    _require_private_directory(state_root, owner_uid, owner_gid)

    @contextmanager
    def provider_connection() -> Iterator[Any]:
        yield connection

    private_base_paths = {
        entry.base_volume: remote_pool_root / entry.base_volume for entry in manifest.entries
    }
    verified_bases = {base.path: base for base in base_files}
    if set(verified_bases) != set(private_base_paths.values()):
        raise ValueError("remote authority System verified bases do not match its manifest")

    def validate_private_base(name: str, path: str) -> None:
        expected = private_base_paths[name]
        if Path(path) != expected:
            raise ValueError("remote authority System base path differs from its manifest")
        verified_bases[expected].verify_current(owner_uid=owner_uid, owner_gid=owner_gid)

    return RemoteAuthoritySystemProvider(
        connection=provider_connection,
        pool_name=pool_name,
        manifest=manifest.provider_entries(),
        private_base_paths=private_base_paths,
        validate_private_base=validate_private_base,
        state_dir=state_root,
        executor=executor,
        monotonic=time.monotonic,
        sleep=time.sleep,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )


__all__ = [
    "build_local_authority_system_provider",
    "build_remote_authority_system_provider",
    "AuthoritySystemBaseFile",
    "validate_authority_system_installation",
]
