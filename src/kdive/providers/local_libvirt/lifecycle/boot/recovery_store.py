"""Local external-boot durable records and descriptor-relative stores (ADR-0586)."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kdive.providers.external_boot_authority.teardown import (
    AuthorityTeardownReservationV1,
)
from kdive.providers.local_libvirt.lifecycle.boot.recovery import (
    _ARCHIVE_NAME,
    ModuleArchiveCapture,
    ModuleCapture,
    RecoveryArchiveSink,
    RecoveryArchiveSource,
)
from kdive.providers.ports.external_boot import (
    ActivationOwnership,
    ComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPreparationObservation,
    ExternalBootPreparationRequest,
    KernelIdentity,
    KernelRelease,
    OpaqueProviderRef,
    ProviderStateIdentity,
    RecoveryPoint,
)
from kdive.providers.shared.directory_listing import list_directory_descriptor
from kdive.providers.shared.libvirt_external_boot import (
    parse_projected_domain_xml,
)

if TYPE_CHECKING:
    from kdive.providers.local_libvirt.lifecycle.boot.session import LocalSystemTeardownInspection


def recovery_point_digest(recovery: RecoveryPoint) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            b"kdive-external-boot-recovery-point-v1\0" + recovery.to_canonical_json()
        ).hexdigest()
    )


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

type SystemTeardownPhase = Literal[
    "intent-recorded",
    "domain-destroyed",
    "domain-undefined",
    "overlay-removed",
    "baseline-removed",
    "complete",
]


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
    expected_running: KernelIdentity
    source_state: ProviderStateIdentity
    target_state: ProviderStateIdentity
    prior_power: Literal["running", "inactive"]
    capture: ModuleCapture
    phase: RecoveryPhase
    # The module tree observed when this activation last published it with the domain inactive.
    # libguestfs cannot open a running domain's disk, so a running domain reports this value.
    inactive_modules: ComponentState | None = None

    @model_validator(mode="after")
    def _domain_xml_matches_digests(self) -> LocalRecoveryMetadataV1:
        if unicodedata.normalize("NFC", self.source_xml) != self.source_xml:
            raise ValueError("source domain XML must be NFC")
        parse_projected_domain_xml(self.target_xml)
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
    expected_running: KernelIdentity
    prior_power: Literal["running", "inactive"]

    @model_validator(mode="after")
    def _domain_xml_matches_digests(self) -> LocalPreStopIntentV1:
        if unicodedata.normalize("NFC", self.source_xml) != self.source_xml:
            raise ValueError("source domain XML must be NFC")
        parse_projected_domain_xml(self.target_xml)
        digest = "sha256:" + hashlib.sha256(self.source_xml.encode()).hexdigest()
        if digest != self.source_xml_sha256:
            raise ValueError("source domain XML digest does not match bytes")
        target_digest = "sha256:" + hashlib.sha256(self.target_xml.encode()).hexdigest()
        if target_digest != self.target_xml_sha256:
            raise ValueError("target domain XML digest does not match bytes")
        if self.expected_running.release != self.release:
            raise ValueError("expected running release does not match recovery release")
        return self


class _LocalSystemTeardownFields(_ClosedValue):
    """Teardown facts shared by the anchor and the intent, which narrow the schema differently."""

    schema_: str = Field(alias="schema")
    authority_id: UUID
    generation: Annotated[int, Field(ge=1)]
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    provider_kind: Annotated[str, Field(min_length=1, max_length=255)]
    authority_instance: Annotated[str, Field(min_length=1, max_length=255)]
    operation_identity: Annotated[str, Field(min_length=1, max_length=255)]
    operation_digest: Digest
    attempt_id: UUID
    journal_sequence: Annotated[int, Field(ge=1)]
    journal_digest: Digest

    @property
    def identity(self) -> str:
        return "sha256:" + hashlib.sha256(_canonical_record_bytes(self)).hexdigest()


class LocalSystemTeardownAnchorV1(_LocalSystemTeardownFields):
    """Path-free authority request and mutation-started anchor used for observation."""

    schema_: Literal["local-libvirt-system-teardown-anchor-v1"] = Field(
        "local-libvirt-system-teardown-anchor-v1", alias="schema"
    )


class LocalSystemTeardownIntentV1(_LocalSystemTeardownFields):
    """Exact authority request and reservation persisted before teardown mutation."""

    schema_: Literal["local-libvirt-system-teardown-intent-v1"] = Field(
        "local-libvirt-system-teardown-intent-v1", alias="schema"
    )
    reservation: AuthorityTeardownReservationV1

    @property
    def identity(self) -> str:
        return "sha256:" + hashlib.sha256(_canonical_record_bytes(self)).hexdigest()

    def matches_anchor(self, anchor: _LocalSystemTeardownFields) -> bool:
        return (
            self.authority_id,
            self.generation,
            self.binding,
            self.plan_identity,
            self.provider_kind,
            self.authority_instance,
            self.operation_identity,
            self.operation_digest,
            self.attempt_id,
            self.journal_sequence,
            self.journal_digest,
        ) == (
            anchor.authority_id,
            anchor.generation,
            anchor.binding,
            anchor.plan_identity,
            anchor.provider_kind,
            anchor.authority_instance,
            anchor.operation_identity,
            anchor.operation_digest,
            anchor.attempt_id,
            anchor.journal_sequence,
            anchor.journal_digest,
        )

    def anchor_subject_matches(self, anchor: _LocalSystemTeardownFields) -> bool:
        """Compare the subject fields an anchor carries; it has no reservation (#2921)."""
        return (
            self.binding,
            self.plan_identity,
            self.provider_kind,
            self.authority_instance,
        ) == (anchor.binding, anchor.plan_identity, anchor.provider_kind, anchor.authority_instance)

    def same_subject(self, other: LocalSystemTeardownIntentV1) -> bool:
        """Allow a fresh authority generation only for the exact same teardown subject."""
        return (
            self.binding,
            self.plan_identity,
            self.provider_kind,
            self.authority_instance,
            self.reservation,
        ) == (
            other.binding,
            other.plan_identity,
            other.provider_kind,
            other.authority_instance,
            other.reservation,
        )


class LocalSystemTeardownRecordV1(_ClosedValue):
    schema_: Literal["local-libvirt-system-teardown-record-v1"] = Field(
        "local-libvirt-system-teardown-record-v1", alias="schema"
    )
    intent: LocalSystemTeardownIntentV1
    phase: SystemTeardownPhase
    domain_validated: bool
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def _completion_timestamp_matches_phase(self) -> LocalSystemTeardownRecordV1:
        if (self.phase == "complete") != (self.completed_at is not None):
            raise ValueError("System teardown completion timestamp must match complete phase")
        if self.completed_at is not None and self.completed_at.utcoffset() != UTC.utcoffset(
            self.completed_at
        ):
            raise ValueError("System teardown completion timestamp must be UTC")
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
    recovery_point: RecoveryPoint
    point_digest: Digest
    payload_absent: Literal[True] = True

    @model_validator(mode="after")
    def _receipt_matches_point(self) -> CleanupTombstoneV1:
        if self.recovery_point.binding != self.binding:
            raise ValueError("cleanup tombstone recovery point has different ownership")
        if recovery_point_digest(self.recovery_point) != self.point_digest:
            raise ValueError("cleanup tombstone recovery point digest does not match")
        return self


class CleanupQuarantineReceiptV1(_ClosedValue):
    """Provider-private exact proof retained only when cleanup finalization is interrupted."""

    schema_: Literal["local-libvirt-cleanup-quarantine-v1"] = Field(
        "local-libvirt-cleanup-quarantine-v1", alias="schema"
    )
    tombstone: CleanupTombstoneV1
    proof: FinalizeCleanupProof
    managed: bool = False

    @model_validator(mode="after")
    def _proof_matches_tombstone(self) -> CleanupQuarantineReceiptV1:
        if (
            self.proof.binding != self.tombstone.binding
            or self.proof.point_digest != self.tombstone.point_digest
        ):
            raise ValueError("cleanup quarantine proof does not match tombstone")
        return self


class TargetProjectionV1(_ClosedValue):
    """Minimal durable inputs needed to render one target domain definition."""

    schema_: Literal["local-libvirt-target-projection-v1"] = Field(
        "local-libvirt-target-projection-v1", alias="schema"
    )
    ownership: ActivationOwnership
    activation_id: Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
    plan_identity: Digest
    architecture: Literal["x86_64", "ppc64le"]
    cmdline: Annotated[str, Field(min_length=1, max_length=4096)]
    kernel_filename: Literal["kernel"] = "kernel"
    modules_filename: Literal["modules"] = "modules"
    initrd_filename: Literal["initrd"] | None
    whole_disk_root_uuid: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    # Omitted when absent so projections written before #3130 keep their digest.
    debuginfo_filename: Literal["debuginfo"] | None = Field(
        None, exclude_if=lambda value: value is None
    )

    def canonical_bytes(self) -> bytes:
        return _canonical_record_bytes(self)

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()


_PROJECTION_NAME = "target-projection.json"

_PROJECTION_TEMPORARY_NAME = ".target-projection.next"

# Every temporary the materialize, verify and projection-publish paths write into a digest
# directory. A killed process leaves them behind; cleanup owns exactly these names (#2920).
_OWNED_TEMPORARY_NAMES = frozenset(
    {
        ".bundle.next",
        ".bundle.verify",
        ".kernel.next",
        # `extract_kernel_bundle` stages `.kernel.next` through `write_staged_bytes`.
        ".kernel.next.part",
        ".modules.next",
        ".initrd.next",
        ".initrd.verify",
        ".debuginfo.next",
        _PROJECTION_TEMPORARY_NAME,
    }
)

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
            activation_fd = _open_or_create_private_child(run_fd, projection.activation_id)
        finally:
            os.close(run_fd)
        try:
            digest_name = projection.digest.removeprefix("sha256:")
            projection_fd = _open_or_create_private_child(activation_fd, digest_name)
            try:
                self.publish_at(projection_fd, projection)
            finally:
                os.close(projection_fd)
            os.fsync(activation_fd)
        finally:
            os.close(activation_fd)
        reopened = self.reopen(
            _projection_ref(projection, "kernel"),
            projection.ownership,
            projection.activation_id,
        )
        if reopened != projection:
            raise ValueError("target projection failed exact reopen")
        return _projection_ref(projection, "kernel")

    @staticmethod
    def publish_at(projection_fd: int, projection: TargetProjectionV1) -> None:
        """Publish canonically through an already authenticated digest descriptor."""
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

    @staticmethod
    def reopen_at(projection_fd: int, expected: TargetProjectionV1) -> TargetProjectionV1:
        """Reopen one projection through its borrowed digest-directory descriptor."""
        data = _read_private_file(projection_fd, _PROJECTION_NAME)
        projection = TargetProjectionV1.model_validate_json(data)
        if projection.canonical_bytes() != data or projection.digest != expected.digest:
            raise ValueError("target projection is not canonical or digest-bound")
        if projection != expected:
            raise ValueError("target projection does not match expected projection")
        return projection

    def reopen(
        self, artifact: OpaqueProviderRef, ownership: ActivationOwnership, activation_id: str
    ) -> TargetProjectionV1:
        parts = _artifact_ref_parts(artifact, ownership, activation_id)
        system_fd = _open_private_directory(self._root_fd, parts[1])
        try:
            run_fd = _open_private_directory(system_fd, parts[2])
        finally:
            os.close(system_fd)
        try:
            activation_fd = _open_private_directory(run_fd, parts[3])
        finally:
            os.close(run_fd)
        try:
            projection_fd = _open_private_directory(activation_fd, parts[4])
        finally:
            os.close(activation_fd)
        try:
            data = _read_private_file(projection_fd, _PROJECTION_NAME)
        finally:
            os.close(projection_fd)
        projection = TargetProjectionV1.model_validate_json(data)
        digest_matches = projection.digest.removeprefix("sha256:") == parts[4]
        if projection.canonical_bytes() != data or not digest_matches:
            raise ValueError("target projection is not canonical or digest-bound")
        if projection.ownership != ownership:
            raise ValueError("target projection owner does not match artifact reference")
        return projection


def _projection_ref(projection: TargetProjectionV1, filename: str) -> OpaqueProviderRef:
    return OpaqueProviderRef(
        ref=(
            f"local-artifact-v2/{projection.ownership.system_id}/"
            f"{projection.ownership.run_id}/{projection.activation_id}/"
            f"{projection.digest.removeprefix('sha256:')}/{filename}"
        )
    )


def _artifact_ref_parts(
    artifact: OpaqueProviderRef, ownership: ActivationOwnership, activation_id: str
) -> list[str]:
    parts = artifact.ref.split("/")
    if (
        len(parts) != 6
        or parts[0] != "local-artifact-v2"
        or parts[1] != ownership.system_id
        or parts[2] != ownership.run_id
        or parts[3] != activation_id
        or len(parts[4]) != 64
        or any(character not in "0123456789abcdef" for character in parts[4])
        or parts[5] not in {"kernel", "modules", "initrd", "debuginfo"}
    ):
        raise ValueError("local artifact reference is malformed or cross-owner")
    return parts


def _open_or_create_private_child(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        # `mkdir` masks its mode argument with the process umask, so a worker started with
        # `UMask=0177` creates 0600 and the directory then fails the exact-0700 check below —
        # permanently, because every later call finds it and takes the `FileExistsError` arm.
        #
        # Only on the creating arm, never on `FileExistsError`. A directory that was already
        # present with the wrong mode is foreign or damaged state, and that is precisely what
        # `_require_private_owned_directory` exists to refuse; setting the mode there would
        # launder bad state into good and read like a fix.
        os.chmod(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    return _open_private_directory(parent_fd, name)


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

_QUARANTINE_NAME = "cleanup-quarantine.json"

_PREPARATION_NAME = "preparation-result.json"

_SYSTEM_TEARDOWN_PREFIX = ".system-teardown-"

_MAX_METADATA_BYTES = 262_144


class LocalPartialAbortReceiptV1(BaseModel):
    """Root-level authority retained while the owned partial directory is removed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_: Literal["local-partial-abort-v1"] = Field("local-partial-abort-v1", alias="schema")
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    authority: OpaqueProviderRef


@dataclass(frozen=True, slots=True)
class AbortablePartial:
    intent: LocalPreStopIntentV1 | None
    materialization: ExternalBootMaterialization | None


def _same_authority_generation(left: OpaqueProviderRef, right: OpaqueProviderRef) -> bool:
    """Whether two phase receipts belong to one authority generation.

    One generation owns materialize, prepare, and activate (ADR-0608), but migration 0135 binds
    each phase receipt to its own operation attempt: ``authority/<id>/<generation>/<attempt>``.
    Receipts from different phases therefore agree on everything except the final segment.
    """
    return left.ref.rsplit("/", 1)[0] == right.ref.rsplit("/", 1)[0]


class LocalPreparationReceiptsV1(BaseModel):
    """Both phase receipts retained in one owner-bound canonical record."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_: Literal["local-preparation-results-v1"] = Field(
        "local-preparation-results-v1", alias="schema"
    )
    materialize: ExternalBootPreparationObservation | None = None
    prepare: ExternalBootPreparationObservation | None = None

    @model_validator(mode="after")
    def _consistent_owner(self) -> LocalPreparationReceiptsV1:
        receipts = [item for item in (self.materialize, self.prepare) if item is not None]
        if not receipts:
            raise ValueError("preparation result must contain a receipt")
        first = receipts[0]
        if any(
            item.binding != first.binding
            or item.plan_identity != first.plan_identity
            or not _same_authority_generation(item.authority, first.authority)
            for item in receipts[1:]
        ):
            raise ValueError("preparation receipts have conflicting ownership")
        if self.materialize is not None and self.materialize.state != "materialized":
            raise ValueError("materialize preparation receipt has invalid state")
        if self.prepare is not None and self.prepare.state != "prepared":
            raise ValueError("prepare preparation receipt has invalid state")
        if (
            self.materialize is not None
            and self.prepare is not None
            and self.materialize.materialization != self.prepare.materialization
        ):
            raise ValueError("prepare receipt changed materialization")
        return self


def _canonical_record_bytes(record: BaseModel) -> bytes:
    return json.dumps(
        record.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _system_teardown_name(binding: ExternalBootActivationBinding) -> str:
    return f"{_SYSTEM_TEARDOWN_PREFIX}{binding.system_id}.json"


class RecoveryIntentAbsentError(FileNotFoundError):
    """The recovery directory exists but holds no ``intent.json``.

    Cleanup deletes the intent by design, so a caller holding cleanup evidence may treat
    this as an expected observation. Other absent files in the directory stay plain
    ``FileNotFoundError``.
    """


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


def _unlink_owned_temporaries(directory_fd: int) -> None:
    """Unlink the interrupted materialize/verify temporaries a digest directory may hold (#3011).

    Only `_OWNED_TEMPORARY_NAMES` that are private regular files go; anything else is left so the
    caller's `rmdir` stops and the entry is quarantined (ADR-0710).
    """
    removed = False
    for name in sorted(_OWNED_TEMPORARY_NAMES):
        try:
            # lstat, not open: a FIFO or symlink under an owned name must not block or be followed.
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(status.st_mode) or status.st_mode & 0o077:
            raise ValueError(f"owned temporary {name!r} is not a private regular file")
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=directory_fd)
            removed = True
    if removed:
        os.fsync(directory_fd)


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

    def begin_system_teardown(
        self,
        intent: LocalSystemTeardownIntentV1,
        inspection: LocalSystemTeardownInspection,
    ) -> LocalSystemTeardownRecordV1:
        """Persist/reopen exact teardown intent before the first destructive host call."""
        existing = self.read_system_teardown(intent.binding)
        if existing is None:
            if not inspection.domain_validated and not inspection.overlay_absent:
                raise ValueError("unvalidated System overlay is retained for quarantine")
            record = LocalSystemTeardownRecordV1(
                intent=intent,
                phase="intent-recorded",
                domain_validated=inspection.domain_validated,
            )
        else:
            exact_replay = existing.intent == intent
            authenticated_successor = (
                existing.intent.same_subject(intent)
                and intent.generation > existing.intent.generation
            )
            if not exact_replay and not authenticated_successor:
                raise ValueError("System teardown intent conflicts with retained request")
            record = existing.model_copy(update={"intent": intent})
        record = LocalSystemTeardownRecordV1.model_validate(
            record.model_dump(mode="json", by_alias=True)
        )
        _replace_private_file(
            self._root_fd,
            f"{_system_teardown_name(intent.binding)}.next",
            _system_teardown_name(intent.binding),
            _canonical_record_bytes(record),
        )
        os.fsync(self._root_fd)
        reopened = self.read_system_teardown(intent.binding)
        if reopened != record:
            raise ValueError("System teardown intent failed exact reopen")
        return record

    def read_system_teardown(
        self, binding: ExternalBootActivationBinding
    ) -> LocalSystemTeardownRecordV1 | None:
        """Read an exact retained teardown record without creating provider state."""
        try:
            data = _read_private_file(self._root_fd, _system_teardown_name(binding))
        except FileNotFoundError:
            return None
        record = LocalSystemTeardownRecordV1.model_validate_json(data)
        if record.intent.binding != binding or _canonical_record_bytes(record) != data:
            raise ValueError("System teardown record is not canonical or owner-bound")
        return record

    def record_system_teardown_phase(
        self,
        expected: LocalSystemTeardownRecordV1,
        phase: SystemTeardownPhase,
        *,
        completed_at: datetime | None = None,
    ) -> LocalSystemTeardownRecordV1:
        """Advance one exact provider-private teardown checkpoint."""
        current = self.read_system_teardown(expected.intent.binding)
        if current != expected:
            raise ValueError("System teardown record changed before phase publication")
        updated = LocalSystemTeardownRecordV1(
            intent=expected.intent,
            phase=phase,
            domain_validated=expected.domain_validated,
            completed_at=completed_at,
        )
        _replace_private_file(
            self._root_fd,
            f"{_system_teardown_name(expected.intent.binding)}.next",
            _system_teardown_name(expected.intent.binding),
            _canonical_record_bytes(updated),
        )
        os.fsync(self._root_fd)
        return updated

    def observe_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        """Read one exact phase receipt or return the request-bound absent value."""
        self._require_open()
        receipts = self._try_preparation(request.binding)
        if receipts is None:
            return ExternalBootPreparationObservation(
                state="absent",
                binding=request.binding,
                plan_identity=request.plan.identity,
                authority=request.authority,
                operation_identity=request.operation_identity,
            )
        receipt = getattr(receipts, request.phase)
        if receipt is None:
            return ExternalBootPreparationObservation(
                state="absent",
                binding=request.binding,
                plan_identity=request.plan.identity,
                authority=request.authority,
                operation_identity=request.operation_identity,
            )
        if (
            receipt.binding != request.binding
            or receipt.plan_identity != request.plan.identity
            or receipt.authority != request.authority
            or receipt.operation_identity != request.operation_identity
        ):
            raise ValueError("preparation receipt identity conflicts with request")
        return receipt

    def preparation_materialization(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootMaterialization:
        """Return the owner-bound materialization needed by the prepare phase."""
        receipts = self._try_preparation(request.binding)
        receipt = receipts.materialize if receipts is not None else None
        if (
            receipt is None
            or receipt.materialization is None
            or receipt.binding != request.binding
            or receipt.plan_identity != request.plan.identity
            or not _same_authority_generation(receipt.authority, request.authority)
        ):
            raise ValueError("prepare requires a matching durable materialization receipt")
        return receipt.materialization

    def publish_preparation(
        self, receipt: ExternalBootPreparationObservation
    ) -> ExternalBootPreparationObservation:
        """Atomically publish one phase into the activation's preparation record."""
        self._require_open()
        phase = "materialize" if receipt.state == "materialized" else "prepare"
        if receipt.state not in {"materialized", "prepared"}:
            raise ValueError("only completed preparation receipts may be published")
        existing = self._try_preparation(receipt.binding)
        if existing is not None and getattr(existing, phase) is not None:
            if getattr(existing, phase) != receipt:
                raise ValueError("existing preparation receipt conflicts with result")
            if getattr(existing, phase) == receipt:
                return receipt
        updated = (existing or LocalPreparationReceiptsV1(materialize=receipt)).model_copy(
            update={phase: receipt}
        )
        updated = LocalPreparationReceiptsV1.model_validate(updated.model_dump(by_alias=True))
        directory_fd = self._open_preparation_directory(receipt.binding, create=True)
        try:
            _replace_private_file(
                directory_fd,
                ".preparation.next",
                _PREPARATION_NAME,
                _canonical_record_bytes(updated),
            )
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.fsync(self._root_fd)
        reopened = self._try_preparation(receipt.binding)
        if reopened != updated:
            raise ValueError("published preparation receipt failed exact reopen")
        return receipt

    def adopt_preparation(
        self,
        request: ExternalBootPreparationRequest,
        predecessor: ExternalBootPreparationRequest,
        predecessor_receipt_identity: str,
    ) -> ExternalBootPreparationObservation:
        receipt = self.observe_preparation(predecessor)
        if (
            receipt.state == "absent"
            or receipt.identity != predecessor_receipt_identity
            or request.phase != predecessor.phase
            or request.binding != predecessor.binding
            or request.plan.identity != predecessor.plan.identity
        ):
            raise ValueError("preparation predecessor cannot be adopted")
        adopted = receipt.model_copy(
            update={
                "authority": request.authority,
                "operation_identity": request.operation_identity,
            }
        )
        phase = "materialize" if adopted.state == "materialized" else "prepare"
        existing = self._try_preparation(adopted.binding)
        if existing is None or getattr(existing, phase) != receipt:
            raise ValueError("preparation predecessor changed during adoption")
        updated = existing.model_copy(update={phase: adopted})
        directory_fd = self._open_preparation_directory(adopted.binding, create=False)
        try:
            _replace_private_file(
                directory_fd,
                ".preparation.next",
                _PREPARATION_NAME,
                _canonical_record_bytes(updated),
            )
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.fsync(self._root_fd)
        if self.observe_preparation(request) != adopted:
            raise ValueError("adopted preparation receipt failed exact reopen")
        return adopted

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
                _canonical_record_bytes(metadata),
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
                _canonical_record_bytes(intent),
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
                _canonical_record_bytes(metadata),
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
        *,
        inactive_modules: ComponentState | None = None,
    ) -> LocalRecoveryMetadataV1:
        self._require_open()
        name = recovery_directory_name(reference, binding)
        directory_fd = _open_private_directory(self._root_fd, name)
        try:
            if self._read(directory_fd) != expected:
                raise ValueError("recovery metadata changed before phase publication")
            update: dict[str, object] = {"phase": phase}
            if inactive_modules is not None:
                update["inactive_modules"] = inactive_modules
            updated = expected.model_copy(update=update)
            temporary = ".intent.next"
            _replace_private_file(
                directory_fd,
                temporary,
                _INTENT_NAME,
                _canonical_record_bytes(updated),
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
        *,
        require_recovered: bool = True,
    ) -> CleanupTombstoneV1:
        """Atomically replace recovered metadata with accounted cleanup evidence.

        System teardown discards a conflicted activation unrecovered (#3341), so it passes
        ``require_recovered=False``.
        """
        self._require_open()
        name = recovery_directory_name(reference, binding)
        directory_fd = _open_private_directory(self._root_fd, name)
        recovery_point = RecoveryPoint(
            binding=expected.binding,
            plan_identity=expected.plan_identity,
            materialization_identity=expected.materialization_identity,
            recovery_ref=reference,
            source_state=expected.source_state,
            target_state=expected.target_state,
        )
        tombstone = CleanupTombstoneV1(
            binding=binding,
            recovery_point=recovery_point,
            point_digest=point_digest,
        )
        try:
            if expected.binding != binding or self._read(directory_fd) != expected:
                raise ValueError("recovery metadata changed before cleanup")
            if require_recovered and expected.phase != "recovered":
                raise ValueError("recovery must complete before cleanup")
            temporary = ".tombstone.next"
            _replace_private_file(
                directory_fd,
                temporary,
                _TOMBSTONE_NAME,
                _canonical_record_bytes(tombstone),
            )
            os.unlink(_INTENT_NAME, dir_fd=directory_fd)
            with suppress(FileNotFoundError):
                os.unlink(_PREPARATION_NAME, dir_fd=directory_fd)
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
            recovery_point=recovery,
            point_digest=recovery_point_digest(recovery),
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
            recovery_point=recovery,
            point_digest=recovery_point_digest(recovery),
        )
        if proof.binding != recovery.binding or proof.point_digest != expected.point_digest:
            raise ValueError("cleanup finalization proof does not match recovery point")
        try:
            actual = self._read_tombstone_named(name)
        except FileNotFoundError:
            # Absence is success only for the closed exact mutation-started proof
            # re-presented by #2140 for the still-current operation.
            self.prune_empty_activation_parents(recovery.binding)
            return
        if actual != expected:
            raise ValueError("cleanup tombstone does not match recovery point")
        directory_fd = _open_private_directory(self._root_fd, name)
        try:
            # The receipt is re-read under the lock adoption takes, so an adoption that lands
            # after a caller's own read is kept rather than deleted (ADR-0787, #3467).
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            entries = self._checked_tombstone_entries(directory_fd, expected)
            receipt = self._read_cleanup_quarantine_at(directory_fd, recovery.binding)
            adopted = receipt is not None and receipt.managed
            if receipt is not None and adopted and receipt.tombstone != expected:
                raise ValueError("adopted cleanup receipt does not match recovery point")
            _drop_residue(directory_fd, entries)
            if not adopted:
                with suppress(FileNotFoundError):
                    os.unlink(_QUARANTINE_NAME, dir_fd=directory_fd)
                os.unlink(_TOMBSTONE_NAME, dir_fd=directory_fd)
                os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if adopted:
            self.prune_empty_activation_parents(recovery.binding)
            return
        os.rmdir(name, dir_fd=self._root_fd)
        os.fsync(self._root_fd)
        try:
            _open_private_directory(self._root_fd, name)
        except FileNotFoundError:
            # A session opened before ADR-0710 re-created the parents cleanup pruned (#2898).
            self.prune_empty_activation_parents(recovery.binding)
            return
        raise ValueError("cleanup tombstone remained after finalization")

    def drop_tombstone_residue(self, recovery: RecoveryPoint) -> None:
        """Drop residue beside an exact tombstone, keeping it and the receipt; prune parents.

        System teardown keeps an adopted receipt and its tombstone (ADR-0787, #3467), but a
        crash inside ``publish_tombstone`` can leave the intent beside them.
        """
        self._require_open()
        name = recovery_directory_name(recovery.recovery_ref, recovery.binding)
        expected = CleanupTombstoneV1(
            binding=recovery.binding,
            recovery_point=recovery,
            point_digest=recovery_point_digest(recovery),
        )
        directory_fd = _open_private_directory(self._root_fd, name)
        try:
            _drop_residue(directory_fd, self._checked_tombstone_entries(directory_fd, expected))
        finally:
            os.close(directory_fd)
        self.prune_empty_activation_parents(recovery.binding)

    def record_cleanup_quarantine(
        self, recovery: RecoveryPoint, proof: FinalizeCleanupProof
    ) -> None:
        """Persist the exact mutation proof beside an exact cleanup tombstone."""
        self._require_open()
        name = recovery_directory_name(recovery.recovery_ref, recovery.binding)
        tombstone = self._read_tombstone_named(name)
        expected = CleanupTombstoneV1(
            binding=recovery.binding,
            recovery_point=recovery,
            point_digest=recovery_point_digest(recovery),
        )
        if tombstone != expected or proof.point_digest != expected.point_digest:
            raise ValueError("cleanup tombstone does not match recovery point")
        receipt = CleanupQuarantineReceiptV1(tombstone=tombstone, proof=proof)
        directory_fd = _open_private_directory(self._root_fd, name)
        try:
            # Held across the managed check and the replace, as adoption and finalize hold it.
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            # A finalize that completed since the early read removed the tombstone: fail
            # rather than write a receipt into a directory with none (#3517).
            if self._read_tombstone(directory_fd) != expected:
                raise ValueError("cleanup tombstone does not match recovery point")
            standing = self._read_cleanup_quarantine_at(directory_fd, recovery.binding)
            if standing is not None and standing.managed:
                return  # an adopted receipt is kept as is: no path un-adopts it (ADR-0787)
            temporary = ".cleanup-quarantine.next"
            _replace_private_file(
                directory_fd,
                temporary,
                _QUARANTINE_NAME,
                _canonical_record_bytes(receipt),
            )
            os.fsync(directory_fd)
            if self._read_cleanup_quarantine_at(directory_fd, recovery.binding) != receipt:
                raise ValueError("cleanup quarantine receipt failed exact reopen")
        finally:
            os.close(directory_fd)

    def read_cleanup_quarantine(
        self, binding: ExternalBootActivationBinding
    ) -> CleanupQuarantineReceiptV1 | None:
        self._require_open()
        name = recovery_directory_name(_recovery_ref(binding), binding)
        try:
            directory_fd = _open_private_directory(self._root_fd, name)
        except FileNotFoundError:
            return None
        try:
            return self._read_cleanup_quarantine_at(directory_fd, binding)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _read_cleanup_quarantine_at(
        directory_fd: int, binding: ExternalBootActivationBinding
    ) -> CleanupQuarantineReceiptV1 | None:
        try:
            data = _read_private_file(directory_fd, _QUARANTINE_NAME)
        except FileNotFoundError:
            return None
        receipt = CleanupQuarantineReceiptV1.model_validate_json(data)
        if _canonical_record_bytes(receipt) != data:
            raise ValueError("cleanup quarantine receipt is not canonical JSON")
        if receipt.tombstone.binding != binding:
            raise ValueError("cleanup quarantine receipt ownership changed")
        return receipt

    def adopt_cleanup_quarantine(self, receipt: CleanupQuarantineReceiptV1) -> None:
        self._require_open()
        binding = receipt.tombstone.binding
        changed = ValueError("cleanup quarantine receipt changed before adoption")
        adopted = receipt.model_copy(update={"managed": True})
        try:
            directory_fd = _open_private_directory(
                self._root_fd, recovery_directory_name(_recovery_ref(binding), binding)
            )
        except FileNotFoundError:
            raise changed from None
        try:
            # Held across the compare and the replace; finalize_tombstone takes it too (#3467).
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            if self._read_cleanup_quarantine_at(directory_fd, binding) != receipt:
                raise changed
            _replace_private_file(
                directory_fd,
                ".cleanup-quarantine.next",
                _QUARANTINE_NAME,
                _canonical_record_bytes(adopted),
            )
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _checked_tombstone_entries(
        self, directory_fd: int, expected: CleanupTombstoneV1
    ) -> set[str]:
        """The directory's entries, after proving the tombstone and every residue file exact."""
        entries = set(list_directory_descriptor(directory_fd))
        allowed = {_TOMBSTONE_NAME, _INTENT_NAME, _PREPARATION_NAME, _QUARANTINE_NAME}
        if entries - allowed:
            raise ValueError("cleanup tombstone directory contains unexpected payload")
        if self._read_tombstone(directory_fd) != expected:
            raise ValueError("cleanup tombstone changed before finalization")
        self._validate_tombstone_residue(directory_fd, entries, expected)
        return entries

    def _validate_tombstone_residue(
        self,
        directory_fd: int,
        entries: set[str],
        expected: CleanupTombstoneV1,
    ) -> None:
        point = expected.recovery_point
        if _INTENT_NAME in entries:
            metadata = self._read(directory_fd)
            recovered = RecoveryPoint(
                binding=metadata.binding,
                plan_identity=metadata.plan_identity,
                materialization_identity=metadata.materialization_identity,
                recovery_ref=point.recovery_ref,
                source_state=metadata.source_state,
                target_state=metadata.target_state,
            )
            # System teardown publishes the tombstone from any phase (#3341), so a crash between
            # the publish and the intent unlink leaves the intent at that phase.
            if recovered != point:
                raise ValueError("cleanup intent does not match tombstone recovery point")
        if _PREPARATION_NAME not in entries:
            return
        receipts = self._read_preparation(directory_fd)
        for receipt in (receipts.materialize, receipts.prepare):
            if receipt is None:
                continue
            materialization = receipt.materialization
            if (
                receipt.binding != point.binding
                or receipt.plan_identity != point.plan_identity
                or materialization is None
                or materialization.identity != point.materialization_identity
                or (receipt.recovery_point is not None and receipt.recovery_point != point)
            ):
                raise ValueError("cleanup preparation does not match tombstone recovery point")

    def _open_or_create_partial(self, name: str) -> int:
        try:
            os.mkdir(name, 0o700, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        except FileExistsError:
            pass
        return _open_private_directory(self._root_fd, name)

    def inspect_abortable_partial(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: Digest,
        authority: OpaqueProviderRef,
    ) -> AbortablePartial | Literal["absent", "not-partial"]:
        name = recovery_directory_name(_recovery_ref(binding), binding)
        abort_name = f".{name}.abort.json"
        expected_abort = LocalPartialAbortReceiptV1(
            binding=binding, plan_identity=plan_identity, authority=authority
        )
        try:
            complete_fd = _open_private_directory(self._root_fd, name)
        except FileNotFoundError:
            pass
        else:
            os.close(complete_fd)
            # A complete directory is only ever a renamed, filled partial, and only
            # finalize_tombstone empties one, so an empty one is finalization interrupted
            # before its rmdir (#2927). rmdir itself refuses any remaining entry.
            try:
                os.rmdir(name, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass
            except OSError as error:
                if error.errno == errno.ENOTEMPTY:
                    return "not-partial"
                raise
            else:
                os.fsync(self._root_fd)
        abort_receipt = self._read_optional_abort_receipt(abort_name)
        if abort_receipt is not None and abort_receipt != expected_abort:
            raise ValueError("partial abort receipt does not match teardown request")
        partial_name = f".{name}.partial"
        try:
            directory_fd = _open_private_directory(self._root_fd, partial_name)
        except FileNotFoundError:
            if abort_receipt is None:
                return "absent"
            return AbortablePartial(intent=None, materialization=None)
        try:
            entries = set(list_directory_descriptor(directory_fd))
            allowed = {
                _INTENT_NAME,
                _INITIAL_INTENT_TEMPORARY_NAME,
                _PREPARATION_NAME,
                _ARCHIVE_NAME,
                f".{_ARCHIVE_NAME}.partial",
            }
            if entries - allowed:
                raise ValueError("recovery partial contains unexpected residue")
            intent: LocalPreStopIntentV1 | None = None
            materialization: ExternalBootMaterialization | None = None
            if _INTENT_NAME in entries:
                intent = self._read_pre_stop(directory_fd)
                if intent.binding != binding or intent.plan_identity != plan_identity:
                    raise ValueError("recovery partial does not match teardown request")
            if _PREPARATION_NAME in entries:
                receipts = self._read_preparation(directory_fd)
                for receipt in (receipts.materialize, receipts.prepare):
                    if receipt is not None and (
                        receipt.binding != binding
                        or receipt.plan_identity != plan_identity
                        or receipt.authority != authority
                    ):
                        raise ValueError("recovery partial does not match teardown request")
                receipt = receipts.prepare or receipts.materialize
                materialization = None if receipt is None else receipt.materialization
            if intent is None and materialization is None and abort_receipt is None:
                raise ValueError("recovery partial has no owned record")
            return AbortablePartial(intent=intent, materialization=materialization)
        finally:
            os.close(directory_fd)

    def remove_abortable_partial(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: Digest,
        authority: OpaqueProviderRef,
    ) -> None:
        checked = self.inspect_abortable_partial(binding, plan_identity, authority)
        if isinstance(checked, str):
            return
        name = recovery_directory_name(_recovery_ref(binding), binding)
        partial_name = f".{name}.partial"
        abort_name = f".{name}.abort.json"
        receipt = LocalPartialAbortReceiptV1(
            binding=binding, plan_identity=plan_identity, authority=authority
        )
        data = receipt.model_dump_json(by_alias=True).encode()
        if len(data) > _MAX_METADATA_BYTES:
            raise ValueError("partial abort receipt exceeds its byte bound")
        existing = self._read_optional_abort_receipt(abort_name)
        if existing is None:
            _replace_private_file(self._root_fd, f"{abort_name}.next", abort_name, data)
            os.fsync(self._root_fd)
        elif existing != receipt:
            raise ValueError("partial abort receipt conflicts with teardown request")
        try:
            directory_fd = _open_private_directory(self._root_fd, partial_name)
        except FileNotFoundError:
            directory_fd = None
        if directory_fd is not None:
            try:
                for entry in (
                    f".{_ARCHIVE_NAME}.partial",
                    _ARCHIVE_NAME,
                    _INITIAL_INTENT_TEMPORARY_NAME,
                    _INTENT_NAME,
                    _PREPARATION_NAME,
                ):
                    with suppress(FileNotFoundError):
                        os.unlink(entry, dir_fd=directory_fd)
                        os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        with suppress(FileNotFoundError):
            os.rmdir(partial_name, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        with suppress(FileNotFoundError):
            os.unlink(abort_name, dir_fd=self._root_fd)
            os.fsync(self._root_fd)

    def remove_abortable_activation(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: Digest,
        materialization: ExternalBootMaterialization,
    ) -> None:
        """Remove only artifacts authenticated by the durable preparation receipt."""
        ownership = ActivationOwnership(system_id=binding.system_id, run_id=binding.run_id)
        if materialization.ownership != ownership or materialization.plan_identity != plan_identity:
            raise ValueError("materialization does not match partial abort ownership")
        refs = [materialization.artifacts.kernel, materialization.artifacts.modules]
        if materialization.artifacts.initrd is not None:
            refs.append(materialization.artifacts.initrd)
        if materialization.artifacts.debuginfo is not None:
            refs.append(materialization.artifacts.debuginfo)
        parts = [_artifact_ref_parts(ref, ownership, binding.activation_id) for ref in refs]
        if any(item[1:5] != parts[0][1:5] for item in parts[1:]):
            raise ValueError("materialization artifacts do not share one projection")
        try:
            system_fd = _open_private_directory(self._root_fd, binding.system_id)
        except FileNotFoundError:
            return
        try:
            try:
                run_fd = _open_private_directory(system_fd, binding.run_id)
            except FileNotFoundError:
                return
            try:
                try:
                    activation_fd = _open_private_directory(run_fd, binding.activation_id)
                except FileNotFoundError:
                    return
                try:
                    try:
                        digest_fd = _open_private_directory(activation_fd, parts[0][4])
                    except FileNotFoundError:
                        digest_fd = None
                    if digest_fd is not None:
                        try:
                            try:
                                projection_data = _read_private_file(digest_fd, _PROJECTION_NAME)
                            except FileNotFoundError:
                                pass
                            else:
                                projection = TargetProjectionV1.model_validate_json(projection_data)
                                if (
                                    projection.canonical_bytes() != projection_data
                                    or projection.ownership != ownership
                                    or projection.activation_id != binding.activation_id
                                    or projection.plan_identity != plan_identity
                                    or projection.digest.removeprefix("sha256:") != parts[0][4]
                                ):
                                    raise ValueError(
                                        "target projection does not authenticate abort"
                                    )
                                os.unlink(_PROJECTION_NAME, dir_fd=digest_fd)
                                os.fsync(digest_fd)
                            _unlink_owned_temporaries(digest_fd)
                        finally:
                            os.close(digest_fd)
                    for item in parts:
                        with suppress(FileNotFoundError):
                            os.unlink(item[5], dir_fd=activation_fd)
                            os.fsync(activation_fd)
                    with suppress(FileNotFoundError):
                        os.rmdir(parts[0][4], dir_fd=activation_fd)
                        os.fsync(activation_fd)
                finally:
                    os.close(activation_fd)
            finally:
                os.close(run_fd)
        finally:
            os.close(system_fd)
        # Another activation or run may still share these parents (#2982).
        self.prune_empty_activation_parents(binding)

    def prune_empty_activation_parents(self, binding: ExternalBootActivationBinding) -> None:
        """Remove the activation's `<system>/<run>/<activation>` directories only while empty.

        A session opened before ADR-0710 created them on open, and a materialization
        interrupted between its first artifact use and its digest `mkdir` leaves them; either
        way they own nothing, yet exact recovery absence stays false while they exist (#2898,
        #2926). Anything inside stops the walk: residue stays for quarantine.
        """
        chain = (binding.system_id, binding.run_id, binding.activation_id)
        descriptors = [self._root_fd]
        try:
            for name in chain:
                try:
                    descriptors.append(_open_private_directory(descriptors[-1], name))
                except FileNotFoundError:
                    return
            for parent_fd, name in reversed(tuple(zip(descriptors[:-1], chain, strict=True))):
                try:
                    os.rmdir(name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    if error.errno == errno.ENOTEMPTY:
                        return
                    raise
                os.fsync(parent_fd)
        finally:
            for descriptor in descriptors[1:]:
                os.close(descriptor)

    def _holds_only_adopted_receipt(
        self, directory_fd: int, binding: ExternalBootActivationBinding
    ) -> bool:
        if set(list_directory_descriptor(directory_fd)) != {_TOMBSTONE_NAME, _QUARANTINE_NAME}:
            return False
        receipt = self.read_cleanup_quarantine(binding)
        return (
            receipt is not None
            and receipt.managed
            and self._read_tombstone(directory_fd) == receipt.tombstone
        )

    def _read_optional_abort_receipt(self, name: str) -> LocalPartialAbortReceiptV1 | None:
        try:
            data = _read_private_file(self._root_fd, name)
        except FileNotFoundError:
            return None
        if not data or len(data) > _MAX_METADATA_BYTES:
            raise ValueError("partial abort receipt is empty or oversized")
        receipt = LocalPartialAbortReceiptV1.model_validate_json(data)
        if receipt.model_dump_json(by_alias=True).encode() != data:
            raise ValueError("partial abort receipt is not canonical")
        return receipt

    def exact_recovery_absence(
        self, binding: ExternalBootActivationBinding, *, adopted_receipt_absent: bool = False
    ) -> bool:
        """Read-only proof that every exact activation-owned storage location is absent.

        System teardown passes ``adopted_receipt_absent``: a recovery directory holding only its
        tombstone and an adopted receipt is the operator's, not the activation's (ADR-0787,
        #3467).
        """
        name = recovery_directory_name(_recovery_ref(binding), binding)
        for candidate in (name, f".{name}.partial"):
            try:
                descriptor = _open_private_directory(self._root_fd, candidate)
            except FileNotFoundError:
                continue
            try:
                adopted = (
                    adopted_receipt_absent
                    and candidate == name
                    and self._holds_only_adopted_receipt(descriptor, binding)
                )
            finally:
                os.close(descriptor)
            if not adopted:
                return False
        if self._read_optional_abort_receipt(f".{name}.abort.json") is not None:
            return False
        try:
            system_fd = _open_private_directory(self._root_fd, binding.system_id)
        except FileNotFoundError:
            return True
        try:
            try:
                run_fd = _open_private_directory(system_fd, binding.run_id)
            except FileNotFoundError:
                return True
            try:
                try:
                    activation_fd = _open_private_directory(run_fd, binding.activation_id)
                except FileNotFoundError:
                    return True
                else:
                    os.close(activation_fd)
                    return False
            finally:
                os.close(run_fd)
        finally:
            os.close(system_fd)

    def _open_preparation_directory(
        self, binding: ExternalBootActivationBinding, *, create: bool
    ) -> int:
        name = recovery_directory_name(_recovery_ref(binding), binding)
        try:
            return _open_private_directory(self._root_fd, name)
        except FileNotFoundError:
            partial = f".{name}.partial"
            if create:
                return self._open_or_create_partial(partial)
            return _open_private_directory(self._root_fd, partial)

    def _try_preparation(
        self, binding: ExternalBootActivationBinding
    ) -> LocalPreparationReceiptsV1 | None:
        try:
            directory_fd = self._open_preparation_directory(binding, create=False)
        except FileNotFoundError:
            return None
        try:
            try:
                return self._read_preparation(directory_fd)
            except FileNotFoundError:
                return None
        finally:
            os.close(directory_fd)

    @staticmethod
    def _read_preparation(directory_fd: int) -> LocalPreparationReceiptsV1:
        data = _read_private_file(directory_fd, _PREPARATION_NAME)
        receipts = LocalPreparationReceiptsV1.model_validate_json(data)
        if _canonical_record_bytes(receipts) != data:
            raise ValueError("preparation result is not canonical JSON")
        return receipts

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

    def reopen_tombstone(
        self, reference: OpaqueProviderRef, binding: ExternalBootActivationBinding
    ) -> CleanupTombstoneV1:
        """Read one canonical owner-bound cleanup receipt without requiring intent metadata."""
        self._require_open()
        tombstone = self._read_tombstone_named(recovery_directory_name(reference, binding))
        if tombstone.binding != binding or tombstone.recovery_point.recovery_ref != reference:
            raise ValueError("cleanup tombstone does not match requested recovery object")
        return tombstone

    @staticmethod
    def _read_tombstone(directory_fd: int) -> CleanupTombstoneV1:
        data = _read_private_file(directory_fd, _TOMBSTONE_NAME)
        tombstone = CleanupTombstoneV1.model_validate_json(data)
        if _canonical_record_bytes(tombstone) != data:
            raise ValueError("cleanup tombstone is not canonical JSON")
        return tombstone

    @staticmethod
    def _read(directory_fd: int) -> LocalRecoveryMetadataV1:
        try:
            data = _read_private_file(directory_fd, _INTENT_NAME)
        except FileNotFoundError as error:
            raise RecoveryIntentAbsentError(_INTENT_NAME) from error
        metadata = LocalRecoveryMetadataV1.model_validate_json(data)
        if _canonical_record_bytes(metadata) != data:
            raise ValueError("recovery intent is not canonical JSON")
        return metadata

    @staticmethod
    def _read_pre_stop(directory_fd: int) -> LocalPreStopIntentV1:
        data = _read_private_file(directory_fd, _INTENT_NAME)
        intent = LocalPreStopIntentV1.model_validate_json(data)
        if _canonical_record_bytes(intent) != data:
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


def _drop_residue(directory_fd: int, entries: set[str]) -> None:
    for residual in (_INTENT_NAME, _PREPARATION_NAME):
        if residual in entries:
            os.unlink(residual, dir_fd=directory_fd)
            os.fsync(directory_fd)


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
    entries = set(list_directory_descriptor(directory_fd))
    expected = {_INTENT_NAME, _INITIAL_INTENT_TEMPORARY_NAME, _PREPARATION_NAME}
    if entries - expected or len(entries - {_PREPARATION_NAME}) > 1:
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
