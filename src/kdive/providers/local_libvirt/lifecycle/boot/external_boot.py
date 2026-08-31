"""Local-libvirt external-boot recovery state machine (ADR-0586)."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
import tarfile
import tempfile
import unicodedata
import xml.etree.ElementTree as ET  # noqa: S405 - edits trusted domain structure after safe parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, BinaryIO, Literal, Protocol, cast

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kdive.providers.local_libvirt.lifecycle.boot.recovery import (
    MAX_ARCHIVE_BYTES,
    MAX_ENTRIES,
    MAX_REGULAR_BYTES,
    GuestTreeEntry,
    ModuleCapture,
)
from kdive.providers.ports.external_boot import (
    ActivationOwnership,
    ComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    OpaqueProviderRef,
    ProviderStateIdentity,
    RecoveryPoint,
    RunningKernelObservation,
)
from kdive.providers.shared.libvirt_xml import register_kdive_namespace, register_qemu_namespace


class PublicationPhase(StrEnum):
    MOVE_READY = "move-ready"
    OLD_ASIDE = "old-aside"
    ROLLBACK_READY = "rollback-ready"
    ROLLBACK_COMPLETE = "rollback-complete"
    NEW_LIVE = "new-live"
    PUBLICATION_COMPLETE = "publication-complete"
    ABSENCE_LIVE = "absence-live"
    ABSENCE_COMPLETE = "absence-complete"
    ABSENCE_CLEANED = "absence-cleaned"


type RecoveryPhase = Literal[
    "pre-stop-intent",
    "move-ready",
    "old-aside",
    "rollback-ready",
    "rollback-complete",
    "new-live",
    "publication-complete",
    "absence-live",
    "absence-complete",
    "absence-cleaned",
    "target-defined",
    "module-restored",
    "source-restored",
    "recovered",
    "cleaned",
]
type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LocalRecoveryMetadataV1(_ClosedValue):
    """Closed durable local recovery record; it contains no host path authority."""

    schema_: Literal["local-libvirt-recovery-v1"] = Field(
        "local-libvirt-recovery-v1", alias="schema"
    )
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    materialization_identity: Digest
    release: str
    materialized_modules: OpaqueProviderRef
    materialized_modules_sha256: Digest
    materialized_modules_bytes: Annotated[int, Field(ge=0)]
    source_xml_sha256: Digest
    source_xml: str
    source_definition: Digest
    source_boot: Digest
    target_boot: Digest
    target_projection_sha256: Digest
    target_xml: str
    source_state: ProviderStateIdentity
    target_state: ProviderStateIdentity
    prior_power: Literal["running", "inactive"]
    capture: ModuleCapture
    phase: RecoveryPhase

    @model_validator(mode="after")
    def _source_xml_matches_digest(self) -> LocalRecoveryMetadataV1:
        xml_values = (self.source_xml, self.target_xml)
        if any(unicodedata.normalize("NFC", value) != value for value in xml_values):
            raise ValueError("source and target domain XML must be NFC")
        digest = "sha256:" + hashlib.sha256(self.source_xml.encode()).hexdigest()
        if digest != self.source_xml_sha256:
            raise ValueError("source domain XML digest does not match bytes")
        return self


class LocalPreStopIntentV1(_ClosedValue):
    """Information established without mutating libvirt or the guest overlay."""

    schema_: Literal["local-libvirt-pre-stop-intent-v1"] = Field(
        "local-libvirt-pre-stop-intent-v1", alias="schema"
    )
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    materialization_identity: Digest
    release: str
    materialized_modules: OpaqueProviderRef
    materialized_modules_sha256: Digest
    materialized_modules_bytes: Annotated[int, Field(ge=0)]
    source_xml_sha256: Digest
    source_xml: str
    source_definition: Digest
    source_boot: Digest
    target_boot: Digest
    target_projection_sha256: Digest
    target_xml: str
    prior_power: Literal["running", "inactive"]

    @model_validator(mode="after")
    def _source_xml_matches_digest(self) -> LocalPreStopIntentV1:
        xml_values = (self.source_xml, self.target_xml)
        if any(unicodedata.normalize("NFC", value) != value for value in xml_values):
            raise ValueError("source and target domain XML must be NFC")
        digest = "sha256:" + hashlib.sha256(self.source_xml.encode()).hexdigest()
        if digest != self.source_xml_sha256:
            raise ValueError("source domain XML digest does not match bytes")
        return self


class FinalizeCleanupProof(_ClosedValue):
    point_digest: Digest
    binding: ExternalBootActivationBinding
    operation_id: Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
    attempt_id: Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
    journal_sequence: Annotated[int, Field(ge=1)]
    journal_digest: Digest
    phase: Literal["mutation-started"] = "mutation-started"


class CleanupTombstoneV1(_ClosedValue):
    """Accounted cleanup evidence retained until authority finalization."""

    schema_: Literal["local-libvirt-cleanup-tombstone-v1"] = Field(
        "local-libvirt-cleanup-tombstone-v1", alias="schema"
    )
    binding: ExternalBootActivationBinding
    point_digest: Digest
    payload_absent: Literal[True] = True


class TargetProjectionV1(_ClosedValue):
    """Minimal durable inputs needed to render one target domain definition."""

    schema_: Literal["local-libvirt-target-projection-v1"] = Field(
        "local-libvirt-target-projection-v1", alias="schema"
    )
    ownership: ActivationOwnership
    plan_identity: Digest
    architecture: Literal["x86_64", "ppc64le"]
    cmdline: Annotated[str, Field(min_length=1, max_length=4096)]
    kernel_filename: Literal["kernel"] = "kernel"
    modules_filename: Literal["modules"] = "modules"
    initrd_filename: Literal["initrd"] | None

    @model_validator(mode="after")
    def _canonical_text(self) -> TargetProjectionV1:
        if unicodedata.normalize("NFC", self.cmdline) != self.cmdline:
            raise ValueError("target projection cmdline must be NFC")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


_PROJECTION_NAME = "target-projection.json"
_PROJECTION_TEMPORARY_NAME = ".target-projection.next"
_MAX_PROJECTION_BYTES = 16_384


class TargetProjectionStore:
    """Owner-relative durable target projection and deterministic artifact references."""

    def __init__(self, root: Path) -> None:
        self._root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _require_private_owned_directory(self._root_fd, "artifact root")
        except BaseException:
            os.close(self._root_fd)
            raise

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> TargetProjectionStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def publish(self, projection: TargetProjectionV1) -> OpaqueProviderRef:
        """Exclusively publish and exactly reopen one canonical projection."""
        owner_fd = _open_or_create_private_child(self._root_fd, projection.ownership.system_id)
        try:
            run_fd = _open_or_create_private_child(owner_fd, projection.ownership.run_id)
        finally:
            os.close(owner_fd)
        try:
            digest_name = projection.digest.removeprefix("sha256:")
            projection_fd = _open_or_create_private_child(run_fd, digest_name)
            try:
                data = projection.canonical_bytes()
                if len(data) > _MAX_PROJECTION_BYTES:
                    raise ValueError("target projection exceeds its byte bound")
                try:
                    existing = _read_private_file(projection_fd, _PROJECTION_NAME)
                except FileNotFoundError:
                    _replace_private_file(
                        projection_fd,
                        _PROJECTION_TEMPORARY_NAME,
                        _PROJECTION_NAME,
                        data,
                    )
                else:
                    if existing != data:
                        raise ValueError("target projection conflicts with existing sidecar")
                os.fsync(projection_fd)
            finally:
                os.close(projection_fd)
            os.fsync(run_fd)
        finally:
            os.close(run_fd)
        reopened = self.reopen(_projection_ref(projection, "kernel"), projection.ownership)
        if reopened != projection:
            raise ValueError("target projection failed exact reopen")
        return _projection_ref(projection, "kernel")

    def reopen(
        self, artifact: OpaqueProviderRef, ownership: ActivationOwnership
    ) -> TargetProjectionV1:
        parts = _artifact_ref_parts(artifact, ownership)
        system_fd = _open_private_directory(self._root_fd, parts[1])
        try:
            run_fd = _open_private_directory(system_fd, parts[2])
        finally:
            os.close(system_fd)
        try:
            projection_fd = _open_private_directory(run_fd, parts[3])
        finally:
            os.close(run_fd)
        try:
            data = _read_private_file(projection_fd, _PROJECTION_NAME)
        finally:
            os.close(projection_fd)
        projection = TargetProjectionV1.model_validate_json(data)
        digest_matches = projection.digest.removeprefix("sha256:") == parts[3]
        if projection.canonical_bytes() != data or not digest_matches:
            raise ValueError("target projection is not canonical or digest-bound")
        if projection.ownership != ownership:
            raise ValueError("target projection owner does not match artifact reference")
        return projection


def _projection_ref(projection: TargetProjectionV1, filename: str) -> OpaqueProviderRef:
    return OpaqueProviderRef(
        ref=(
            f"local-artifact-v1/{projection.ownership.system_id}/"
            f"{projection.ownership.run_id}/{projection.digest.removeprefix('sha256:')}/{filename}"
        )
    )


def _artifact_ref_parts(artifact: OpaqueProviderRef, ownership: ActivationOwnership) -> list[str]:
    parts = artifact.ref.split("/")
    if (
        len(parts) != 5
        or parts[0] != "local-artifact-v1"
        or parts[1] != ownership.system_id
        or parts[2] != ownership.run_id
        or len(parts[3]) != 64
        or any(character not in "0123456789abcdef" for character in parts[3])
        or parts[4] not in {"kernel", "modules", "initrd"}
    ):
        raise ValueError("local artifact reference is malformed or cross-owner")
    return parts


def _open_or_create_private_child(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    return _open_private_directory(parent_fd, name)


@dataclass(frozen=True, slots=True)
class ModuleLayout:
    live: ComponentState | None
    staging: ComponentState | None
    old: ComponentState | None


@dataclass(frozen=True, slots=True)
class _ConvertedMember:
    name: str
    mode: int
    uid: int
    gid: int
    kind: Literal["directory", "regular", "symlink"]
    size: int
    target: str
    offset: int


def convert_kernel_bundle_modules(
    source: BinaryIO, destination: BinaryIO, *, release: str
) -> tuple[str, int]:
    """Convert one raw bundle module tree into Task 2's canonical archive."""
    prefix = f"lib/modules/{release}/"
    entries: list[_ConvertedMember] = []
    seen: set[str] = set()
    regular_bytes = 0
    with tempfile.TemporaryFile() as content:
        with tarfile.open(fileobj=source, mode="r|gz") as archive:
            for member in archive:
                if member.name == "boot/vmlinuz" or member.name in {
                    "lib",
                    "lib/modules",
                    f"lib/modules/{release}",
                }:
                    continue
                if not member.name.startswith(prefix):
                    raise ValueError("module bundle contains a cross-release or unknown entry")
                name = _canonical_bundle_path(member.name[len(prefix) :])
                if (
                    member.issym()
                    and member.linkname.startswith("/")
                    and name
                    in {
                        "build",
                        "source",
                    }
                ):
                    continue
                if name in seen:
                    raise ValueError("module bundle contains duplicate entries")
                seen.add(name)
                kind = _bundle_member_kind(member)
                target = (
                    _canonical_bundle_target(name, member.linkname) if kind == "symlink" else ""
                )
                size, offset = 0, content.tell()
                if kind == "regular":
                    regular_bytes += member.size
                    if regular_bytes > MAX_REGULAR_BYTES:
                        raise ValueError("module bundle exceeds the regular-byte bound")
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise ValueError("module bundle regular entry is unreadable")
                    size = _copy_exact(cast(BinaryIO, extracted), content, member.size)
                entries.append(
                    _ConvertedMember(
                        name,
                        member.mode,
                        member.uid,
                        member.gid,
                        kind,
                        size,
                        target,
                        offset,
                    )
                )
                if len(entries) > MAX_ENTRIES:
                    raise ValueError("module bundle exceeds the entry-count bound")
        with tempfile.TemporaryFile() as converted:
            with tarfile.open(fileobj=converted, mode="w", format=tarfile.PAX_FORMAT) as output:
                for entry in sorted(entries, key=lambda value: value.name.encode()):
                    info = tarfile.TarInfo(entry.name)
                    info.mode, info.uid, info.gid, info.mtime = (
                        entry.mode,
                        entry.uid,
                        entry.gid,
                        0,
                    )
                    info.uname = info.gname = ""
                    info.pax_headers = {"KDIVE.xattrs-supported": "0"}
                    reader: BinaryIO | None = None
                    if entry.kind == "directory":
                        info.type = tarfile.DIRTYPE
                    elif entry.kind == "symlink":
                        info.type, info.linkname = tarfile.SYMTYPE, entry.target
                    else:
                        info.size = entry.size
                        content.seek(entry.offset)
                        reader = cast(BinaryIO, _BoundedSlice(content, entry.size))
                    output.addfile(info, reader)
            size = converted.tell()
            if size > MAX_ARCHIVE_BYTES:
                raise ValueError("canonical module archive exceeds its byte bound")
            converted.seek(0)
            digest = hashlib.sha256()
            while chunk := converted.read(1024 * 1024):
                digest.update(chunk)
                destination.write(chunk)
    return "sha256:" + digest.hexdigest(), size


class _BoundedSlice:
    def __init__(self, source: BinaryIO, remaining: int) -> None:
        self._source, self._remaining = source, remaining

    def read(self, size: int = -1) -> bytes:
        count = self._remaining if size < 0 else min(size, self._remaining)
        data = self._source.read(count)
        self._remaining -= len(data)
        return data


def _copy_exact(source: BinaryIO, destination: BinaryIO, expected: int) -> int:
    remaining = expected
    while remaining:
        chunk = source.read(min(1024 * 1024, remaining))
        if not chunk:
            raise ValueError("module bundle entry ended before its declared size")
        destination.write(chunk)
        remaining -= len(chunk)
    return expected


def _canonical_bundle_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or unicodedata.normalize("NFC", value) != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("module bundle path is not canonical relative text")
    return value


def _bundle_member_kind(member: tarfile.TarInfo) -> Literal["directory", "regular", "symlink"]:
    if member.isdir():
        return "directory"
    if member.isfile() and not member.islnk():
        return "regular"
    if member.issym():
        return "symlink"
    raise ValueError("module bundle contains forbidden topology")


def _canonical_bundle_target(name: str, target: str) -> str:
    if unicodedata.normalize("NFC", target) != target:
        raise ValueError("module bundle symlink target is not NFC")
    if target.startswith("/"):
        raise ValueError("module bundle symlink escapes the release tree")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError("module bundle symlink escapes the release tree")
    return target


class ModulePublicationIO(Protocol):
    def require_inactive(self) -> None: ...
    def observe_layout(self) -> ModuleLayout: ...
    def move_live_to_old(self) -> None: ...
    def move_staging_to_live(self) -> None: ...
    def move_old_to_live(self) -> None: ...
    def remove_old(self) -> None: ...
    def guest_sync(self) -> None: ...
    def record_phase(self, phase: PublicationPhase) -> None: ...


class _GuestfsTreeHandle(Protocol):  # pragma: no cover - live_vm (libguestfs binding)
    def exists(self, path: str) -> int: ...
    def is_dir(self, path: str, *, followsymlinks: bool) -> int: ...
    def find(self, path: str) -> list[str]: ...
    def lstatns(self, path: str) -> dict[str, int]: ...
    def readlink(self, path: str) -> str: ...
    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]: ...
    def download(self, remotefilename: str, filename: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def upload(self, filename: str, remotefilename: str) -> None: ...
    def ln_s(self, target: str, linkname: str) -> None: ...
    def chmod(self, mode: int, path: str) -> None: ...
    def chown(self, owner: int, group: int, path: str) -> None: ...
    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None: ...
    def rm_rf(self, path: str) -> None: ...


class LibguestfsAuthenticatedGuestTree:
    """Private, owner-bound capability for one deterministic guest tree."""

    def __init__(
        self,
        guest: _GuestfsTreeHandle,
        *,
        binding: ExternalBootActivationBinding,
        release: str,
        root: str,
        mutable: bool,
    ) -> None:
        expected_prefix = f"/lib/modules/.kdive-{binding.activation_id}-"
        live = f"/lib/modules/{release}"
        if root != live and not root.startswith(expected_prefix):
            raise ValueError("guest-tree root is not the bound release or activation staging tree")
        if mutable and root == live:
            raise ValueError("mutable guest-tree capability requires an activation staging tree")
        if not release or "/" in release or unicodedata.normalize("NFC", release) != release:
            raise ValueError("guest-tree release is invalid")
        self._guest = guest
        self.binding = binding
        self.release = release
        self.mutable = mutable
        self._root = root

    def root_kind(self) -> Literal["absent", "directory", "other"]:
        if not bool(self._guest.exists(self._root)):
            return "absent"
        if bool(self._guest.is_dir(self._root, followsymlinks=False)):
            return "directory"
        return "other"

    def entries(self) -> Iterator[GuestTreeEntry]:
        if self.root_kind() != "directory":
            return
        for relative in sorted(self._guest.find(self._root), key=lambda item: item.encode()):
            path = _guest_relative(relative)
            yield self._entry(path)

    @contextmanager
    def open_regular(self, path: str, size: int) -> Iterator[BinaryIO]:
        remote = self._remote(path)
        opened = self._guest.lstatns(remote)
        if opened["st_size"] != size or not stat.S_ISREG(opened["st_mode"]):
            raise ValueError("guest regular file changed before content read")
        with tempfile.TemporaryFile("w+b") as local:
            self._guest.download(remote, f"/proc/self/fd/{local.fileno()}")
            local.seek(0)
            yield local

    def create_directory(self, entry: GuestTreeEntry) -> None:
        self._require_mutable()
        remote = self._remote(entry.path)
        self._guest.mkdir(remote)
        self._apply_metadata(remote, entry)

    def create_regular(self, entry: GuestTreeEntry, content: BinaryIO) -> None:
        self._require_mutable()
        with tempfile.NamedTemporaryFile("w+b") as local:
            remaining = entry.size
            while remaining:
                chunk = content.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("guest regular content ended before declared size")
                local.write(chunk)
                remaining -= len(chunk)
            local.flush()
            self._guest.upload(local.name, self._remote(entry.path))
        self._apply_metadata(self._remote(entry.path), entry)

    def create_symlink(self, entry: GuestTreeEntry) -> None:
        self._require_mutable()
        if entry.target is None:
            raise ValueError("guest symlink target is missing")
        remote = self._remote(entry.path)
        self._guest.ln_s(entry.target, remote)
        self._guest.chown(entry.uid, entry.gid, remote)

    def remove_all(self) -> None:
        self._require_mutable()
        self._guest.rm_rf(self._root)

    def _entry(self, path: str) -> GuestTreeEntry:
        remote = self._remote(path)
        value = self._guest.lstatns(remote)
        mode = value["st_mode"]
        kind: Literal["directory", "regular", "symlink"]
        if stat.S_ISDIR(mode):
            kind = "directory"
        elif stat.S_ISREG(mode):
            kind = "regular"
        elif stat.S_ISLNK(mode):
            kind = "symlink"
        else:
            raise ValueError("guest tree contains an unsupported entry type")
        xattrs = self._guest.lgetxattrs(remote)
        return GuestTreeEntry(
            path=path,
            kind=kind,
            mode=f"{stat.S_IMODE(mode):04o}",
            uid=value["st_uid"],
            gid=value["st_gid"],
            size=value["st_size"] if kind == "regular" else 0,
            target=self._guest.readlink(remote) if kind == "symlink" else None,
            xattrs_supported=True,
            xattrs={str(item["attrname"]): _xattr_bytes(item["attrval"]) for item in xattrs},
            link_count=value["st_nlink"],
        )

    def _apply_metadata(self, remote: str, entry: GuestTreeEntry) -> None:
        self._guest.chmod(int(entry.mode, 8), remote)
        self._guest.chown(entry.uid, entry.gid, remote)
        for name, value in entry.xattrs.items():
            self._guest.lsetxattr(name, value, len(value), remote)

    def _remote(self, relative: str) -> str:
        return f"{self._root}/{_guest_relative(relative)}"

    def _require_mutable(self) -> None:
        if not self.mutable:
            raise ValueError("guest-tree capability is read-only")


def _guest_relative(path: str) -> str:
    if (
        not path
        or path.startswith("/")
        or unicodedata.normalize("NFC", path) != path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("guest-tree entry path is not a canonical relative path")
    return path


def _xattr_bytes(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode()


class LocalExternalBootIO(Protocol):
    """Injected privileged operations; the state machine retains ordering and validation."""

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization: ...
    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> LocalRecoveryMetadataV1: ...
    def recovery_ref(self, binding: ExternalBootActivationBinding) -> OpaqueProviderRef: ...
    def reopen(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> LocalRecoveryMetadataV1: ...
    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation: ...
    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def record_phase(
        self, metadata: LocalRecoveryMetadataV1, phase: RecoveryPhase
    ) -> LocalRecoveryMetadataV1: ...
    def cleanup(self, metadata: LocalRecoveryMetadataV1, point_digest: Digest) -> None: ...
    def finalize_tombstone(self, recovery: RecoveryPoint, proof: FinalizeCleanupProof) -> None: ...


class LocalExternalBootHost(Protocol):
    """Injected libvirt, libguestfs, artifact, and readiness operations."""

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization: ...
    def inspect_prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> LocalPreStopIntentV1: ...
    def complete_prepare(self, intent: LocalPreStopIntentV1) -> LocalRecoveryMetadataV1: ...
    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation: ...
    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def cleanup_payloads(self, metadata: LocalRecoveryMetadataV1) -> None: ...


class RealLocalExternalBootIO:
    """Concrete durable adapter that orders host mutations behind recovery evidence."""

    def __init__(self, recovery_root: Path, host: LocalExternalBootHost) -> None:
        self._recovery_root = recovery_root
        self._host = host

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization:
        return self._host.materialize(plan, authority)

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> LocalRecoveryMetadataV1:
        reference = _recovery_ref(binding)
        with RecoveryMetadataStore(self._recovery_root) as store:
            try:
                complete = store.reopen(reference, binding)
            except FileNotFoundError:
                complete = None
            if complete is not None:
                _validate_preparation_owner(complete, materialization, binding)
                return complete
            try:
                intent = store.reopen_pre_stop(reference, binding)
            except FileNotFoundError:
                intent = self._host.inspect_prepare(materialization, binding, authority)
                _validate_preparation_owner(intent, materialization, binding)
                store.publish_pre_stop(intent)
            else:
                _validate_preparation_owner(intent, materialization, binding)
        metadata = self._host.complete_prepare(intent)
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.complete_preparation(reference, intent, metadata)

    @staticmethod
    def recovery_ref(binding: ExternalBootActivationBinding) -> OpaqueProviderRef:
        return _recovery_ref(binding)

    def reopen(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> LocalRecoveryMetadataV1:
        del authority
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.reopen(recovery.recovery_ref, recovery.binding)

    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self._host.activate_modules(metadata)

    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None:
        self._host.define_target(metadata)

    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation:
        return self._host.observe_running(metadata)

    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self._host.recover_modules(metadata)

    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None:
        self._host.define_source(metadata)

    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None:
        self._host.restore_power(metadata)

    def record_phase(
        self, metadata: LocalRecoveryMetadataV1, phase: RecoveryPhase
    ) -> LocalRecoveryMetadataV1:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.record_phase(
                _recovery_ref(metadata.binding), metadata.binding, metadata, phase
            )

    def cleanup(self, metadata: LocalRecoveryMetadataV1, point_digest: Digest) -> None:
        self._host.cleanup_payloads(metadata)
        with RecoveryMetadataStore(self._recovery_root) as store:
            store.publish_tombstone(
                _recovery_ref(metadata.binding), metadata.binding, metadata, point_digest
            )

    def finalize_tombstone(self, recovery: RecoveryPoint, proof: FinalizeCleanupProof) -> None:
        with RecoveryMetadataStore(self._recovery_root) as store:
            store.finalize_tombstone(recovery.recovery_ref, recovery, proof)


def _validate_preparation_owner(
    value: LocalPreStopIntentV1 | LocalRecoveryMetadataV1,
    materialization: ExternalBootMaterialization,
    binding: ExternalBootActivationBinding,
) -> None:
    if (
        value.binding != binding
        or value.materialization_identity != materialization.identity
        or value.plan_identity != materialization.plan_identity
    ):
        raise ValueError("pre-stop intent does not match activation")


class LocalLibvirtExternalBoot:
    """Six-port local lifecycle coordinator over authenticated, injected host I/O."""

    def __init__(self, io: LocalExternalBootIO) -> None:
        self._io = io

    @staticmethod
    def point_digest(recovery: RecoveryPoint) -> str:
        return (
            "sha256:"
            + hashlib.sha256(
                b"kdive-external-boot-recovery-point-v1\0" + recovery.to_canonical_json()
            ).hexdigest()
        )

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization:
        materialization = self._io.materialize(plan, authority)
        if (
            materialization.provider_kind != "local-libvirt"
            or materialization.ownership.system_id != plan.ownership.system_id
            or materialization.ownership.run_id != plan.ownership.run_id
            or materialization.plan_identity != plan.identity
            or materialization.architecture != plan.architecture
        ):
            raise ValueError("external-boot materialization does not match plan")
        return materialization

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> RecoveryPoint:
        if (
            binding.system_id != materialization.ownership.system_id
            or binding.run_id != materialization.ownership.run_id
        ):
            raise ValueError("external-boot binding does not match materialization")
        metadata = self._io.prepare(materialization, binding, authority)
        if (
            metadata.binding != binding
            or metadata.materialization_identity != materialization.identity
            or metadata.plan_identity != materialization.plan_identity
        ):
            raise ValueError("external-boot prepared metadata does not match request")
        point = RecoveryPoint(
            binding=binding,
            plan_identity=metadata.plan_identity,
            materialization_identity=metadata.materialization_identity,
            recovery_ref=self._io.recovery_ref(binding),
            source_state=metadata.source_state,
            target_state=metadata.target_state,
        )
        self._validate_metadata(point, metadata)
        return point

    def activate(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        metadata = self._reopen(recovery, authority)
        if metadata.phase not in {"pre-stop-intent", "module-restored", "target-defined"}:
            raise ValueError("external-boot activation phase is not resumable")
        if metadata.phase == "pre-stop-intent":
            self._io.activate_modules(metadata)
            metadata = self._io.record_phase(metadata, "module-restored")
        if metadata.phase == "module-restored":
            self._io.define_target(metadata)
            self._io.record_phase(metadata, "target-defined")

    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation:
        metadata = self._reopen(recovery, authority)
        if metadata.phase != "target-defined":
            raise ValueError("external-boot target-defined evidence is required")
        return self._io.observe_running(metadata)

    def recover(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        metadata = self._reopen(recovery, authority)
        if metadata.phase in {"recovered", "cleaned"}:
            return
        if metadata.phase not in {
            "target-defined",
            "module-restored",
            "source-restored",
        }:
            raise ValueError("external-boot recovery phase is not resumable")
        if metadata.phase == "target-defined":
            self._io.recover_modules(metadata)
            metadata = self._io.record_phase(metadata, "module-restored")
        if metadata.phase == "module-restored":
            self._io.define_source(metadata)
            metadata = self._io.record_phase(metadata, "source-restored")
        if metadata.phase == "source-restored":
            self._io.restore_power(metadata)
            self._io.record_phase(metadata, "recovered")

    def cleanup(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        metadata = self._reopen(recovery, authority)
        if metadata.phase == "cleaned":
            return
        if metadata.phase != "recovered":
            raise ValueError("external-boot recovery must complete before cleanup")
        self._io.cleanup(metadata, self.point_digest(recovery))

    def finalize_cleanup_tombstone(
        self,
        recovery: RecoveryPoint,
        proof: FinalizeCleanupProof,
        authority: OpaqueProviderRef,
    ) -> None:
        if proof.binding != recovery.binding or proof.point_digest != self.point_digest(recovery):
            raise ValueError("external-boot cleanup proof does not match recovery point")
        # #2140 authenticates ``authority`` and supplies only an unresolved exact
        # mutation-started proof.  The local seam deliberately does not decode it;
        # it compares the closed owner/point fields and handles present or
        # post-delete absence idempotently.
        self._io.finalize_tombstone(recovery, proof)

    def _reopen(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> LocalRecoveryMetadataV1:
        metadata = self._io.reopen(recovery, authority)
        self._validate_metadata(recovery, metadata)
        return metadata

    @staticmethod
    def _validate_metadata(recovery: RecoveryPoint, metadata: LocalRecoveryMetadataV1) -> None:
        if (
            metadata.binding != recovery.binding
            or metadata.plan_identity != recovery.plan_identity
            or metadata.materialization_identity != recovery.materialization_identity
            or metadata.source_state != recovery.source_state
            or metadata.target_state != recovery.target_state
        ):
            raise ValueError("external-boot recovery metadata does not match recovery point")


def recovery_directory_name(
    reference: OpaqueProviderRef, binding: ExternalBootActivationBinding
) -> str:
    """Resolve a closed recovery token to its owner-derived directory name."""
    parts = reference.ref.split("/")
    if len(parts) != 3 or parts[0] != "local-recovery-v1":
        raise ValueError("external-boot recovery reference is malformed")
    if parts[1] != binding.system_id or parts[2] != binding.activation_id:
        raise ValueError("external-boot recovery reference owner does not match binding")
    return f"{parts[1]}.{parts[2]}"


_INTENT_NAME = "intent.json"
_INITIAL_INTENT_TEMPORARY_NAME = ".intent.initial"
_TOMBSTONE_NAME = "tombstone.json"
_MAX_METADATA_BYTES = 262_144


def _metadata_bytes(metadata: LocalRecoveryMetadataV1) -> bytes:
    return json.dumps(
        metadata.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _pre_stop_bytes(intent: LocalPreStopIntentV1) -> bytes:
    return json.dumps(
        intent.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _tombstone_bytes(tombstone: CleanupTombstoneV1) -> bytes:
    return json.dumps(
        tombstone.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _read_private_file(directory_fd: int, name: str, *, sync: bool = False) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or opened.st_size > _MAX_METADATA_BYTES
        ):
            raise ValueError("recovery evidence is not an owned private regular file")
        data = os.read(fd, _MAX_METADATA_BYTES + 1)
        if sync:
            os.fsync(fd)
        return data
    finally:
        os.close(fd)


class RecoveryMetadataStore:
    """Descriptor-relative publisher for one provider-owned recovery root."""

    def __init__(self, root: Path) -> None:
        self._root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _require_private_owned_directory(self._root_fd, "recovery root")
        except BaseException:
            os.close(self._root_fd)
            raise

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> RecoveryMetadataStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def publish(self, metadata: LocalRecoveryMetadataV1) -> OpaqueProviderRef:
        """Publish canonical intent with file, directory, rename, and parent fsyncs."""
        self._require_open()
        reference = _recovery_ref(metadata.binding)
        final_name = recovery_directory_name(reference, metadata.binding)
        existing = self._try_read(final_name)
        if existing is not None:
            if existing != metadata:
                raise ValueError("existing recovery metadata conflicts with requested point")
            return reference
        partial_name = f".{final_name}.partial"
        directory_fd = self._open_or_create_partial(partial_name)
        try:
            _publish_initial_intent(
                directory_fd,
                _metadata_bytes(metadata),
                conflict="recovery partial is not the exact owned intent",
            )
        finally:
            os.close(directory_fd)
        os.rename(partial_name, final_name, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
        os.fsync(self._root_fd)
        reopened = self._read_named(final_name)
        if reopened != metadata:
            raise ValueError("published recovery metadata failed exact reopen")
        return reference

    def publish_pre_stop(self, intent: LocalPreStopIntentV1) -> OpaqueProviderRef:
        """Durably publish the first partial entry before any provider mutation."""
        self._require_open()
        reference = _recovery_ref(intent.binding)
        final_name = recovery_directory_name(reference, intent.binding)
        if self._try_read(final_name) is not None:
            raise ValueError("complete recovery metadata already exists")
        partial_name = f".{final_name}.partial"
        directory_fd = self._open_or_create_partial(partial_name)
        try:
            _publish_initial_intent(
                directory_fd,
                _pre_stop_bytes(intent),
                conflict="recovery partial is not the exact pre-stop intent",
            )
        finally:
            os.close(directory_fd)
        os.fsync(self._root_fd)
        if self.reopen_pre_stop(reference, intent.binding) != intent:
            raise ValueError("pre-stop intent failed exact reopen")
        return reference

    def reopen_pre_stop(
        self, reference: OpaqueProviderRef, binding: ExternalBootActivationBinding
    ) -> LocalPreStopIntentV1:
        self._require_open()
        final_name = recovery_directory_name(reference, binding)
        directory_fd = _open_private_directory(self._root_fd, f".{final_name}.partial")
        try:
            return self._read_pre_stop(directory_fd)
        finally:
            os.close(directory_fd)

    def complete_preparation(
        self,
        reference: OpaqueProviderRef,
        intent: LocalPreStopIntentV1,
        metadata: LocalRecoveryMetadataV1,
    ) -> LocalRecoveryMetadataV1:
        """Replace exact partial intent and publish complete recovery metadata atomically."""
        self._require_open()
        if not _metadata_extends_intent(metadata, intent):
            raise ValueError("complete recovery metadata does not extend pre-stop intent")
        final_name = recovery_directory_name(reference, intent.binding)
        partial_name = f".{final_name}.partial"
        directory_fd = _open_private_directory(self._root_fd, partial_name)
        try:
            if self._read_pre_stop(directory_fd) != intent:
                raise ValueError("pre-stop intent changed before completion")
            temporary = ".intent.complete"
            _replace_private_file(
                directory_fd,
                temporary,
                _INTENT_NAME,
                _metadata_bytes(metadata),
            )
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.rename(partial_name, final_name, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
        os.fsync(self._root_fd)
        reopened = self._read_named(final_name)
        if reopened != metadata:
            raise ValueError("completed recovery metadata failed exact reopen")
        return reopened

    def reopen(
        self, reference: OpaqueProviderRef, binding: ExternalBootActivationBinding
    ) -> LocalRecoveryMetadataV1:
        self._require_open()
        return self._read_named(recovery_directory_name(reference, binding))

    def record_phase(
        self,
        reference: OpaqueProviderRef,
        binding: ExternalBootActivationBinding,
        expected: LocalRecoveryMetadataV1,
        phase: RecoveryPhase,
    ) -> LocalRecoveryMetadataV1:
        self._require_open()
        name = recovery_directory_name(reference, binding)
        directory_fd = _open_private_directory(self._root_fd, name)
        try:
            if self._read(directory_fd) != expected:
                raise ValueError("recovery metadata changed before phase publication")
            updated = expected.model_copy(update={"phase": phase})
            temporary = ".intent.next"
            _replace_private_file(
                directory_fd,
                temporary,
                _INTENT_NAME,
                _metadata_bytes(updated),
            )
            os.fsync(directory_fd)
            return updated
        finally:
            os.close(directory_fd)

    def publish_tombstone(
        self,
        reference: OpaqueProviderRef,
        binding: ExternalBootActivationBinding,
        expected: LocalRecoveryMetadataV1,
        point_digest: Digest,
    ) -> CleanupTombstoneV1:
        """Atomically replace recovered metadata with accounted cleanup evidence."""
        self._require_open()
        name = recovery_directory_name(reference, binding)
        directory_fd = _open_private_directory(self._root_fd, name)
        tombstone = CleanupTombstoneV1(binding=binding, point_digest=point_digest)
        try:
            if expected.binding != binding or self._read(directory_fd) != expected:
                raise ValueError("recovery metadata changed before cleanup")
            if expected.phase != "recovered":
                raise ValueError("recovery must complete before cleanup")
            temporary = ".tombstone.next"
            _replace_private_file(
                directory_fd,
                temporary,
                _TOMBSTONE_NAME,
                _tombstone_bytes(tombstone),
            )
            os.unlink(_INTENT_NAME, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.fsync(self._root_fd)
        if self._read_tombstone_named(name) != tombstone:
            raise ValueError("cleanup tombstone failed exact reopen")
        return tombstone

    def finalize_tombstone(
        self,
        reference: OpaqueProviderRef,
        recovery: RecoveryPoint,
        proof: FinalizeCleanupProof,
    ) -> None:
        """Delete an exact tombstone or confirm the exact U1a post-delete retry."""
        self._require_open()
        name = recovery_directory_name(reference, recovery.binding)
        expected = CleanupTombstoneV1(
            binding=recovery.binding,
            point_digest=LocalLibvirtExternalBoot.point_digest(recovery),
        )
        if proof.binding != recovery.binding or proof.point_digest != expected.point_digest:
            raise ValueError("cleanup finalization proof does not match recovery point")
        try:
            actual = self._read_tombstone_named(name)
        except FileNotFoundError:
            # Absence is success only for the closed exact mutation-started proof
            # re-presented by #2140 for the still-current operation.
            return
        if actual != expected:
            raise ValueError("cleanup tombstone does not match recovery point")
        directory_fd = _open_private_directory(self._root_fd, name)
        try:
            if os.listdir(directory_fd) != [_TOMBSTONE_NAME]:
                raise ValueError("cleanup tombstone directory contains unexpected payload")
            if self._read_tombstone(directory_fd) != expected:
                raise ValueError("cleanup tombstone changed before finalization")
            os.unlink(_TOMBSTONE_NAME, dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=self._root_fd)
        os.fsync(self._root_fd)
        try:
            _open_private_directory(self._root_fd, name)
        except FileNotFoundError:
            return
        raise ValueError("cleanup tombstone remained after finalization")

    def _open_or_create_partial(self, name: str) -> int:
        try:
            os.mkdir(name, 0o700, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        except FileExistsError:
            pass
        return _open_private_directory(self._root_fd, name)

    def _try_read(self, name: str) -> LocalRecoveryMetadataV1 | None:
        try:
            return self._read_named(name)
        except FileNotFoundError:
            return None

    def _read_named(self, name: str) -> LocalRecoveryMetadataV1:
        directory_fd = _open_private_directory(self._root_fd, name)
        try:
            return self._read(directory_fd)
        finally:
            os.close(directory_fd)

    def _read_tombstone_named(self, name: str) -> CleanupTombstoneV1:
        directory_fd = _open_private_directory(self._root_fd, name)
        try:
            return self._read_tombstone(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _read_tombstone(directory_fd: int) -> CleanupTombstoneV1:
        data = _read_private_file(directory_fd, _TOMBSTONE_NAME)
        tombstone = CleanupTombstoneV1.model_validate_json(data)
        if _tombstone_bytes(tombstone) != data:
            raise ValueError("cleanup tombstone is not canonical JSON")
        return tombstone

    @staticmethod
    def _read(directory_fd: int) -> LocalRecoveryMetadataV1:
        data = _read_private_file(directory_fd, _INTENT_NAME)
        metadata = LocalRecoveryMetadataV1.model_validate_json(data)
        if _metadata_bytes(metadata) != data:
            raise ValueError("recovery intent is not canonical JSON")
        return metadata

    @staticmethod
    def _read_pre_stop(directory_fd: int) -> LocalPreStopIntentV1:
        data = _read_private_file(directory_fd, _INTENT_NAME)
        intent = LocalPreStopIntentV1.model_validate_json(data)
        if _pre_stop_bytes(intent) != data:
            raise ValueError("pre-stop intent is not canonical JSON")
        return intent

    def _require_open(self) -> None:
        if self._root_fd < 0:
            raise ValueError("recovery metadata store is closed")


def _recovery_ref(binding: ExternalBootActivationBinding) -> OpaqueProviderRef:
    return OpaqueProviderRef(ref=f"local-recovery-v1/{binding.system_id}/{binding.activation_id}")


def _metadata_extends_intent(
    metadata: LocalRecoveryMetadataV1, intent: LocalPreStopIntentV1
) -> bool:
    shared = (
        "binding",
        "plan_identity",
        "materialization_identity",
        "release",
        "materialized_modules",
        "materialized_modules_sha256",
        "materialized_modules_bytes",
        "source_xml_sha256",
        "source_xml",
        "source_definition",
        "source_boot",
        "target_boot",
        "prior_power",
    )
    return all(getattr(metadata, field) == getattr(intent, field) for field in shared)


def _require_private_owned_directory(fd: int, label: str) -> None:
    opened = os.fstat(fd)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_IMODE(opened.st_mode) != 0o700
        or opened.st_uid != os.geteuid()
    ):
        raise ValueError(f"{label} must be an owner-only service-owned directory")


def _open_private_directory(parent_fd: int, name: str) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        _require_private_owned_directory(fd, "recovery directory")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_exclusive(directory_fd: int, name: str, data: bytes) -> None:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError("short write while publishing recovery intent")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_private_file(directory_fd: int, temporary: str, final: str, data: bytes) -> None:
    """Publish through a retryable, authenticated temporary file."""
    try:
        _write_exclusive(directory_fd, temporary, data)
    except FileExistsError:
        existing = _read_private_file(directory_fd, temporary, sync=True)
        if existing != data:
            os.unlink(temporary, dir_fd=directory_fd)
            _write_exclusive(directory_fd, temporary, data)
    os.rename(temporary, final, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)


def _publish_initial_intent(directory_fd: int, data: bytes, *, conflict: str) -> None:
    entries = set(os.listdir(directory_fd))
    expected = {_INTENT_NAME, _INITIAL_INTENT_TEMPORARY_NAME}
    if entries - expected or len(entries) > 1:
        raise ValueError(conflict)
    if _INTENT_NAME in entries:
        if _read_private_file(directory_fd, _INTENT_NAME) != data:
            raise ValueError(conflict)
        return
    _replace_private_file(
        directory_fd,
        _INITIAL_INTENT_TEMPORARY_NAME,
        _INTENT_NAME,
        data,
    )
    os.fsync(directory_fd)


def _sync_phase(io: ModulePublicationIO, phase: PublicationPhase) -> None:
    io.guest_sync()
    io.record_phase(phase)


def _move_with_reclassification(
    io: ModulePublicationIO,
    move: Callable[[], None],
    *,
    before: ModuleLayout,
    after: ModuleLayout,
    after_phase: PublicationPhase,
) -> None:
    """Retry a no-effect move or record its exact after-effect layout."""
    try:
        move()
    except OSError as exc:
        io.require_inactive()
        observed = io.observe_layout()
        if observed == before:
            move()
            return
        if observed == after:
            _sync_phase(io, after_phase)
            return
        raise ValueError("external-boot module publication conflict") from exc


def advance_module_publication(
    io: ModulePublicationIO,
    *,
    phase: PublicationPhase,
    layout: ModuleLayout,
    prior: ComponentState,
    desired: ComponentState,
) -> None:
    """Perform the sole ADR-0586 action allowed by a present-tree restart row."""
    io.require_inactive()
    if phase == PublicationPhase.OLD_ASIDE and layout == ModuleLayout(None, desired, prior):
        try:
            io.move_staging_to_live()
        except OSError as exc:
            io.require_inactive()
            observed = io.observe_layout()
            if observed == ModuleLayout(None, desired, prior):
                io.record_phase(PublicationPhase.ROLLBACK_READY)
                return
            if observed == ModuleLayout(desired, None, prior):
                _sync_phase(io, PublicationPhase.NEW_LIVE)
                return
            raise ValueError("external-boot module publication conflict") from exc
        return
    rows = {
        (PublicationPhase.MOVE_READY, ModuleLayout(prior, desired, None)): lambda: (
            _move_with_reclassification(
                io,
                io.move_live_to_old,
                before=ModuleLayout(prior, desired, None),
                after=ModuleLayout(None, desired, prior),
                after_phase=PublicationPhase.OLD_ASIDE,
            )
        ),
        (PublicationPhase.MOVE_READY, ModuleLayout(None, desired, prior)): lambda: _sync_phase(
            io, PublicationPhase.OLD_ASIDE
        ),
        (PublicationPhase.OLD_ASIDE, ModuleLayout(desired, None, prior)): lambda: _sync_phase(
            io, PublicationPhase.NEW_LIVE
        ),
        (PublicationPhase.ROLLBACK_READY, ModuleLayout(None, desired, prior)): lambda: (
            _move_with_reclassification(
                io,
                io.move_old_to_live,
                before=ModuleLayout(None, desired, prior),
                after=ModuleLayout(prior, desired, None),
                after_phase=PublicationPhase.ROLLBACK_COMPLETE,
            )
        ),
        (PublicationPhase.ROLLBACK_READY, ModuleLayout(prior, desired, None)): lambda: _sync_phase(
            io, PublicationPhase.ROLLBACK_COMPLETE
        ),
        (PublicationPhase.ROLLBACK_COMPLETE, ModuleLayout(prior, desired, None)): lambda: None,
        (PublicationPhase.NEW_LIVE, ModuleLayout(desired, None, prior)): io.remove_old,
        (PublicationPhase.NEW_LIVE, ModuleLayout(desired, None, None)): lambda: _sync_phase(
            io, PublicationPhase.PUBLICATION_COMPLETE
        ),
        (PublicationPhase.PUBLICATION_COMPLETE, ModuleLayout(desired, None, None)): lambda: None,
    }
    try:
        action = rows[(PublicationPhase(phase), layout)]
    except (KeyError, ValueError) as exc:
        raise ValueError("external-boot module publication conflict") from exc
    action()


def advance_absence_publication(
    io: ModulePublicationIO,
    *,
    phase: PublicationPhase,
    layout: ModuleLayout,
    prior: ComponentState,
) -> None:
    """Perform the sole ADR-0586 action allowed by an absent-tree restart row."""
    io.require_inactive()
    rows = {
        (PublicationPhase.MOVE_READY, ModuleLayout(prior, None, None)): lambda: (
            _move_with_reclassification(
                io,
                io.move_live_to_old,
                before=ModuleLayout(prior, None, None),
                after=ModuleLayout(None, None, prior),
                after_phase=PublicationPhase.ABSENCE_LIVE,
            )
        ),
        (PublicationPhase.MOVE_READY, ModuleLayout(None, None, prior)): lambda: _sync_phase(
            io, PublicationPhase.ABSENCE_LIVE
        ),
        (PublicationPhase.MOVE_READY, ModuleLayout(None, None, None)): lambda: io.record_phase(
            PublicationPhase.ABSENCE_COMPLETE
        ),
        (PublicationPhase.ABSENCE_LIVE, ModuleLayout(None, None, prior)): lambda: io.record_phase(
            PublicationPhase.ABSENCE_COMPLETE
        ),
        (PublicationPhase.ABSENCE_COMPLETE, ModuleLayout(None, None, prior)): io.remove_old,
        (PublicationPhase.ABSENCE_COMPLETE, ModuleLayout(None, None, None)): lambda: _sync_phase(
            io, PublicationPhase.ABSENCE_CLEANED
        ),
        (PublicationPhase.ABSENCE_CLEANED, ModuleLayout(None, None, None)): lambda: None,
    }
    try:
        action = rows[(PublicationPhase(phase), layout)]
    except (KeyError, ValueError) as exc:
        raise ValueError("external-boot module absence conflict") from exc
    action()


def render_target_xml(source: str, *, kernel: str, initrd: str | None, cmdline: str) -> str:
    """Return source domain XML with only the direct-boot projection replaced."""
    if unicodedata.normalize("NFC", source) != source:
        raise ValueError("domain XML must be NFC")
    try:
        root = _safe_fromstring(source)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError("domain XML is malformed or forbidden") from exc
    if root.tag != "domain":
        raise ValueError("domain XML must have a domain root")
    os_element = root.find("os")
    if os_element is None:
        os_element = ET.SubElement(root, "os")
    for tag in ("kernel", "initrd", "cmdline"):
        element = os_element.find(tag)
        if element is not None:
            os_element.remove(element)
    ET.SubElement(os_element, "kernel").text = kernel
    if initrd is not None:
        ET.SubElement(os_element, "initrd").text = initrd
    ET.SubElement(os_element, "cmdline").text = cmdline
    register_kdive_namespace()
    register_qemu_namespace()
    return ET.tostring(root, encoding="unicode")
