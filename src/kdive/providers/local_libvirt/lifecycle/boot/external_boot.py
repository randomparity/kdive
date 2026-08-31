"""Local-libvirt external-boot recovery state machine (ADR-0586)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unicodedata
import xml.etree.ElementTree as ET  # noqa: S405 - edits trusted domain structure after safe parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, BinaryIO, Literal, Protocol

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kdive.providers.local_libvirt.lifecycle.boot.recovery import GuestTreeEntry, ModuleCapture
from kdive.providers.ports.external_boot import (
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
    source_state: ProviderStateIdentity
    target_state: ProviderStateIdentity
    prior_power: Literal["running", "inactive"]
    capture: ModuleCapture
    phase: RecoveryPhase

    @model_validator(mode="after")
    def _source_xml_matches_digest(self) -> LocalRecoveryMetadataV1:
        if unicodedata.normalize("NFC", self.source_xml) != self.source_xml:
            raise ValueError("source domain XML must be NFC")
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


@dataclass(frozen=True, slots=True)
class ModuleLayout:
    live: ComponentState | None
    staging: ComponentState | None
    old: ComponentState | None


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
    """Privileged host operations whose transports remain injected for production assembly."""

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization: ...
    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> LocalRecoveryMetadataV1: ...
    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation: ...
    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None: ...
    def cleanup_payloads(self, metadata: LocalRecoveryMetadataV1) -> None: ...


class RealLocalExternalBootIO:
    """Production state/persistence adapter over host libvirt, guestfs, and artifact seams."""

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
        metadata = self._host.prepare(materialization, binding, authority)
        if (
            metadata.binding != binding
            or metadata.materialization_identity != materialization.identity
        ):
            raise ValueError("prepared recovery metadata does not match activation")
        with RecoveryMetadataStore(self._recovery_root) as store:
            reference = store.publish(metadata)
            return store.reopen(reference, binding)

    @staticmethod
    def recovery_ref(binding: ExternalBootActivationBinding) -> OpaqueProviderRef:
        return _recovery_ref(binding)

    def reopen(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> LocalRecoveryMetadataV1:
        del authority  # Authenticated by #2140; the provider never decodes authority refs.
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
_TOMBSTONE_NAME = "tombstone.json"
_MAX_METADATA_BYTES = 262_144


def _metadata_bytes(metadata: LocalRecoveryMetadataV1) -> bytes:
    return json.dumps(
        metadata.model_dump(mode="json", by_alias=True),
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


def _read_private_file(directory_fd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.geteuid()
            or opened.st_size > _MAX_METADATA_BYTES
        ):
            raise ValueError("recovery evidence is not an owned private regular file")
        return os.read(fd, _MAX_METADATA_BYTES + 1)
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
            entries = os.listdir(directory_fd)
            if entries:
                if entries != [_INTENT_NAME] or self._read(directory_fd) != metadata:
                    raise ValueError("recovery partial is not the exact owned intent")
            else:
                _write_exclusive(directory_fd, _INTENT_NAME, _metadata_bytes(metadata))
                os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.rename(partial_name, final_name, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
        os.fsync(self._root_fd)
        reopened = self._read_named(final_name)
        if reopened != metadata:
            raise ValueError("published recovery metadata failed exact reopen")
        return reference

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
            _write_exclusive(directory_fd, temporary, _metadata_bytes(updated))
            os.rename(temporary, _INTENT_NAME, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
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
            _write_exclusive(directory_fd, temporary, _tombstone_bytes(tombstone))
            os.rename(
                temporary,
                _TOMBSTONE_NAME,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
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

    def _require_open(self) -> None:
        if self._root_fd < 0:
            raise ValueError("recovery metadata store is closed")


def _recovery_ref(binding: ExternalBootActivationBinding) -> OpaqueProviderRef:
    return OpaqueProviderRef(ref=f"local-recovery-v1/{binding.system_id}/{binding.activation_id}")


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
