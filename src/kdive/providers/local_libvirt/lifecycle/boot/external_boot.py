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
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, BinaryIO, Literal, Protocol, cast
from uuid import UUID

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kdive.providers.local_libvirt.lifecycle.boot.readiness import ReadinessResult
from kdive.providers.local_libvirt.lifecycle.boot.recovery import (
    MAX_ARCHIVE_BYTES,
    MAX_ENTRIES,
    MAX_REGULAR_BYTES,
    AbsentModuleCapture,
    GuestRecoveryWriter,
    GuestTreeEntry,
    KernelBundleSource,
    ModuleArchiveCapture,
    ModuleCapture,
    RecoveryArchiveSink,
    RecoveryArchiveSource,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    ClosedDomainInspection,
    ExpectedOperationOwnership,
    LocalExternalBootOperationLease,
    LocalExternalBootSession,
    LocalExternalBootSessionFactory,
    TreeCursor,
)
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ActivationOwnership,
    ComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    KernelRelease,
    OpaqueProviderRef,
    PresentComponentState,
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
    release: KernelRelease
    materialized_modules: OpaqueProviderRef
    materialized_modules_sha256: Digest
    materialized_modules_bytes: Annotated[int, Field(ge=0)]
    source_xml_sha256: Digest
    source_xml: str
    source_definition: Digest
    source_boot: Digest
    target_boot: Digest
    target_projection_sha256: Digest
    target_xml_sha256: Digest
    target_xml: str
    expected_running: RunningKernelObservation
    source_state: ProviderStateIdentity
    target_state: ProviderStateIdentity
    prior_power: Literal["running", "inactive"]
    capture: ModuleCapture
    phase: RecoveryPhase

    @model_validator(mode="after")
    def _domain_xml_matches_digests(self) -> LocalRecoveryMetadataV1:
        xml_values = (self.source_xml, self.target_xml)
        if any(unicodedata.normalize("NFC", value) != value for value in xml_values):
            raise ValueError("source and target domain XML must be NFC")
        digest = "sha256:" + hashlib.sha256(self.source_xml.encode()).hexdigest()
        if digest != self.source_xml_sha256:
            raise ValueError("source domain XML digest does not match bytes")
        target_digest = "sha256:" + hashlib.sha256(self.target_xml.encode()).hexdigest()
        if target_digest != self.target_xml_sha256:
            raise ValueError("target domain XML digest does not match bytes")
        if self.expected_running.release != self.release:
            raise ValueError("expected running release does not match recovery release")
        return self


class LocalPreStopIntentV1(_ClosedValue):
    """Information established without mutating libvirt or the guest overlay."""

    schema_: Literal["local-libvirt-pre-stop-intent-v1"] = Field(
        "local-libvirt-pre-stop-intent-v1", alias="schema"
    )
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    materialization_identity: Digest
    release: KernelRelease
    materialized_modules: OpaqueProviderRef
    materialized_modules_sha256: Digest
    materialized_modules_bytes: Annotated[int, Field(ge=0)]
    source_xml_sha256: Digest
    source_xml: str
    source_definition: Digest
    source_boot: Digest
    target_boot: Digest
    target_projection_sha256: Digest
    target_xml_sha256: Digest
    target_xml: str
    expected_running: RunningKernelObservation
    prior_power: Literal["running", "inactive"]

    @model_validator(mode="after")
    def _domain_xml_matches_digests(self) -> LocalPreStopIntentV1:
        xml_values = (self.source_xml, self.target_xml)
        if any(unicodedata.normalize("NFC", value) != value for value in xml_values):
            raise ValueError("source and target domain XML must be NFC")
        digest = "sha256:" + hashlib.sha256(self.source_xml.encode()).hexdigest()
        if digest != self.source_xml_sha256:
            raise ValueError("source domain XML digest does not match bytes")
        target_digest = "sha256:" + hashlib.sha256(self.target_xml.encode()).hexdigest()
        if target_digest != self.target_xml_sha256:
            raise ValueError("target domain XML digest does not match bytes")
        if self.expected_running.release != self.release:
            raise ValueError("expected running release does not match recovery release")
        return self


class FinalizeCleanupProof(_ClosedValue):
    point_digest: Digest
    binding: ExternalBootActivationBinding
    # The authority's operation identifier is ``_AuthorityBinding.operation_identity``:
    # bounded text (ADR-0584), not a UUID. Deriving one here would make this field
    # synthesized, which is what the proof exists to avoid. ``attempt_id`` keeps its UUID
    # pattern because the journal's ``attempt_id`` really is a ``UUID``.
    operation_id: Annotated[str, Field(min_length=1, max_length=255)]
    attempt_id: Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
    journal_sequence: Annotated[int, Field(ge=1)]
    journal_digest: Digest
    # No default: with one, this field carried no information, because
    # ``AuthorityCommitContextV1.phase`` pins the same single literal and nothing could
    # distinguish a carried value from a defaulted one (ADR-0592).
    phase: Literal["mutation-started"]


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
    def open_tree(self, path: str, *, limit: int) -> TreeCursor: ...
    def lstatns(self, path: str) -> dict[str, int]: ...
    def readlink(self, path: str) -> str: ...
    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]: ...
    def open_regular(self, path: str, *, size: int) -> AbstractContextManager[BinaryIO]: ...
    def create_regular(self, content: BinaryIO, path: str, *, size: int) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def ln_s(self, target: str, linkname: str) -> None: ...
    def chmod(self, mode: int, path: str) -> None: ...
    def chown(self, owner: int, group: int, path: str) -> None: ...
    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None: ...
    def mv(self, source: str, destination: str) -> None: ...
    def rm_rf(self, path: str) -> None: ...
    def sync(self) -> None: ...


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
        seen: set[str] = set()
        with self._guest.open_tree(self._root, limit=MAX_ENTRIES) as cursor:
            for index, entry in enumerate(cursor, start=1):
                if index > MAX_ENTRIES:
                    raise ValueError("guest tree exceeds the entry-count bound")
                path = _guest_relative(entry.path)
                if path in seen:
                    raise ValueError("guest tree contains duplicate paths")
                seen.add(path)
                yield self._entry(path)

    @contextmanager
    def open_regular(self, path: str, size: int) -> Iterator[BinaryIO]:
        remote = self._remote(path)
        opened = self._guest.lstatns(remote)
        if opened["st_size"] != size or not stat.S_ISREG(opened["st_mode"]):
            raise ValueError("guest regular file changed before content read")
        with self._guest.open_regular(remote, size=size) as content:
            yield content

    def create_directory(self, entry: GuestTreeEntry) -> None:
        self._require_mutable()
        remote = self._remote(entry.path)
        self._guest.mkdir(remote)
        self._apply_metadata(remote, entry)

    def create_regular(self, entry: GuestTreeEntry, content: BinaryIO) -> None:
        self._require_mutable()
        self._guest.create_regular(content, self._remote(entry.path), size=entry.size)
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


@dataclass(frozen=True, slots=True)
class LocalObservedState:
    """A read of observed provider state that classifies instead of refusing.

    ``observe_running`` requires the exact running target and raises otherwise, so it
    cannot name the source, mixed, unreadable, or conflicting states an authority
    observation has to distinguish (ADR-0584). This read reports whatever is there and
    leaves classification to the caller. ``None`` means that half could not be read; it
    carries no provider message, path, or output.
    """

    definition: str | None
    modules: ComponentState | None
    active: bool | None


class LocalExternalBootOperation(Protocol):
    """One authenticated operation's privileged and durable capabilities."""

    def materialize(self, plan: ExternalBootPlan) -> ExternalBootMaterialization: ...
    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
    ) -> LocalRecoveryMetadataV1: ...
    def recovery_ref(self, binding: ExternalBootActivationBinding) -> OpaqueProviderRef: ...
    def reopen(self, recovery: RecoveryPoint) -> LocalRecoveryMetadataV1: ...
    def reopen_binding(self, binding: ExternalBootActivationBinding) -> LocalRecoveryMetadataV1: ...
    def observe_state(self, metadata: LocalRecoveryMetadataV1) -> LocalObservedState: ...
    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation: ...
    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def record_phase(
        self, metadata: LocalRecoveryMetadataV1, phase: RecoveryPhase
    ) -> LocalRecoveryMetadataV1: ...
    def cleanup_complete(self, recovery: RecoveryPoint) -> bool: ...
    def cleanup(self, metadata: LocalRecoveryMetadataV1, point_digest: Digest) -> None: ...


class LocalExternalBootIO(Protocol):
    """Opens one authenticated capability for each public coordinator call."""

    def open(
        self,
        authority: OpaqueProviderRef,
        expected: ExpectedOperationOwnership,
    ) -> AbstractContextManager[LocalExternalBootOperation]: ...
    def finalize_tombstone(self, recovery: RecoveryPoint, proof: FinalizeCleanupProof) -> None: ...


class LocalExternalBootMaterializer(Protocol):
    """Builds immutable local artifacts without owning guest recovery I/O."""

    def materialize(
        self, plan: ExternalBootPlan, session: LocalExternalBootSession
    ) -> ExternalBootMaterialization: ...
    def inspect_prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        inspection: ClosedDomainInspection,
        session: LocalExternalBootSession,
    ) -> LocalPreStopIntentV1: ...


type ResolveOperationLease = Callable[[OpaqueProviderRef], LocalExternalBootOperationLease]


class RealLocalExternalBootIO:
    """Concrete durable adapter that orders host mutations behind recovery evidence."""

    def __init__(
        self,
        recovery_root: Path,
        materializer: LocalExternalBootMaterializer,
        recovery_writer: GuestRecoveryWriter,
        resolve_operation_lease: ResolveOperationLease,
        session_factory: LocalExternalBootSessionFactory,
    ) -> None:
        self._recovery_root = recovery_root
        self._materializer = materializer
        self._recovery_writer = recovery_writer
        self._resolve_operation_lease = resolve_operation_lease
        self._session_factory = session_factory

    @contextmanager
    def open(
        self,
        authority: OpaqueProviderRef,
        expected: ExpectedOperationOwnership,
    ) -> Iterator[LocalExternalBootOperation]:
        lease = self._resolve_operation_lease(authority)
        session = self._session_factory.open(lease, expected)
        operation = _RealLocalExternalBootOperation(
            self._recovery_root,
            self._materializer,
            self._recovery_writer,
            session,
        )
        try:
            yield operation
        except BaseException as primary:
            try:
                session.close()
            except BaseException as close_error:
                primary.add_note(f"cleanup failed: {close_error!r}")
            raise
        else:
            session.close()

    def finalize_tombstone(self, recovery: RecoveryPoint, proof: FinalizeCleanupProof) -> None:
        with RecoveryMetadataStore(self._recovery_root) as store:
            store.finalize_tombstone(recovery.recovery_ref, recovery, proof)


class _RealLocalExternalBootOperation:
    def __init__(
        self,
        recovery_root: Path,
        materializer: LocalExternalBootMaterializer,
        recovery_writer: GuestRecoveryWriter,
        session: LocalExternalBootSession,
    ) -> None:
        self._recovery_root = recovery_root
        self._materializer = materializer
        self._recovery_writer = recovery_writer
        self._session = session

    def materialize(self, plan: ExternalBootPlan) -> ExternalBootMaterialization:
        return self._materializer.materialize(plan, self._session)

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
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
                inspection = self._session.inspect_closed()
                intent = self._materializer.inspect_prepare(
                    materialization, binding, inspection, self._session
                )
                _validate_preparation_owner(intent, materialization, binding)
                _validate_preparation_inspection(intent, inspection, retry=False)
                store.publish_pre_stop(intent)
            else:
                _validate_preparation_owner(intent, materialization, binding)
                _validate_preparation_inspection(intent, self._session.inspect_closed(), retry=True)
        self._session.stop_and_require_inactive()
        with RecoveryMetadataStore(self._recovery_root) as store:
            owned_sink = store.recovery_archive_sink(reference, intent)
        primary: BaseException | None = None
        try:
            with self._session.guest() as guest:
                tree = LibguestfsAuthenticatedGuestTree(
                    guest,
                    binding=binding,
                    release=intent.release,
                    root=f"/lib/modules/{intent.release}",
                    mutable=False,
                )
                capture_sink, owned_sink = owned_sink, None
                capture = self._recovery_writer.capture(tree, intent.release, capture_sink)
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if owned_sink is not None:
                try:
                    owned_sink.close()
                except BaseException as cleanup:
                    if primary is None:
                        raise
                    primary.add_note(f"recovery archive sink cleanup failed: {cleanup!r}")
        metadata = _complete_preparation_metadata(intent, materialization, capture)
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.complete_preparation(reference, intent, metadata)

    @staticmethod
    def recovery_ref(binding: ExternalBootActivationBinding) -> OpaqueProviderRef:
        return _recovery_ref(binding)

    def reopen(self, recovery: RecoveryPoint) -> LocalRecoveryMetadataV1:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.reopen(recovery.recovery_ref, recovery.binding)

    def reopen_binding(self, binding: ExternalBootActivationBinding) -> LocalRecoveryMetadataV1:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.reopen(_recovery_ref(binding), binding)

    def observe_state(self, metadata: LocalRecoveryMetadataV1) -> LocalObservedState:
        # Each half is read independently so a readable definition still classifies when
        # the guest tree is not reachable. A failed read becomes ``None`` rather than an
        # exception: the caller must be able to name "unreadable" as an outcome, and no
        # libvirt or libguestfs message may cross this boundary (ADR-0584).
        try:
            inspection = self._session.inspect_closed()
        except Exception:  # noqa: BLE001 - an unreadable definition is a classification
            return LocalObservedState(definition=None, modules=None, active=None)
        modules: ComponentState | None
        try:
            with self._session.guest() as opened_guest:
                tree = LibguestfsAuthenticatedGuestTree(
                    cast(_GuestfsTreeHandle, opened_guest),
                    binding=metadata.binding,
                    release=metadata.release,
                    root=f"/lib/modules/{metadata.release}",
                    mutable=False,
                )
                modules = self._recovery_writer.observe(tree, metadata.release)
        except Exception:  # noqa: BLE001 - an unreadable module tree is a classification
            modules = None
        return LocalObservedState(
            definition=inspection.source_boot_identity,
            modules=modules,
            active=inspection.active,
        )

    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        if self._host_state(metadata) != ("source", False):
            raise ValueError("external-boot module activation requires inactive source XML/power")
        desired = _present_component(metadata.target_state.modules, "target module state")
        prior = _layout_component(metadata.source_state.modules)
        with self._session.guest() as opened_guest:
            guest = cast(_GuestfsTreeHandle, opened_guest)
            publication = _SessionModulePublicationIO(
                guest,
                metadata,
                self._recovery_root,
                self._recovery_writer,
                self._session,
            )
            if metadata.phase == "pre-stop-intent":
                before = ModuleLayout(prior, None, None)
                staged = ModuleLayout(prior, desired, None)
                layout = publication.observe_layout()
                if layout == before:
                    source = self._kernel_bundle_source(metadata)
                    try:
                        publication.create_staging()
                        manifest = self._recovery_writer.install(
                            publication.staging_tree(),
                            metadata.release,
                            source,
                        )
                    finally:
                        source.close()
                    if manifest != desired.manifest or publication.observe_layout() != staged:
                        raise ValueError(
                            "external-boot staged target modules do not match metadata"
                        )
                elif layout != staged:
                    raise ValueError("external-boot target staging layout conflicts with metadata")
                publication.guest_sync()
                publication.record_phase(
                    PublicationPhase.MOVE_READY if prior is not None else PublicationPhase.OLD_ASIDE
                )
            self._finish_present_publication(publication, prior=prior, desired=desired)
            completed = publication.metadata
        self.record_phase(completed, "module-restored")

    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None:
        while metadata.phase == "module-restored":
            xml, active = self._host_state(metadata)
            if xml == "source" and not active:
                self._session.define_xml(metadata.target_xml)
                continue
            if xml == "target" and not active and metadata.prior_power == "inactive":
                self.record_phase(metadata, "target-defined")
                return
            if xml == "target" and not active and metadata.prior_power == "running":
                self._session.start()
                continue
            if xml == "target" and active and metadata.prior_power == "running":
                self._require_readiness()
                if self._host_state(metadata) != ("target", True):
                    raise ValueError("external-boot target changed during readiness")
                self.record_phase(metadata, "target-defined")
                return
            raise ValueError(
                "external-boot target XML/power state conflicts with recovery metadata"
            )

    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation:
        if self._host_state(metadata) != ("target", True):
            raise ValueError("external-boot observation requires exact running target XML/power")
        observed = self._session.observe_running()
        if observed != metadata.expected_running:
            raise ValueError("external-boot running kernel does not match recovery metadata")
        return observed

    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self._stop_for_recovery(metadata)
        target = _present_component(metadata.target_state.modules, "target module state")
        desired = _layout_component(metadata.source_state.modules)
        with self._session.guest() as opened_guest:
            guest = cast(_GuestfsTreeHandle, opened_guest)
            publication = _SessionModulePublicationIO(
                guest,
                metadata,
                self._recovery_root,
                self._recovery_writer,
                self._session,
            )
            if metadata.phase in {"target-defined", "module-restored"}:
                terminal = ModuleLayout(desired, None, None)
                layout = publication.observe_layout()
                if metadata.phase == "module-restored" and layout == terminal:
                    return
                if isinstance(metadata.capture, AbsentModuleCapture):
                    if layout != ModuleLayout(target, None, None):
                        raise ValueError(
                            "external-boot absence recovery layout conflicts with metadata"
                        )
                    publication.guest_sync()
                    publication.record_phase(PublicationPhase.MOVE_READY)
                else:
                    staged = ModuleLayout(target, desired, None)
                    if layout == ModuleLayout(target, None, None):
                        source = self._recovery_archive_source(metadata)
                        try:
                            publication.create_staging()
                            manifest = self._recovery_writer.restore(
                                publication.staging_tree(),
                                metadata.release,
                                metadata.capture,
                                source,
                            )
                        finally:
                            source.close()
                        expected = _present_component(
                            metadata.source_state.modules, "source module state"
                        )
                        if manifest != expected.manifest:
                            raise ValueError(
                                "external-boot staged recovery modules do not match metadata"
                            )
                    elif layout != staged:
                        raise ValueError(
                            "external-boot module recovery staging conflicts with metadata"
                        )
                    if publication.observe_layout() != staged:
                        raise ValueError(
                            "external-boot staged recovery modules changed before publication"
                        )
                    publication.guest_sync()
                    publication.record_phase(PublicationPhase.MOVE_READY)
            if isinstance(metadata.capture, AbsentModuleCapture):
                self._finish_absence_publication(publication, prior=target)
            else:
                assert desired is not None
                self._finish_present_publication(publication, prior=target, desired=desired)
            completed = publication.metadata
        self.record_phase(completed, "module-restored")

    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None:
        while metadata.phase == "module-restored":
            xml, active = self._host_state(metadata)
            if xml == "target" and not active:
                self._session.define_xml(metadata.source_xml)
                continue
            if xml == "source" and not active:
                self.record_phase(metadata, "source-restored")
                return
            raise ValueError(
                "external-boot source XML/power state conflicts with recovery metadata"
            )

    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None:
        while metadata.phase == "source-restored":
            xml, active = self._host_state(metadata)
            if xml == "source" and not active and metadata.prior_power == "inactive":
                self.record_phase(metadata, "recovered")
                return
            if xml == "source" and not active and metadata.prior_power == "running":
                self._session.start()
                continue
            if xml == "source" and active and metadata.prior_power == "running":
                self._require_readiness()
                if self._host_state(metadata) != ("source", True):
                    raise ValueError("external-boot source changed during readiness")
                self.record_phase(metadata, "recovered")
                return
            raise ValueError("external-boot restored power state conflicts with recovery metadata")

    def record_phase(
        self, metadata: LocalRecoveryMetadataV1, phase: RecoveryPhase
    ) -> LocalRecoveryMetadataV1:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.record_phase(
                _recovery_ref(metadata.binding), metadata.binding, metadata, phase
            )

    def cleanup_complete(self, recovery: RecoveryPoint) -> bool:
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.cleanup_complete(recovery.recovery_ref, recovery)

    def cleanup(self, metadata: LocalRecoveryMetadataV1, point_digest: Digest) -> None:
        with RecoveryMetadataStore(self._recovery_root) as store:
            reference = _recovery_ref(metadata.binding)
            if store.reopen(reference, metadata.binding) != metadata:
                raise ValueError("recovery metadata changed before cleanup")
            self._session.cleanup_payloads()
            store.publish_tombstone(reference, metadata.binding, metadata, point_digest)

    def _kernel_bundle_source(self, metadata: LocalRecoveryMetadataV1) -> KernelBundleSource:
        ownership = ActivationOwnership(
            system_id=metadata.binding.system_id,
            run_id=metadata.binding.run_id,
        )
        parts = _artifact_ref_parts(metadata.materialized_modules, ownership)
        descriptor = self._session.open_artifact(parts[4], os.O_RDONLY)
        try:
            return KernelBundleSource(
                descriptor,
                binding=metadata.binding,
                release=metadata.release,
                size=metadata.materialized_modules_bytes,
                digest=metadata.materialized_modules_sha256,
            )
        finally:
            os.close(descriptor)

    def _recovery_archive_source(
        self,
        metadata: LocalRecoveryMetadataV1,
    ) -> RecoveryArchiveSource:
        if not isinstance(metadata.capture, ModuleArchiveCapture):
            raise ValueError("absent module capture has no recovery archive")
        with RecoveryMetadataStore(self._recovery_root) as store:
            return store.recovery_archive_source(
                _recovery_ref(metadata.binding),
                metadata,
            )

    @staticmethod
    def _finish_present_publication(
        publication: _SessionModulePublicationIO,
        *,
        prior: ComponentState | None,
        desired: PresentComponentState,
    ) -> None:
        while publication.metadata.phase != PublicationPhase.PUBLICATION_COMPLETE:
            phase = PublicationPhase(publication.metadata.phase)
            advance_module_publication(
                publication,
                phase=phase,
                layout=publication.observe_layout(),
                prior=prior,
                desired=desired,
            )
        if publication.observe_layout() != ModuleLayout(desired, None, None):
            raise ValueError("external-boot completed module publication layout conflicts")

    @staticmethod
    def _finish_absence_publication(
        publication: _SessionModulePublicationIO,
        *,
        prior: PresentComponentState,
    ) -> None:
        while publication.metadata.phase != PublicationPhase.ABSENCE_CLEANED:
            phase = PublicationPhase(publication.metadata.phase)
            advance_absence_publication(
                publication,
                phase=phase,
                layout=publication.observe_layout(),
                prior=prior,
            )
        if publication.observe_layout() != ModuleLayout(None, None, None):
            raise ValueError("external-boot completed module absence layout conflicts")

    def _host_state(
        self, metadata: LocalRecoveryMetadataV1
    ) -> tuple[Literal["source", "target"], bool]:
        inspection = self._session.inspect_closed()
        if inspection.xml == metadata.source_xml.encode():
            return "source", inspection.active
        if inspection.xml == metadata.target_xml.encode():
            return "target", inspection.active
        raise ValueError("external-boot observed domain XML does not match recovery metadata")

    def _stop_for_recovery(self, metadata: LocalRecoveryMetadataV1) -> None:
        xml, active = self._host_state(metadata)
        if metadata.phase == "target-defined" and xml != "target":
            raise ValueError("external-boot recovery expected the target domain XML")
        if active and xml != "target":
            raise ValueError("external-boot refuses to stop an unexpected running domain")
        if not active:
            self._session.require_inactive()
            return
        try:
            self._session.stop_and_require_inactive()
        except Exception as primary:
            try:
                after = self._host_state(metadata)
            except Exception as observation_error:
                primary.add_note(f"post-stop host-state observation failed: {observation_error!r}")
                raise primary from None
            if after == (xml, False):
                self._session.require_inactive()
                return
            raise
        if self._host_state(metadata) != (xml, False):
            raise ValueError("external-boot stop did not preserve exact XML and inactivity")

    def _require_readiness(self) -> None:
        if self._session.readiness() != ReadinessResult(True, True, None):
            raise ValueError("external-boot readiness did not answer with exact success")


class _SessionModulePublicationIO:
    """Session-owned adapter for one exact three-name module publication."""

    def __init__(
        self,
        guest: _GuestfsTreeHandle,
        metadata: LocalRecoveryMetadataV1,
        recovery_root: Path,
        writer: GuestRecoveryWriter,
        session: LocalExternalBootSession,
    ) -> None:
        self._guest = guest
        self.metadata = metadata
        self._recovery_root = recovery_root
        self._writer = writer
        self._session = session
        base = f"/lib/modules/.kdive-{metadata.binding.activation_id}"
        self._live = f"/lib/modules/{metadata.release}"
        self._staging = f"{base}-staging"
        self._old = f"{base}-old"

    def require_inactive(self) -> None:
        self._session.require_inactive()

    def observe_layout(self) -> ModuleLayout:
        return ModuleLayout(
            self._observe(self._live),
            self._observe(self._staging),
            self._observe(self._old),
        )

    def create_staging(self) -> None:
        self._guest.mkdir(self._staging)

    def staging_tree(self) -> LibguestfsAuthenticatedGuestTree:
        return LibguestfsAuthenticatedGuestTree(
            self._guest,
            binding=self.metadata.binding,
            release=self.metadata.release,
            root=self._staging,
            mutable=True,
        )

    def move_live_to_old(self) -> None:
        self._guest.mv(self._live, self._old)

    def move_staging_to_live(self) -> None:
        self._guest.mv(self._staging, self._live)

    def move_old_to_live(self) -> None:
        self._guest.mv(self._old, self._live)

    def remove_old(self) -> None:
        self._guest.rm_rf(self._old)

    def guest_sync(self) -> None:
        self._guest.sync()

    def record_phase(self, phase: PublicationPhase) -> None:
        with RecoveryMetadataStore(self._recovery_root) as store:
            self.metadata = store.record_phase(
                _recovery_ref(self.metadata.binding),
                self.metadata.binding,
                self.metadata,
                cast(RecoveryPhase, phase.value),
            )

    def _observe(self, root: str) -> ComponentState | None:
        tree = LibguestfsAuthenticatedGuestTree(
            self._guest,
            binding=self.metadata.binding,
            release=self.metadata.release,
            root=root,
            mutable=False,
        )
        kind = tree.root_kind()
        if kind == "absent":
            return None
        if kind != "directory":
            raise ValueError("external-boot module publication name is not a directory")
        return self._writer.observe(tree, self.metadata.release)


def _layout_component(state: ComponentState) -> PresentComponentState | None:
    return state if isinstance(state, PresentComponentState) else None


def _present_component(state: ComponentState, label: str) -> PresentComponentState:
    if not isinstance(state, PresentComponentState):
        raise ValueError(f"external-boot {label} must be present")
    return state


def _validate_preparation_owner(
    value: LocalPreStopIntentV1 | LocalRecoveryMetadataV1,
    materialization: ExternalBootMaterialization,
    binding: ExternalBootActivationBinding,
) -> None:
    if (
        value.binding != binding
        or value.materialization_identity != materialization.identity
        or value.plan_identity != materialization.plan_identity
        or value.expected_running != materialization.kernel_observation
    ):
        raise ValueError("pre-stop intent does not match activation")


def _complete_preparation_metadata(
    intent: LocalPreStopIntentV1,
    materialization: ExternalBootMaterialization,
    capture: ModuleCapture,
) -> LocalRecoveryMetadataV1:
    source_modules: ComponentState
    if isinstance(capture, AbsentModuleCapture):
        source_modules = AbsentComponentState()
    else:
        source_modules = PresentComponentState(manifest=capture.manifest)
    return LocalRecoveryMetadataV1.model_validate(
        intent.model_dump(exclude={"schema_"}, by_alias=True)
        | {
            "source_state": ProviderStateIdentity(
                definition=intent.source_boot,
                modules=source_modules,
            ),
            "target_state": ProviderStateIdentity(
                definition=intent.target_boot,
                modules=PresentComponentState(manifest=materialization.installed_module_tree),
            ),
            "capture": capture,
            "phase": "pre-stop-intent",
        }
    )


def _validate_preparation_inspection(
    intent: LocalPreStopIntentV1,
    inspection: ClosedDomainInspection,
    *,
    retry: bool,
) -> None:
    expected_prior = "running" if inspection.active else "inactive"
    if (
        intent.source_xml.encode() != inspection.xml
        or intent.source_definition != inspection.definition_identity
        or intent.source_boot != inspection.source_boot_identity
        or (not retry and intent.prior_power != expected_prior)
    ):
        raise ValueError("closed domain inspection does not match pre-stop intent")


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
        expected = ExpectedOperationOwnership(
            UUID(plan.ownership.system_id), UUID(plan.ownership.run_id), None
        )
        with self._io.open(authority, expected) as operation:
            materialization = operation.materialize(plan)
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
        expected = _expected_binding(binding)
        with self._io.open(authority, expected) as operation:
            if (
                binding.system_id != materialization.ownership.system_id
                or binding.run_id != materialization.ownership.run_id
            ):
                raise ValueError("external-boot binding does not match materialization")
            metadata = operation.prepare(materialization, binding)
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
                recovery_ref=operation.recovery_ref(binding),
                source_state=metadata.source_state,
                target_state=metadata.target_state,
            )
            self._validate_metadata(point, metadata)
            return point

    def activate(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        with self._io.open(authority, _expected_binding(recovery.binding)) as operation:
            metadata = self._reopen(operation, recovery)
            resumable = {
                "pre-stop-intent",
                "move-ready",
                "old-aside",
                "rollback-ready",
                "rollback-complete",
                "new-live",
                "publication-complete",
                "module-restored",
                "target-defined",
            }
            if metadata.phase not in resumable:
                raise ValueError("external-boot activation phase is not resumable")
            if metadata.phase == "target-defined":
                return
            if metadata.phase != "module-restored":
                operation.activate_modules(metadata)
                metadata = self._reopen(operation, recovery)
            if metadata.phase == "module-restored":
                operation.define_target(metadata)

    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation:
        with self._io.open(authority, _expected_binding(recovery.binding)) as operation:
            metadata = self._reopen(operation, recovery)
            if metadata.phase != "target-defined":
                raise ValueError("external-boot target-defined evidence is required")
            return operation.observe_running(metadata)

    def recover(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        with self._io.open(authority, _expected_binding(recovery.binding)) as operation:
            metadata = self._reopen(operation, recovery)
            if metadata.phase in {"recovered", "cleaned"}:
                return
            if metadata.phase not in {
                "target-defined",
                "move-ready",
                "old-aside",
                "rollback-ready",
                "rollback-complete",
                "new-live",
                "publication-complete",
                "absence-live",
                "absence-complete",
                "absence-cleaned",
                "module-restored",
                "source-restored",
            }:
                raise ValueError("external-boot recovery phase is not resumable")
            if metadata.phase not in {"source-restored"}:
                operation.recover_modules(metadata)
                metadata = self._reopen(operation, recovery)
            if metadata.phase == "module-restored":
                operation.define_source(metadata)
                metadata = self._reopen(operation, recovery)
            if metadata.phase == "source-restored":
                operation.restore_power(metadata)

    def cleanup_is_accounted(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> bool:
        """Whether accounted cleanup evidence for this exact point already exists.

        The authority adapter asks before cleaning, because only a tombstone that *this*
        commit publishes is safe to finalize. ``publish_tombstone`` writes ``tombstone.json``
        and then unlinks ``intent.json``; a crash between those leaves both present, and
        ``cleanup`` below then short-circuits without completing the unlink.
        ``RecoveryMetadataStore.finalize_tombstone`` refuses a directory holding anything but
        the tombstone, so finalizing that state raises — and a caller that finalized
        unconditionally would turn a harmless retry into a permanent failure.

        Kept separate from ``cleanup`` rather than returned by it: ``cleanup`` is part of the
        provider-neutral ``ExternalBootPorts`` protocol, which the remote provider also
        implements, and that signature is not this change's to widen.
        """
        with self._io.open(authority, _expected_binding(recovery.binding)) as operation:
            return operation.cleanup_complete(recovery)

    def cleanup(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        with self._io.open(authority, _expected_binding(recovery.binding)) as operation:
            if operation.cleanup_complete(recovery):
                return
            metadata = self._reopen(operation, recovery)
            if metadata.phase == "cleaned":
                return
            if metadata.phase != "recovered":
                raise ValueError("external-boot recovery must complete before cleanup")
            operation.cleanup(metadata, self.point_digest(recovery))

    def recovery_point(
        self, binding: ExternalBootActivationBinding, authority: OpaqueProviderRef
    ) -> RecoveryPoint:
        """Resolve the durable recovery point an activation already owns.

        The authority seam addresses an activation by its owner identities, not by a
        recovery point it never held, so the point is rebuilt from the durable record
        under the same authenticated lease every other operation uses.

        The recovery reference encodes only System and activation, so the durable record
        is checked against the whole requested binding here. Without that, a Run mismatch
        would surface two layers down as an opaque lease failure rather than at the seam
        that made the claim.
        """
        with self._io.open(authority, _expected_binding(binding)) as operation:
            metadata = operation.reopen_binding(binding)
            if metadata.binding != binding:
                raise ValueError("external-boot recovery record does not match the binding")
            return RecoveryPoint(
                binding=metadata.binding,
                plan_identity=metadata.plan_identity,
                materialization_identity=metadata.materialization_identity,
                recovery_ref=operation.recovery_ref(binding),
                source_state=metadata.source_state,
                target_state=metadata.target_state,
            )

    def observe_state(
        self, binding: ExternalBootActivationBinding, authority: OpaqueProviderRef
    ) -> LocalObservedState:
        """Read observed provider state for an activation without requiring a phase."""
        with self._io.open(authority, _expected_binding(binding)) as operation:
            metadata = operation.reopen_binding(binding)
            return operation.observe_state(metadata)

    def finalize_cleanup_tombstone(
        self,
        recovery: RecoveryPoint,
        proof: FinalizeCleanupProof,
        authority: OpaqueProviderRef,
    ) -> None:
        if proof.binding != recovery.binding or proof.point_digest != self.point_digest(recovery):
            raise ValueError("external-boot cleanup proof does not match recovery point")
        # The authority supplies the anchored mutation-started proof as
        # ``AuthorityCommitContextV1`` (ADR-0592).  The local seam deliberately does not
        # decode it; it compares the closed owner/point fields and handles present or
        # post-delete absence idempotently.
        self._io.finalize_tombstone(recovery, proof)

    def _reopen(
        self, operation: LocalExternalBootOperation, recovery: RecoveryPoint
    ) -> LocalRecoveryMetadataV1:
        metadata = operation.reopen(recovery)
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


def _expected_binding(binding: ExternalBootActivationBinding) -> ExpectedOperationOwnership:
    return ExpectedOperationOwnership(
        UUID(binding.system_id), UUID(binding.run_id), UUID(binding.activation_id)
    )


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

    def recovery_archive_sink(
        self,
        reference: OpaqueProviderRef,
        intent: LocalPreStopIntentV1,
    ) -> RecoveryArchiveSink:
        """Open an owner-bound archive sink beneath an exact partial intent."""
        self._require_open()
        final_name = recovery_directory_name(reference, intent.binding)
        directory_fd = _open_private_directory(self._root_fd, f".{final_name}.partial")
        try:
            if self._read_pre_stop(directory_fd) != intent:
                raise ValueError("pre-stop intent changed before recovery capture")
            return RecoveryArchiveSink(
                directory_fd,
                binding=intent.binding,
                release=intent.release,
            )
        finally:
            os.close(directory_fd)

    def recovery_archive_source(
        self,
        reference: OpaqueProviderRef,
        metadata: LocalRecoveryMetadataV1,
    ) -> RecoveryArchiveSource:
        """Open an owner-bound archive source beneath exact complete metadata."""
        self._require_open()
        if not isinstance(metadata.capture, ModuleArchiveCapture):
            raise ValueError("absent module capture has no recovery archive")
        directory_fd = _open_private_directory(
            self._root_fd,
            recovery_directory_name(reference, metadata.binding),
        )
        try:
            if self._read(directory_fd) != metadata:
                raise ValueError("recovery metadata changed before archive reopen")
            return RecoveryArchiveSource(
                directory_fd,
                binding=metadata.binding,
                release=metadata.release,
                capture=metadata.capture,
            )
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

    def cleanup_complete(self, reference: OpaqueProviderRef, recovery: RecoveryPoint) -> bool:
        """Return whether the exact recovery point already has a durable tombstone."""
        self._require_open()
        name = recovery_directory_name(reference, recovery.binding)
        try:
            actual = self._read_tombstone_named(name)
        except FileNotFoundError:
            return False
        expected = CleanupTombstoneV1(
            binding=recovery.binding,
            point_digest=LocalLibvirtExternalBoot.point_digest(recovery),
        )
        if actual != expected:
            raise ValueError("cleanup tombstone does not match recovery point")
        return True

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
        "target_projection_sha256",
        "target_xml_sha256",
        "target_xml",
        "expected_running",
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
        os.fsync(directory_fd)
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


def _remove_with_reclassification(
    io: ModulePublicationIO,
    *,
    before: ModuleLayout,
    after: ModuleLayout,
    after_phase: PublicationPhase,
) -> None:
    """Retry a no-effect removal or record its exact after-effect layout."""
    try:
        io.remove_old()
    except OSError as exc:
        io.require_inactive()
        observed = io.observe_layout()
        if observed == before:
            io.remove_old()
            return
        if observed == after:
            _sync_phase(io, after_phase)
            return
        raise ValueError("external-boot module removal conflict") from exc


def advance_module_publication(
    io: ModulePublicationIO,
    *,
    phase: PublicationPhase,
    layout: ModuleLayout,
    prior: ComponentState | None,
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
                if prior is None:
                    io.move_staging_to_live()
                    return
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
        (
            PublicationPhase.ROLLBACK_COMPLETE,
            ModuleLayout(prior, desired, None),
        ): lambda: io.record_phase(PublicationPhase.MOVE_READY),
        (PublicationPhase.NEW_LIVE, ModuleLayout(desired, None, prior)): lambda: (
            _remove_with_reclassification(
                io,
                before=ModuleLayout(desired, None, prior),
                after=ModuleLayout(desired, None, None),
                after_phase=PublicationPhase.PUBLICATION_COMPLETE,
            )
        ),
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
        (PublicationPhase.ABSENCE_COMPLETE, ModuleLayout(None, None, prior)): lambda: (
            _remove_with_reclassification(
                io,
                before=ModuleLayout(None, None, prior),
                after=ModuleLayout(None, None, None),
                after_phase=PublicationPhase.ABSENCE_CLEANED,
            )
        ),
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
