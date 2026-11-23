"""Descriptor-confined remote external-boot durable evidence (ADRs 0608, 0620, 0787)."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.providers.external_boot_authority.module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
)
from kdive.providers.external_boot_authority.module_protocol import (
    MAX_PREPARATION_BYTES,
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
    RemoteModuleVolumePreparationResponseV1,
    authority_reference,
    canonical_model_bytes,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityPreparationMutationRequestV1,
)
from kdive.providers.external_boot_authority.teardown import (
    AuthorityTeardownReservationV1,
    ProviderRecoveryRefusal,
    SystemTeardownSupersededError,
)
from kdive.providers.ports.external_boot import (
    Digest,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPreparationObservation,
    ExternalBootPreparationRequest,
    OpaqueProviderRef,
    ProviderStateIdentity,
    RecoveryObjectBinding,
    RecoveryObjectObservation,
    RecoveryPoint,
)
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    RemoteExternalBootDefinition,
)
from kdive.providers.shared.directory_listing import list_directory_descriptor

logger = logging.getLogger(__name__)


_MAX_RECORD_BYTES = 1_048_576


# Names finalize parses by content and a completed System teardown collects (ADR-0787, #3475).
_FINALIZE_COUNTED_SUFFIXES = (".request", ".lifecycle-request", ".preparation")


_FINALIZE_SCAN_BOUND = 4096


type PriorPower = Literal["running", "inactive"]


type RemoteSystemTeardownPhase = Literal[
    "intent-recorded",
    "domain-validated",
    "domain-destroyed",
    "recovery-removed",
    "domain-undefined",
    "artifacts-removed",
    "complete",
]


class _RemoteSystemTeardownFields(BaseModel):
    """Teardown facts shared by the anchor and the intent, which narrow the schema differently."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True, strict=True)

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
        return "sha256:" + hashlib.sha256(canonical_model_bytes(self)).hexdigest()


class RemoteSystemTeardownAnchorV1(_RemoteSystemTeardownFields):
    """Path-free authority request and mutation-started anchor for remote teardown."""

    schema_: Literal["remote-libvirt-system-teardown-anchor-v1"] = Field(
        "remote-libvirt-system-teardown-anchor-v1", alias="schema"
    )


class RemoteSystemTeardownIntentV1(_RemoteSystemTeardownFields):
    """Exact request and accounting owner persisted before the first host mutation."""

    schema_: Literal["remote-libvirt-system-teardown-intent-v1"] = Field(
        "remote-libvirt-system-teardown-intent-v1", alias="schema"
    )
    reservation: AuthorityTeardownReservationV1

    def matches_anchor(self, anchor: _RemoteSystemTeardownFields) -> bool:
        left = self.model_dump(exclude={"schema_", "reservation"})
        right = anchor.model_dump(exclude={"schema_"})
        return left == right

    def anchor_subject_matches(self, anchor: _RemoteSystemTeardownFields) -> bool:
        """Compare the subject fields an anchor carries; it has no reservation (#2921)."""
        return (
            self.binding,
            self.plan_identity,
            self.provider_kind,
            self.authority_instance,
        ) == (anchor.binding, anchor.plan_identity, anchor.provider_kind, anchor.authority_instance)

    def same_subject(self, other: RemoteSystemTeardownIntentV1) -> bool:
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


class RemoteSystemTeardownRecordV1(BaseModel):
    """Durable remote teardown checkpoint retained across process loss."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True, strict=True)

    schema_: Literal["remote-libvirt-system-teardown-record-v1"] = Field(
        "remote-libvirt-system-teardown-record-v1", alias="schema"
    )
    intent: RemoteSystemTeardownIntentV1
    phase: RemoteSystemTeardownPhase
    domain_validated: bool
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def _completion_matches_phase(self) -> Self:
        if (self.phase == "complete") != (self.completed_at is not None):
            raise ValueError("remote System teardown completion timestamp differs from phase")
        if self.completed_at is not None and self.completed_at.utcoffset() != UTC.utcoffset(
            self.completed_at
        ):
            raise ValueError("remote System teardown completion timestamp must be UTC")
        return self

    def to_canonical_json(self) -> bytes:
        return canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > MAX_PREPARATION_BYTES:
            raise ProviderRecoveryRefusal("remote System teardown record exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal("remote System teardown record is not canonical JSON")
        return value


type RemoteModulePreparationResponse = (
    RemoteModuleVolumePreparationResponseV1 | RemoteModuleTerminalPreparationResponseV1
)


class RemoteModulePreparationCompletionV1(BaseModel):
    """Durable completion published only after the provider call has stopped."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_: Literal["remote-module-preparation-completion-v1"] = Field(
        "remote-module-preparation-completion-v1", alias="schema"
    )
    state: Literal["terminal", "failed-after-mutation"]
    response: RemoteModuleTerminalPreparationResponseV1 | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.response is not None) != (self.state == "terminal"):
            raise ValueError("remote preparation completion state differs from response")
        return self

    def to_canonical_json(self) -> bytes:
        return canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal("remote preparation completion is not canonical JSON")
        return value


class _PersistedRemoteModuleLifecycleV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    request: RemoteModuleLifecycleRequestV1
    local_deadline: Annotated[float, Field(gt=0)]

    def to_canonical_json(self) -> bytes:
        return canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal(
                "persisted remote module lifecycle request is not canonical JSON"
            )
        return value


class RemoteModuleLifecycleCompletionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: Literal["terminal", "failed-after-mutation"]
    response: RemoteModuleLifecycleResponseV1 | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.response is not None) != (self.state == "terminal"):
            raise ValueError("remote module lifecycle completion differs from response")
        return self

    def to_canonical_json(self) -> bytes:
        return canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal(
                "remote module lifecycle completion is not canonical JSON"
            )
        return value


_FAILED_LIFECYCLE_COMPLETION = RemoteModuleLifecycleCompletionV1(
    state="failed-after-mutation"
).to_canonical_json()


class RemoteModuleTerminalRecord(BaseModel):
    """Exact authenticated request and terminal provider-host response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: RemoteModuleVolumePreparationRequestV1
    response: RemoteModuleTerminalPreparationResponseV1

    @model_validator(mode="after")
    def _terminal_matches_request(self) -> Self:
        self.response.validate_terminal_for(self.request.operation, self.request.authority)
        return self

    def to_canonical_json(self) -> bytes:
        encoded = canonical_model_bytes(self)
        if len(encoded) > MAX_PREPARATION_BYTES:
            raise ProviderRecoveryRefusal("remote module terminal record exceeds 1048576 bytes")
        return encoded

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > MAX_PREPARATION_BYTES:
            raise ProviderRecoveryRefusal("remote module terminal record exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal("remote module terminal record is not canonical JSON")
        return value


class _PersistedRemoteModulePreparationV1(BaseModel):
    """Authority-private request and one non-renewable local monotonic deadline."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    request: RemoteModuleVolumePreparationRequestV1
    local_deadline: Annotated[float, Field(gt=0)]

    def to_canonical_json(self) -> bytes:
        return canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal(
                "persisted remote module preparation is not canonical JSON"
            )
        return value


@dataclass(frozen=True, slots=True)
class AdmittedRemoteModulePreparation:
    """Process-local view of the persisted request and authority-clock deadline."""

    request: RemoteModuleVolumePreparationRequestV1
    local_deadline: float


@dataclass(frozen=True, slots=True)
class AdmittedRemoteModuleLifecycle:
    request: RemoteModuleLifecycleRequestV1
    local_deadline: float


class RemoteCleanupFinalizedError(FileNotFoundError):
    """A per-activation record is gone because its cleanup was finalized, not never written."""


class RemoteCleanupQuarantineV1(BaseModel):
    """Provider-private proof that release cleanup ran, kept until authority finalization.

    Bound to the anchored mutation-started journal record, so finalization and orphan
    disposition act only on the cleanup that record authorized.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["remote-libvirt-cleanup-quarantine-v1"] = Field(
        "remote-libvirt-cleanup-quarantine-v1", alias="schema"
    )
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    recovery_ref: OpaqueProviderRef
    operation_identity: Annotated[str, Field(min_length=1, max_length=255)]
    attempt_id: UUID
    journal_sequence: Annotated[int, Field(ge=1)]
    journal_digest: Digest
    managed: bool = False

    def to_canonical_json(self) -> bytes:
        return canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_RECORD_BYTES:
            raise ProviderRecoveryRefusal("remote cleanup quarantine receipt exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal("remote cleanup quarantine receipt is not canonical JSON")
        return value


class RemoteFinalizedBootArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: Annotated[str, Field(min_length=1, max_length=1024)]
    kind: Literal["kernel", "initrd"]
    sha256: Annotated[str, Field(min_length=1, max_length=128)] | None


class RemoteFinalizedTeardownRefsV1(BaseModel):
    """What System teardown must still prove absent once finalize removed the records naming it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    module_volumes: tuple[tuple[str, str], ...]
    operation_nonce: Annotated[str, Field(min_length=1, max_length=1024)] | None
    boot_artifacts: tuple[RemoteFinalizedBootArtifactV1, ...]


class RemoteCleanupFinalizedMarkerV1(BaseModel):
    """The ``cleanup-finalized`` marker: the cleaned receipt plus the refs teardown still needs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_: Literal["remote-libvirt-cleanup-finalized-v1"] = Field(
        "remote-libvirt-cleanup-finalized-v1", alias="schema"
    )
    receipt: RemoteCleanupQuarantineV1
    teardown: RemoteFinalizedTeardownRefsV1

    def to_canonical_json(self) -> bytes:
        return canonical_model_bytes(self)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_RECORD_BYTES:
            raise ProviderRecoveryRefusal("remote cleanup finalized marker exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal("remote cleanup finalized marker is not canonical JSON")
        return value


def _parse_finalized_marker(
    data: bytes,
) -> tuple[RemoteCleanupQuarantineV1, RemoteFinalizedTeardownRefsV1 | None]:
    """Receipt and teardown refs; a marker from before the refs existed has none."""
    try:
        marker = RemoteCleanupFinalizedMarkerV1.from_canonical_json(data)
    except ValueError:
        try:
            return RemoteCleanupQuarantineV1.from_canonical_json(data), None
        except ValueError:
            raise ProviderRecoveryRefusal("remote cleanup finalized marker is malformed") from None
    return marker.receipt, marker.teardown


def quarantine_observation(receipt: RemoteCleanupQuarantineV1) -> RecoveryObjectObservation:
    reference = receipt.recovery_ref
    binding = RecoveryObjectBinding(
        record_id=str(
            uuid5(
                NAMESPACE_URL,
                f"kdive/remote-recovery/{reference.ref}/{receipt.journal_digest}",
            )
        ),
        binding=receipt.binding,
        kind="recovery-record",
        reference=reference,
        ownership_digest="sha256:" + _recovery_identity(reference),
        operation_identity=receipt.operation_identity,
        attempt_id=str(receipt.attempt_id),
        mutation_journal_sequence=receipt.journal_sequence,
        mutation_journal_digest=receipt.journal_digest,
        reserved_bytes=0,
    )
    digest = (
        "sha256:"
        + hashlib.sha256(
            b"kdive-remote-cleanup-quarantine-observation-v1\0" + receipt.to_canonical_json()
        ).hexdigest()
    )
    return RecoveryObjectObservation(
        binding=binding, present=True, managed=receipt.managed, observed_digest=digest
    )


def _recovery_identity(reference: OpaqueProviderRef) -> str:
    prefix = "remote-external-boot/"
    if not reference.ref.startswith(prefix):
        raise ProviderRecoveryRefusal("remote recovery reference has the wrong namespace")
    identity = reference.ref.removeprefix(prefix)
    if len(identity) != 64 or any(value not in "0123456789abcdef" for value in identity):
        raise ProviderRecoveryRefusal("remote recovery reference is malformed")
    return identity


def _authority_binding(
    authority: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
) -> ExternalBootActivationBinding:
    return ExternalBootActivationBinding(
        system_id=str(authority.system_id),
        run_id=str(authority.run_id),
        activation_id=str(authority.activation_id),
    )


def _authority_owned(
    authority: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
    binding: ExternalBootActivationBinding,
    plan_identity: str,
) -> bool:
    return (
        str(authority.system_id),
        str(authority.activation_id),
        str(authority.run_id),
        authority.plan_identity,
    ) == (binding.system_id, binding.activation_id, binding.run_id, plan_identity)


def _object_index_name(reference: OpaqueProviderRef) -> str:
    key = hashlib.sha256(b"kdive-remote-recovery-object-v1\0" + reference.to_canonical_json())
    return f"{key.hexdigest()}.object-index"


def _boot_artifacts(
    materialization: ExternalBootMaterialization,
) -> list[RemoteFinalizedBootArtifactV1]:
    artifacts = materialization.artifacts
    boot = [
        RemoteFinalizedBootArtifactV1(
            ref=artifacts.kernel.ref,
            kind="kernel",
            sha256=materialization.extracted_vmlinuz_sha256,
        )
    ]
    if artifacts.initrd is not None:
        boot.append(
            RemoteFinalizedBootArtifactV1(
                ref=artifacts.initrd.ref,
                kind="initrd",
                sha256=materialization.verified_initrd_sha256,
            )
        )
    return boot


def _claims_within(
    current: RemoteFinalizedTeardownRefsV1 | None, proven: RemoteFinalizedTeardownRefsV1 | None
) -> bool:
    """Whether an activation's records name nothing the proof did not (#3486).

    ``None`` is a finalized activation. Records only shrink while collection runs, so a subset of
    what was proven is still proven.
    """
    if current is None:
        return True
    if proven is None:
        return False
    return (
        set(current.module_volumes) <= set(proven.module_volumes)
        and set(current.boot_artifacts) <= set(proven.boot_artifacts)
        and current.operation_nonce in (None, proven.operation_nonce)
    )


class RemoteExternalBootStore:
    """Descriptor-confined durable request/result evidence for provider-host restart."""

    def __init__(self, root: Path) -> None:
        self._root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        status = os.fstat(self._root_fd)
        if not stat.S_ISDIR(status.st_mode) or stat.S_IMODE(status.st_mode) & 0o022:
            os.close(self._root_fd)
            raise PermissionError("remote preparation store must not be group/world writable")
        self._owner_uid = status.st_uid

    def close(self) -> None:
        descriptor, self._root_fd = self._root_fd, -1
        if descriptor >= 0:
            os.close(descriptor)

    @contextmanager
    def _exclusive(self) -> Generator[None]:
        """Hold an exclusive lock on the store directory (ADR-0787, #3472).

        Each acquisition opens its own file description, so threads of one process exclude
        each other too; closing it releases the lock.
        """
        descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=self._root_fd)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _key(request: RemoteModuleVolumePreparationRequestV1) -> str:
        return RemoteExternalBootStore._authority_key(request.authority)

    @staticmethod
    def _authority_key(authority: AuthorityPreparationMutationRequestV1) -> str:
        bound = "\0".join(
            (
                str(authority.system_id),
                str(authority.activation_id),
                str(authority.run_id),
                str(authority.generation),
                authority.operation_identity,
                str(authority.attempt_id),
            )
        ).encode()
        return hashlib.sha256(b"kdive-remote-preparation-v1\0" + bound).hexdigest()

    def _read(self, name: str) -> bytes | None:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            return None
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or stat.S_IMODE(status.st_mode) != 0o600
                or status.st_uid != self._owner_uid
            ):
                raise PermissionError("remote preparation evidence mode is unsafe")
            chunks: list[bytes] = []
            remaining = MAX_PREPARATION_BYTES + 1
            while remaining and (chunk := os.read(descriptor, min(65_536, remaining))):
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > MAX_PREPARATION_BYTES:
                raise ProviderRecoveryRefusal("remote preparation evidence exceeds 1048576 bytes")
            return data
        finally:
            os.close(descriptor)

    def _publish(self, name: str, data: bytes) -> None:
        existing = self._read(name)
        if existing is not None:
            if existing != data:
                raise ProviderRecoveryRefusal(
                    "remote preparation evidence conflicts with durable bytes"
                )
            return
        temporary = f".{name}.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._root_fd,
            )
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("remote preparation evidence write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=self._root_fd,
                    dst_dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if self._read(name) != data:
                    raise ProviderRecoveryRefusal(
                        "remote preparation evidence conflicts with durable bytes"
                    ) from None
            os.unlink(temporary, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self._root_fd)

    def _replace(self, name: str, expected: bytes, data: bytes) -> None:
        if self._read(name) != expected:
            raise ProviderRecoveryRefusal("remote teardown evidence changed before checkpoint")
        temporary = f".{name}.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._root_fd,
            )
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("remote teardown evidence write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, name, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=self._root_fd)

    @staticmethod
    def _system_teardown_key(system_id: str) -> str:
        bound = system_id.encode()
        return hashlib.sha256(b"kdive-remote-system-teardown-v1\0" + bound).hexdigest()

    def begin_system_teardown(
        self, intent: RemoteSystemTeardownIntentV1
    ) -> RemoteSystemTeardownRecordV1:
        name = f"{self._system_teardown_key(intent.binding.system_id)}.teardown"
        data = self._read(name)
        if data is None:
            record = RemoteSystemTeardownRecordV1(
                intent=intent, phase="intent-recorded", domain_validated=False
            )
            self._publish(name, record.to_canonical_json())
            return record
        record = RemoteSystemTeardownRecordV1.from_canonical_json(data)
        if record.intent == intent:
            return record
        if not record.intent.same_subject(intent) or intent.generation <= record.intent.generation:
            raise ProviderRecoveryRefusal("remote System teardown conflicts with retained intent")
        adopted = record.model_copy(update={"intent": intent})
        self._replace(name, data, adopted.to_canonical_json())
        return adopted

    def reopen_system_teardown(
        self, anchor: _RemoteSystemTeardownFields
    ) -> RemoteSystemTeardownRecordV1 | None:
        """Reopen the record this anchor owns; ``None`` when it never reached ``begin``.

        ADR-0620 (#2921): an earlier generation's record of the same subject is not this
        anchor's, so it reopens as absent; a later generation's means this one is superseded.
        """
        name = f"{self._system_teardown_key(anchor.binding.system_id)}.teardown"
        data = self._read(name)
        if data is None:
            return None
        record = RemoteSystemTeardownRecordV1.from_canonical_json(data)
        if record.intent.matches_anchor(anchor):
            return record
        if (
            not record.intent.anchor_subject_matches(anchor)
            or record.intent.generation == anchor.generation
        ):
            raise ProviderRecoveryRefusal(
                "remote System teardown observation conflicts with retained intent"
            )
        if record.intent.generation > anchor.generation:
            raise SystemTeardownSupersededError
        return None

    def checkpoint_system_teardown(
        self,
        record: RemoteSystemTeardownRecordV1,
        phase: RemoteSystemTeardownPhase,
        *,
        completed_at: datetime | None = None,
    ) -> RemoteSystemTeardownRecordV1:
        phases: tuple[RemoteSystemTeardownPhase, ...] = (
            "intent-recorded",
            "domain-validated",
            "domain-destroyed",
            "recovery-removed",
            "domain-undefined",
            "artifacts-removed",
            "complete",
        )
        current = phases.index(record.phase)
        requested = phases.index(phase)
        if requested < current:
            return record
        if requested > current + 1:
            raise ProviderRecoveryRefusal("remote System teardown phase skipped a checkpoint")
        if requested == current:
            return record
        updated = record.model_copy(
            update={
                "phase": phase,
                "domain_validated": record.domain_validated or phase == "domain-validated",
                "completed_at": completed_at,
            }
        )
        updated = RemoteSystemTeardownRecordV1.from_canonical_json(updated.to_canonical_json())
        key = self._system_teardown_key(record.intent.binding.system_id)
        name = f"{key}.teardown"
        self._replace(name, record.to_canonical_json(), updated.to_canonical_json())
        return updated

    def stage(self, request: RemoteModuleVolumePreparationRequestV1, local_deadline: float) -> None:
        self.refuse_finalized(
            _authority_binding(request.authority), request.authority.plan_identity
        )
        name = f"{self._key(request)}.request"
        candidate = _PersistedRemoteModulePreparationV1(
            request=request, local_deadline=local_deadline
        )
        if data := self._read(name):
            persisted = _PersistedRemoteModulePreparationV1.from_canonical_json(data)
            if persisted.request != request:
                raise ProviderRecoveryRefusal(
                    "remote preparation evidence conflicts with durable bytes"
                )
            return
        self._publish(name, candidate.to_canonical_json())

    def reopen_request(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> AdmittedRemoteModulePreparation:
        data = self._read(f"{self._key(request)}.request")
        if data is None:
            raise FileNotFoundError("remote preparation request is absent")
        reopened = _PersistedRemoteModulePreparationV1.from_canonical_json(data)
        if reopened.request != request:
            raise ProviderRecoveryRefusal(
                "remote preparation evidence conflicts with durable bytes"
            )
        return AdmittedRemoteModulePreparation(reopened.request, reopened.local_deadline)

    def publish_result(
        self,
        request: RemoteModuleVolumePreparationRequestV1,
        response: RemoteModulePreparationResponse,
    ) -> None:
        self.reopen_request(request)
        self._publish(f"{self._key(request)}.result", response.to_canonical_json())
        if isinstance(response, RemoteModuleTerminalPreparationResponseV1):
            terminal = RemoteModuleTerminalRecord(request=request, response=response)
            authority = request.authority
            key = self._terminal_key(
                authority.system_id,
                authority.activation_id,
                authority.run_id,
                authority.plan_identity,
            )
            self._publish(
                f"{key}.terminal",
                terminal.to_canonical_json(),
            )
            self._publish(
                f"{self._key(request)}.completion",
                RemoteModulePreparationCompletionV1(
                    state="terminal", response=response
                ).to_canonical_json(),
            )

    def publish_failed_completion(self, request: RemoteModuleVolumePreparationRequestV1) -> None:
        self.reopen_request(request)
        self._publish(
            f"{self._key(request)}.completion",
            RemoteModulePreparationCompletionV1(state="failed-after-mutation").to_canonical_json(),
        )

    def reopen_completion(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModulePreparationCompletionV1 | None:
        self.reopen_request(request)
        data = self._read(f"{self._key(request)}.completion")
        return (
            None if data is None else RemoteModulePreparationCompletionV1.from_canonical_json(data)
        )

    def reopen_result(
        self, request: RemoteModuleVolumePreparationRequestV1
    ) -> RemoteModulePreparationResponse | None:
        self.reopen_request(request)
        data = self._read(f"{self._key(request)}.result")
        if data is None:
            return None
        raw = json.loads(data)
        model = (
            RemoteModuleTerminalPreparationResponseV1
            if isinstance(raw, dict)
            and raw.get("schema") == "remote-module-terminal-preparation-response-v1"
            else RemoteModuleVolumePreparationResponseV1
        )
        return model.from_canonical_json(data)

    @staticmethod
    def _lifecycle_key(request: RemoteModuleLifecycleRequestV1) -> str:
        identity = json.dumps(
            request.model_dump(
                mode="json",
                by_alias=True,
                exclude={"budget_seconds"},
            ),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(b"kdive-remote-module-lifecycle-v1\0" + identity).hexdigest()

    @staticmethod
    def _same_lifecycle(
        left: RemoteModuleLifecycleRequestV1,
        right: RemoteModuleLifecycleRequestV1,
    ) -> bool:
        return left.authority == right.authority and left.action == right.action

    def stage_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1, local_deadline: float
    ) -> None:
        self.refuse_finalized(
            _authority_binding(request.authority), request.authority.plan_identity
        )
        name = f"{self._lifecycle_key(request)}.lifecycle-request"
        candidate = _PersistedRemoteModuleLifecycleV1(
            request=request, local_deadline=local_deadline
        )
        if data := self._read(name):
            persisted = _PersistedRemoteModuleLifecycleV1.from_canonical_json(data)
            if not self._same_lifecycle(persisted.request, request):
                raise ProviderRecoveryRefusal(
                    "remote lifecycle evidence conflicts with durable bytes"
                )
            return
        self._publish(name, candidate.to_canonical_json())

    def reopen_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1
    ) -> AdmittedRemoteModuleLifecycle:
        data = self._read(f"{self._lifecycle_key(request)}.lifecycle-request")
        if data is None:
            raise FileNotFoundError("remote module lifecycle request is absent")
        persisted = _PersistedRemoteModuleLifecycleV1.from_canonical_json(data)
        if not self._same_lifecycle(persisted.request, request):
            raise ProviderRecoveryRefusal("remote lifecycle evidence conflicts with durable bytes")
        return AdmittedRemoteModuleLifecycle(persisted.request, persisted.local_deadline)

    def publish_lifecycle_completion(
        self,
        request: RemoteModuleLifecycleRequestV1,
        response: RemoteModuleLifecycleResponseV1 | None,
    ) -> None:
        self.reopen_lifecycle(request)
        # The restored record goes first: a crash before the completion leaves evidence a restore
        # retry publishes the completion from, never a completion that hides absent evidence.
        if response is not None and response.action == "restore":
            authority = request.authority
            restored_key = self._terminal_key(
                authority.system_id,
                authority.activation_id,
                authority.run_id,
                authority.plan_identity,
            )
            self._publish(f"{restored_key}.restored", response.to_canonical_json())
        name = f"{self._lifecycle_key(request)}.lifecycle-completion"
        data = RemoteModuleLifecycleCompletionV1(
            state="terminal" if response is not None else "failed-after-mutation",
            response=response,
        ).to_canonical_json()
        # A re-driven restore that succeeded supersedes the failure it was re-driven from (#3469).
        if (
            response is not None
            and response.action == "restore"
            and self._read(name) == _FAILED_LIFECYCLE_COMPLETION
        ):
            self._replace(name, _FAILED_LIFECYCLE_COMPLETION, data)
        else:
            self._publish(name, data)

    def reopen_lifecycle_completion(
        self, request: RemoteModuleLifecycleRequestV1
    ) -> RemoteModuleLifecycleCompletionV1 | None:
        self.reopen_lifecycle(request)
        data = self._read(f"{self._lifecycle_key(request)}.lifecycle-completion")
        return None if data is None else RemoteModuleLifecycleCompletionV1.from_canonical_json(data)

    def reopen_restored(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteModuleLifecycleResponseV1 | None:
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        data = self._read(f"{key}.restored")
        if data is None:
            self.refuse_finalized(binding, plan_identity)
            return None
        response = RemoteModuleLifecycleResponseV1.from_canonical_json(data)
        if response.action != "restore":
            raise ProviderRecoveryRefusal("remote module restored evidence has the wrong action")
        return response

    def record_prior_power(
        self, authority: AuthorityPreparationMutationRequestV1, observed: PriorPower
    ) -> PriorPower:
        """Durably fix the System's power from before the first stop; the first record wins.

        A retry after the stop landed observes ``inactive``, so it must read this record
        rather than the domain. The key is the activation, so a later activation records anew.
        """
        key = self._terminal_key(
            authority.system_id, authority.activation_id, authority.run_id, authority.plan_identity
        )
        name = f"{key}.prior-power"
        if (existing := self._read(name)) is None:
            # An owner with no power file is the residue of a crash or a failed clear; it must
            # not make a later request's differing owner bytes a conflict.
            with suppress(FileNotFoundError):
                os.unlink(f"{key}.prior-power-owner", dir_fd=self._root_fd)
            self._publish(
                f"{key}.prior-power-owner", self._authority_key(authority).encode("ascii")
            )
            self._publish(name, observed.encode("ascii"))
            existing = self._read(name)
        return self._decode_prior_power(existing)

    def prior_power_started_by(self, authority: AuthorityPreparationMutationRequestV1) -> bool:
        """Whether this exact request wrote the activation's prior-power record.

        The record is per activation and outlives a successful PREPARE, so only the request
        that wrote it may treat it as proof that its own stop was reached.
        """
        key = self._terminal_key(
            authority.system_id, authority.activation_id, authority.run_id, authority.plan_identity
        )
        owner = self._read(f"{key}.prior-power-owner")
        return owner is not None and owner == self._authority_key(authority).encode("ascii")

    def clear_prior_power(self, binding: ExternalBootActivationBinding, plan_identity: str) -> None:
        """Forget the record once the System is back in that power, so the next stop re-observes."""
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        # The owner goes first: a failure between the two leaves a power file, never an orphan.
        for suffix in ("prior-power-owner", "prior-power"):
            with suppress(FileNotFoundError):
                os.unlink(f"{key}.{suffix}", dir_fd=self._root_fd)
        os.fsync(self._root_fd)

    def prior_power(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> PriorPower | None:
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        data = self._read(f"{key}.prior-power")
        return None if data is None else self._decode_prior_power(data)

    @staticmethod
    def _decode_prior_power(data: bytes | None) -> PriorPower:
        if data == b"running":
            return "running"
        if data == b"inactive":
            return "inactive"
        raise ProviderRecoveryRefusal("remote prior-power evidence is malformed")

    @staticmethod
    def _terminal_key(
        system_id: object, activation_id: object, run_id: object, plan_identity: str
    ) -> str:
        bound = "\0".join(
            (
                str(system_id),
                str(activation_id),
                str(run_id),
                plan_identity,
            )
        ).encode()
        return hashlib.sha256(b"kdive-remote-terminal-index-v1\0" + bound).hexdigest()

    def reopen_terminal(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteModuleTerminalRecord:
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        data = self._read(f"{key}.terminal")
        if data is None:
            self.refuse_finalized(binding, plan_identity)
            raise FileNotFoundError("remote module terminal preparation is absent")
        record = RemoteModuleTerminalRecord.from_canonical_json(data)
        if (
            str(record.request.authority.system_id) != binding.system_id
            or str(record.request.authority.activation_id) != binding.activation_id
            or str(record.request.authority.run_id) != binding.run_id
            or record.request.authority.plan_identity != plan_identity
        ):
            raise ProviderRecoveryRefusal("remote module terminal preparation binding differs")
        return record

    def preparation_authority(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> OpaqueProviderRef:
        """The exact PREPARE authority a module recovery reference was minted under.

        ``RemoteModuleRecoveryRefV2.validate_authority`` binds the reference to that one
        attempt. A later release or teardown attempt carries its own authority, which the
        service journal and cleanup-evidence gate fence; checking the reference against this
        value instead proves the durable record was not re-presented under another
        preparation.
        """
        return authority_reference(self.reopen_terminal(binding, plan_identity).request.authority)

    def teardown_preparation(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteModuleVolumePreparationRequestV1 | None:
        """Find one exact durable PREPARE request without trusting a caller-selected path."""
        names = [
            name
            for name in list_directory_descriptor(self._root_fd)
            if len(name) == 64 + len(".request") and name.endswith(".request")
        ]
        matches: list[RemoteModuleVolumePreparationRequestV1] = []
        for name in names:
            digest = name.removesuffix(".request")
            if any(character not in "0123456789abcdef" for character in digest):
                raise ProviderRecoveryRefusal("remote preparation request name is malformed")
            data = self._read(name)
            if data is None:
                continue
            persisted = _PersistedRemoteModulePreparationV1.from_canonical_json(data)
            request = persisted.request
            authority = request.authority
            if (
                str(authority.system_id) == binding.system_id
                and str(authority.activation_id) == binding.activation_id
                and str(authority.run_id) == binding.run_id
                and authority.plan_identity == plan_identity
            ):
                matches.append(request)
        if not matches:
            return None
        first = matches[0]
        if any(candidate.operation != first.operation for candidate in matches[1:]):
            raise ProviderRecoveryRefusal("remote teardown found conflicting preparation attempts")
        return first

    @staticmethod
    def _preparation_key(request: ExternalBootPreparationRequest) -> str:
        return hashlib.sha256(
            b"kdive-remote-external-boot-preparation-v1\0" + request.to_canonical_json()
        ).hexdigest()

    @staticmethod
    def _validate_preparation(
        request: ExternalBootPreparationRequest,
        receipt: ExternalBootPreparationObservation,
    ) -> None:
        ExternalBootPreparationObservation.model_validate(
            receipt.model_dump(mode="python", by_alias=True)
        )
        expected_state = "materialized" if request.phase == "materialize" else "prepared"
        if (
            receipt.state == "absent"
            or receipt.state != expected_state
            or receipt.binding != request.binding
            or receipt.plan_identity != request.plan.identity
            or receipt.authority != request.authority
            or receipt.operation_identity != request.operation_identity
        ):
            detail = (
                "phase and state differ"
                if receipt.state != expected_state
                else "receipt differs from request"
            )
            raise ValueError(f"remote preparation {detail}")

    def observe_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        data = self._read(f"{self._preparation_key(request)}.preparation")
        if data is not None:
            receipt = ExternalBootPreparationObservation.from_canonical_json(data)
            self._validate_preparation(request, receipt)
            return receipt
        return ExternalBootPreparationObservation(
            state="absent",
            binding=request.binding,
            plan_identity=request.plan.identity,
            authority=request.authority,
            operation_identity=request.operation_identity,
        )

    def publish_preparation(
        self, request: ExternalBootPreparationRequest, receipt: ExternalBootPreparationObservation
    ) -> ExternalBootPreparationObservation:
        self._validate_preparation(request, receipt)
        existing = self.observe_preparation(request)
        if existing.state != "absent":
            if existing != receipt:
                raise ProviderRecoveryRefusal(
                    "remote preparation receipt conflicts with durable bytes"
                )
            return existing
        self._publish(f"{self._preparation_key(request)}.preparation", receipt.to_canonical_json())
        reopened = self.observe_preparation(request)
        if reopened != receipt:
            raise ProviderRecoveryRefusal(
                "published remote preparation receipt failed exact reopen"
            )
        return reopened

    def publish_materialization(
        self, plan: ExternalBootPlan, materialization: ExternalBootMaterialization
    ) -> None:
        record = RemoteExternalBootMaterializationRecord(plan=plan, materialization=materialization)
        identity = materialization.identity.removeprefix("sha256:")
        self._publish(f"{identity}.materialization", record.to_canonical_json())
        self._publish(
            f"{plan.identity.removeprefix('sha256:')}.materialization-index",
            identity.encode("ascii"),
        )

    def reopen_materialization_for_plan(
        self, plan: ExternalBootPlan
    ) -> RemoteExternalBootMaterializationRecord:
        index = self._read(f"{plan.identity.removeprefix('sha256:')}.materialization-index")
        if index is None:
            raise FileNotFoundError("remote materialization plan index is absent")
        try:
            identity = index.decode("ascii")
        except UnicodeDecodeError:
            raise ProviderRecoveryRefusal(
                "remote materialization plan index is malformed"
            ) from None
        if len(identity) != 64 or any(value not in "0123456789abcdef" for value in identity):
            raise ProviderRecoveryRefusal("remote materialization plan index is malformed")
        data = self._read(f"{identity}.materialization")
        if data is None:
            raise ProviderRecoveryRefusal("remote materialization plan index is incomplete")
        record = RemoteExternalBootMaterializationRecord.from_canonical_json(data)
        if (
            record.plan != plan
            or record.materialization.identity.removeprefix("sha256:") != identity
        ):
            raise ProviderRecoveryRefusal("remote materialization plan index differs")
        return record

    def teardown_materialization(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteExternalBootMaterializationRecord | None:
        identity = plan_identity.removeprefix("sha256:")
        if len(identity) != 64 or any(
            character not in "0123456789abcdef" for character in identity
        ):
            raise ProviderRecoveryRefusal("remote teardown plan identity is malformed")
        index = self._read(f"{identity}.materialization-index")
        if index is None:
            return None
        try:
            materialization_identity = index.decode("ascii")
        except UnicodeDecodeError:
            raise ProviderRecoveryRefusal(
                "remote materialization plan index is malformed"
            ) from None
        if len(materialization_identity) != 64 or any(
            character not in "0123456789abcdef" for character in materialization_identity
        ):
            raise ProviderRecoveryRefusal("remote materialization plan index is malformed")
        data = self._read(f"{materialization_identity}.materialization")
        if data is None:
            raise ProviderRecoveryRefusal("remote materialization plan index is incomplete")
        record = RemoteExternalBootMaterializationRecord.from_canonical_json(data)
        materialization = record.materialization
        if (
            record.plan.identity != plan_identity
            or materialization.identity.removeprefix("sha256:") != materialization_identity
            or materialization.ownership.system_id != binding.system_id
            or materialization.ownership.run_id != binding.run_id
        ):
            raise ProviderRecoveryRefusal("remote teardown materialization differs from activation")
        return record

    def teardown_recovery(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteExternalBootRecoveryRecord | None:
        try:
            point = self.recovery_point(binding, plan_identity)
        except FileNotFoundError:
            return None
        return self.reopen_recovery(point)

    def reopen_materialization(
        self, materialization: ExternalBootMaterialization
    ) -> RemoteExternalBootMaterializationRecord:
        identity = materialization.identity.removeprefix("sha256:")
        data = self._read(f"{identity}.materialization")
        if data is None:
            raise FileNotFoundError("remote materialization record is absent")
        record = RemoteExternalBootMaterializationRecord.from_canonical_json(data)
        if record.materialization != materialization:
            raise ProviderRecoveryRefusal("remote materialization differs from durable record")
        return record

    def publish_recovery(self, recovery: RemoteExternalBootRecoveryRecord) -> OpaqueProviderRef:
        encoded = recovery.to_canonical_json()
        identity = hashlib.sha256(b"kdive-remote-recovery-v1\0" + encoded).hexdigest()
        self._publish(f"{identity}.recovery", encoded)
        self._publish(
            f"{self._recovery_key(recovery.binding, recovery.plan_identity)}.recovery-index",
            identity.encode("ascii"),
        )
        for reference in recovery.recovery_objects:
            self._publish(_object_index_name(reference), identity.encode("ascii"))
        return OpaqueProviderRef(ref=f"remote-external-boot/{identity}")

    def recovery_for_object(self, reference: OpaqueProviderRef) -> RemoteExternalBootRecoveryRecord:
        index = self._read(_object_index_name(reference))
        if index is None:
            raise FileNotFoundError("remote recovery object index is absent")
        try:
            identity = index.decode("ascii")
        except UnicodeDecodeError:
            raise ProviderRecoveryRefusal("remote recovery object index is malformed") from None
        data = self._read(f"{identity}.recovery")
        if (
            data is None
            or hashlib.sha256(b"kdive-remote-recovery-v1\0" + data).hexdigest() != identity
        ):
            raise ProviderRecoveryRefusal("remote recovery object index is unauthenticated")
        recovery = RemoteExternalBootRecoveryRecord.from_canonical_json(data)
        if reference not in recovery.recovery_objects:
            raise ProviderRecoveryRefusal("remote recovery object differs from durable record")
        return recovery

    @staticmethod
    def _recovery_key(binding: ExternalBootActivationBinding, plan_identity: str) -> str:
        bound = "\0".join(
            (binding.system_id, binding.activation_id, binding.run_id, plan_identity)
        ).encode()
        return hashlib.sha256(b"kdive-remote-recovery-index-v1\0" + bound).hexdigest()

    def recovery_point(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RecoveryPoint:
        index = self._read(f"{self._recovery_key(binding, plan_identity)}.recovery-index")
        if index is None:
            raise FileNotFoundError("remote recovery index is absent")
        try:
            identity = index.decode("ascii")
        except UnicodeDecodeError:
            raise ProviderRecoveryRefusal("remote recovery index is malformed") from None
        data = self._read(f"{identity}.recovery")
        if data is None:
            raise FileNotFoundError("remote recovery record is absent")
        if hashlib.sha256(b"kdive-remote-recovery-v1\0" + data).hexdigest() != identity:
            raise ProviderRecoveryRefusal("remote recovery record identity mismatched")
        recovery = RemoteExternalBootRecoveryRecord.from_canonical_json(data)
        if recovery.binding != binding or recovery.plan_identity != plan_identity:
            raise ProviderRecoveryRefusal("remote recovery index differs from durable record")
        return RecoveryPoint(
            binding=binding,
            plan_identity=plan_identity,
            materialization_identity=recovery.materialization.identity,
            recovery_ref=OpaqueProviderRef(ref=f"remote-external-boot/{identity}"),
            source_state=recovery.source_state,
            target_state=recovery.target_state,
        )

    def reopen_recovery(self, point: RecoveryPoint) -> RemoteExternalBootRecoveryRecord:
        identity = _recovery_identity(point.recovery_ref)
        data = self._read(f"{identity}.recovery")
        if data is None:
            raise FileNotFoundError("remote recovery record is absent")
        if hashlib.sha256(b"kdive-remote-recovery-v1\0" + data).hexdigest() != identity:
            raise ProviderRecoveryRefusal("remote recovery record identity mismatched")
        recovery = RemoteExternalBootRecoveryRecord.from_canonical_json(data)
        if (
            recovery.binding != point.binding
            or recovery.plan_identity != point.plan_identity
            or recovery.materialization.identity != point.materialization_identity
            or recovery.source_state != point.source_state
            or recovery.target_state != point.target_state
        ):
            raise ProviderRecoveryRefusal("remote recovery point differs from durable record")
        return recovery

    @staticmethod
    def _quarantine_name(reference: OpaqueProviderRef) -> str:
        return f"{_recovery_identity(reference)}.cleanup-quarantine"

    def _finalized_name(self, binding: ExternalBootActivationBinding, plan_identity: str) -> str:
        return f"{self._recovery_key(binding, plan_identity)}.cleanup-finalized"

    def record_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        """Record the receipt; another operation's standing receipt is a conflict.

        An adopted (managed) receipt is kept as is: no path un-adopts it (ADR-0787).
        """
        name = self._quarantine_name(receipt.recovery_ref)
        existing = self.read_cleanup_quarantine(receipt.binding, receipt.plan_identity)
        if existing is None:
            self._publish(name, receipt.to_canonical_json())
        elif existing.operation_identity != receipt.operation_identity:
            raise ProviderRecoveryRefusal("remote cleanup quarantine belongs to another operation")
        elif existing.managed:
            return
        elif existing != receipt:
            self._replace(name, existing.to_canonical_json(), receipt.to_canonical_json())

    def _receipt(self, reference: OpaqueProviderRef) -> RemoteCleanupQuarantineV1 | None:
        data = self._read(self._quarantine_name(reference))
        if data is None:
            return None
        receipt = RemoteCleanupQuarantineV1.from_canonical_json(data)
        if receipt.recovery_ref != reference:
            raise ProviderRecoveryRefusal(
                "remote cleanup quarantine receipt names another recovery record"
            )
        return receipt

    def read_cleanup_quarantine(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteCleanupQuarantineV1 | None:
        """The receipt on disk, found by the recovery index or, once that is gone, the marker."""
        index = self._read(f"{self._recovery_key(binding, plan_identity)}.recovery-index")
        marker = self._read(self._finalized_name(binding, plan_identity))
        if index is not None:
            reference = OpaqueProviderRef(ref=f"remote-external-boot/{index.decode('ascii')}")
        elif marker is not None:
            reference = _parse_finalized_marker(marker)[0].recovery_ref
        else:
            return None
        receipt = self._receipt(reference)
        if receipt is not None and (
            receipt.binding != binding or receipt.plan_identity != plan_identity
        ):
            raise ProviderRecoveryRefusal(
                "remote cleanup quarantine receipt differs from its owner"
            )
        return receipt

    def system_torn_down(self, system_id: str) -> bool:
        """Whether this System's teardown completed; it covers every activation (#3486).

        A System id is never re-activated: ``systems.id`` is generated per row and ``torn_down``
        has no outgoing edge, so refusing every activation refuses nothing live.
        """
        data = self._read(f"{self._system_teardown_key(system_id)}.teardown")
        if data is None:
            return False
        return RemoteSystemTeardownRecordV1.from_canonical_json(data).phase == "complete"

    def cleanup_finalized(self, binding: ExternalBootActivationBinding, plan_identity: str) -> bool:
        """Whether finalize removed (or is removing) its records, or its System was torn down."""
        return self._read(self._finalized_name(binding, plan_identity)) is not None or (
            self.system_torn_down(binding.system_id)
        )

    def refuse_finalized(self, binding: ExternalBootActivationBinding, plan_identity: str) -> None:
        """Refuse a finalized activation; the caller's later write is not atomic with this check.

        Nothing locks the store between the check and the write, and the module executor runs
        up to four calls at once. The ordering rests on the authority service running one
        mutation at a time per System lane (ADR-0584), so a finalize never overlaps a stage or
        publish for the same activation. A second authority instance writing this store
        directory would break that assumption.
        """
        if self.cleanup_finalized(binding, plan_identity):
            raise RemoteCleanupFinalizedError(
                "remote activation records were removed by cleanup finalization or System teardown"
            )

    def system_activations(self, system_id: str) -> list[tuple[ExternalBootActivationBinding, str]]:
        """Every activation whose anchor file content names this System (#3486).

        An unparseable file is left in place with ``_owns``'s warning: nothing proves whose it is.
        """
        found: dict[tuple[str, str, str], tuple[ExternalBootActivationBinding, str]] = {}

        def note(binding: ExternalBootActivationBinding, plan_identity: str) -> bool:
            if binding.system_id != system_id:
                return False
            found[(binding.run_id, binding.activation_id, plan_identity)] = (binding, plan_identity)
            return True

        def authority(
            value: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
        ) -> bool:
            return note(_authority_binding(value), value.plan_identity)

        def owner(
            value: ExternalBootPreparationObservation
            | RemoteExternalBootRecoveryRecord
            | RemoteCleanupQuarantineV1,
        ) -> bool:
            return note(value.binding, value.plan_identity)

        readers: tuple[tuple[str, Callable[[bytes], bool]], ...] = (
            (
                ".lifecycle-request",
                lambda data: authority(
                    _PersistedRemoteModuleLifecycleV1.from_canonical_json(data).request.authority
                ),
            ),
            (
                ".request",
                lambda data: authority(
                    _PersistedRemoteModulePreparationV1.from_canonical_json(data).request.authority
                ),
            ),
            (
                ".preparation",
                lambda data: owner(ExternalBootPreparationObservation.from_canonical_json(data)),
            ),
            (
                ".terminal",
                lambda data: authority(
                    RemoteModuleTerminalRecord.from_canonical_json(data).request.authority
                ),
            ),
            (
                ".recovery",
                lambda data: owner(RemoteExternalBootRecoveryRecord.from_canonical_json(data)),
            ),
            (
                ".cleanup-quarantine",
                lambda data: owner(RemoteCleanupQuarantineV1.from_canonical_json(data)),
            ),
            (".cleanup-finalized", lambda data: owner(_parse_finalized_marker(data)[0])),
        )
        for suffix, read in readers:
            self._scan_owned(suffix, read)
        return [found[key] for key in sorted(found)]

    def _scan_owned(
        self,
        suffix: str,
        owns: Callable[[bytes], bool],
        *,
        strict: bool = False,
    ) -> list[str]:
        """Names of this suffix whose bytes the activation owns, in no particular order.

        A file that does not parse cannot be proven foreign. The deleting scan leaves it in
        place; a ``strict`` scan, which captures what teardown must still prove, refuses it.
        Only finalize's callers are bounded, by the count in ``_finalize_cleanup_quarantine``;
        teardown and collection scans stay unbounded because they are the remedy (ADR-0787,
        #3475).
        """
        return [
            name
            for name in list_directory_descriptor(self._root_fd)
            if len(name) == 64 + len(suffix)
            and name.endswith(suffix)
            and all(character in "0123456789abcdef" for character in name.removesuffix(suffix))
            and (data := self._read(name)) is not None
            and (self._strictly_owns(name, owns, data) if strict else self._owns(name, owns, data))
        ]

    @staticmethod
    def _strictly_owns(name: str, owns: Callable[[bytes], bool], data: bytes) -> bool:
        try:
            return owns(data)
        except ValueError as error:
            raise ValueError(
                f"remote finalize cannot parse {name}, so it cannot prove which activation owns "
                "it, and it may be any activation's on this host; keep a copy of the file, find "
                "the System and run it names, or every System on this host if the copy names "
                "none, confirm that no module volume of those Systems remains unaccounted for in "
                "the storage pool, then remove it and retry finalize"
            ) from error

    @staticmethod
    def _owns(name: str, owns: Callable[[bytes], bool], data: bytes) -> bool:
        try:
            return owns(data)
        except ValueError:
            logger.warning("remote store file %s is unreadable and left in place", name)
            return False

    def _activation_files(
        self, binding: ExternalBootActivationBinding, plan_identity: str, *, keep_plan: bool
    ) -> list[str]:
        """Every per-activation file but the recovery record; anchors after the rest (#3486)."""
        derived, anchors = self._activation_file_groups(binding, plan_identity, keep_plan=keep_plan)
        return derived + anchors

    def _plan_needed_by_sibling(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> bool:
        """Whether another unfinalized activation of the Run still names this plan (#3494).

        The plan's materialization pair is shared by every activation of the Run, so only the
        last one to finalize removes it. A finalized sibling keeps its marker, so it is skipped.
        """
        return any(
            (other, plan) != (binding, plan_identity)
            and other.run_id == binding.run_id
            and plan == plan_identity
            and self._read(self._finalized_name(other, plan)) is None
            for other, plan in self.system_activations(binding.system_id)
        )

    def _activation_file_groups(
        self, binding: ExternalBootActivationBinding, plan_identity: str, *, keep_plan: bool
    ) -> tuple[list[str], list[str]]:
        """Files no content scan finds, then the anchors ``system_activations`` scans.

        Removing the first group first keeps an interrupted collection's activation findable.
        ``keep_plan`` keeps the plan's materialization pair, which every activation of the Run
        shares.
        """

        def owned(
            authority: AuthorityMutationRequestV1 | AuthorityPreparationMutationRequestV1,
        ) -> bool:
            return _authority_owned(authority, binding, plan_identity)

        derived: list[str] = []
        anchors: list[str] = []
        for suffix, dependents, parse in (
            (
                ".lifecycle-request",
                (".lifecycle-completion",),
                lambda data: owned(
                    _PersistedRemoteModuleLifecycleV1.from_canonical_json(data).request.authority
                ),
            ),
            (
                ".request",
                (".result", ".completion"),
                lambda data: owned(
                    _PersistedRemoteModulePreparationV1.from_canonical_json(data).request.authority
                ),
            ),
        ):
            for name in self._scan_owned(suffix, parse):
                stem = name.removesuffix(suffix)
                derived += [f"{stem}{dependent}" for dependent in dependents]
                anchors.append(name)
        anchors += self._scan_owned(
            ".preparation",
            lambda data: (
                (receipt := ExternalBootPreparationObservation.from_canonical_json(data)).binding
                == binding
                and receipt.plan_identity == plan_identity
            ),
        )
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        derived += [
            f"{key}.{suffix}" for suffix in ("restored", "prior-power-owner", "prior-power")
        ]
        anchors.append(f"{key}.terminal")
        if not keep_plan:
            derived += self._materialization_files(binding, plan_identity)
        return derived, anchors

    def _materialization_files(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> list[str]:
        index_name = f"{plan_identity.removeprefix('sha256:')}.materialization-index"
        index = self._read(index_name)
        if index is None:
            return []
        identity = index.decode("ascii", errors="replace")
        if len(identity) != 64 or any(value not in "0123456789abcdef" for value in identity):
            raise ProviderRecoveryRefusal("remote materialization plan index is malformed")
        data = self._read(f"{identity}.materialization")
        if data is not None:
            ownership = RemoteExternalBootMaterializationRecord.from_canonical_json(
                data
            ).materialization.ownership
            if ownership.system_id != binding.system_id or ownership.run_id != binding.run_id:
                raise ProviderRecoveryRefusal(
                    "remote materialization differs from the finalized activation"
                )
        return [f"{identity}.materialization", index_name]

    def finalized_teardown_refs(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteFinalizedTeardownRefsV1 | None:
        """The refs teardown proves absent for a finalized activation; ``None`` if not finalized.

        A marker written before it carried refs is only usable while the activation's terminal
        record still stands, because that earlier finalize removed nothing else. Without it
        nothing says what to prove absent, so teardown is refused instead of passing vacuously.
        """
        data = self._read(self._finalized_name(binding, plan_identity))
        if data is None:
            return None
        refs = _parse_finalized_marker(data)[1]
        if refs is not None:
            return refs
        key = self._terminal_key(
            binding.system_id, binding.activation_id, binding.run_id, plan_identity
        )
        if self._read(f"{key}.terminal") is not None:
            return None
        raise ValueError(
            "remote cleanup finalized marker has no teardown refs and the activation's terminal "
            "record is gone, so teardown is refused; confirm no module volume of System "
            f"{binding.system_id} run {binding.run_id} (plan {plan_identity}) remains in the "
            "storage pool, then remove "
            f"{self._finalized_name(binding, plan_identity)} and retry teardown"
        )

    def _teardown_refs(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteFinalizedTeardownRefsV1:
        try:
            module = self.reopen_terminal(binding, plan_identity).response.recovery
            volumes = (
                (module.pool.ref, module.source_volume.ref),
                (module.pool.ref, module.scratch_volume.ref),
            )
        except FileNotFoundError:
            volumes = ()
        nonce = self._owned_operation_nonce(binding, plan_identity)
        record = self.teardown_materialization(binding, plan_identity)
        return RemoteFinalizedTeardownRefsV1(
            module_volumes=volumes,
            operation_nonce=nonce,
            boot_artifacts=() if record is None else tuple(_boot_artifacts(record.materialization)),
        )

    def _owned_operation_nonce(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> str | None:
        operations: list[RemoteModuleOperationV1] = []

        def collect(data: bytes) -> bool:
            request = _PersistedRemoteModulePreparationV1.from_canonical_json(data).request
            if owned := _authority_owned(request.authority, binding, plan_identity):
                operations.append(request.operation)
            return owned

        self._scan_owned(".request", collect, strict=True)
        # The same comparison teardown_preparation applies, so finalize captures no nonce that
        # teardown would have refused.
        if any(operation != operations[0] for operation in operations[1:]):
            raise ProviderRecoveryRefusal(
                "remote finalize found conflicting preparation requests for one activation; "
                "resolve the duplicate .request records before finalizing"
            )
        return operations[0].operation_nonce if operations else None

    def activation_claims(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteFinalizedTeardownRefsV1 | None:
        """Every pool volume the activation's records name; ``None`` once finalized (#3486)."""
        if self._read(self._finalized_name(binding, plan_identity)) is not None:
            return None
        volumes: set[tuple[str, str]] = set()
        materializations: list[ExternalBootMaterialization] = []
        modules: list[RemoteModuleRecoveryRefV2] = []
        with suppress(FileNotFoundError):
            modules.append(self.reopen_terminal(binding, plan_identity).response.recovery)
        if (recovery := self.teardown_recovery(binding, plan_identity)) is not None:
            modules.append(recovery.module_recovery)
            materializations.append(recovery.materialization)
        for module in modules:
            volumes |= {
                (module.pool.ref, module.source_volume.ref),
                (module.pool.ref, module.scratch_volume.ref),
            }
        if (record := self._claimed_materialization(binding, plan_identity)) is not None:
            materializations.append(record.materialization)

        def preparation(data: bytes) -> bool:
            receipt = ExternalBootPreparationObservation.from_canonical_json(data)
            owned = receipt.binding == binding and receipt.plan_identity == plan_identity
            if owned and receipt.materialization is not None:
                materializations.append(receipt.materialization)
            return owned

        self._scan_owned(".preparation", preparation, strict=True)
        boot = {item for value in materializations for item in _boot_artifacts(value)}
        return RemoteFinalizedTeardownRefsV1(
            module_volumes=tuple(sorted(volumes)),
            operation_nonce=self._owned_operation_nonce(binding, plan_identity),
            boot_artifacts=tuple(sorted(boot, key=lambda item: (item.ref, item.kind))),
        )

    def _claimed_materialization(
        self, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> RemoteExternalBootMaterializationRecord | None:
        """The plan's materialization; ``None`` also once collection removed it before its index."""
        index = self._read(f"{plan_identity.removeprefix('sha256:')}.materialization-index")
        if index is not None:
            identity = index.decode("ascii", errors="replace")
            if (
                len(identity) == 64
                and all(value in "0123456789abcdef" for value in identity)
                and self._read(f"{identity}.materialization") is None
            ):
                return None
        return self.teardown_materialization(binding, plan_identity)

    def _write_finalized_marker(self, receipt: RemoteCleanupQuarantineV1) -> None:
        """Publish the marker once; a retry keeps the refs captured before anything was removed."""
        name = self._finalized_name(receipt.binding, receipt.plan_identity)
        existing = self._read(name)
        cleaned = receipt.model_copy(update={"managed": False})
        if existing is not None:
            held, refs = _parse_finalized_marker(existing)
            if held != cleaned:
                raise ProviderRecoveryRefusal(
                    "remote cleanup finalized marker conflicts with durable bytes"
                )
            if refs is not None:
                return
        marker = RemoteCleanupFinalizedMarkerV1(
            receipt=cleaned, teardown=self._teardown_refs(receipt.binding, receipt.plan_identity)
        )
        if existing is None:
            self._publish(name, marker.to_canonical_json())
        else:
            self._replace(name, existing, marker.to_canonical_json())

    def _recovery_record(self, identity: str) -> RemoteExternalBootRecoveryRecord | None:
        data = self._read(f"{identity}.recovery")
        if data is None:
            return None
        if hashlib.sha256(b"kdive-remote-recovery-v1\0" + data).hexdigest() != identity:
            raise ProviderRecoveryRefusal("remote recovery record identity mismatched")
        return RemoteExternalBootRecoveryRecord.from_canonical_json(data)

    def cleanup_quarantine_for_object(
        self, reference: OpaqueProviderRef
    ) -> RemoteCleanupQuarantineV1 | None:
        """The receipt actually on disk, whatever the finalization marker says."""
        return self._receipt(reference)

    def adopt_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        with self._exclusive():
            self._replace(
                self._quarantine_name(receipt.recovery_ref),
                receipt.to_canonical_json(),
                receipt.model_copy(update={"managed": True}).to_canonical_json(),
            )

    def finalize_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        """Remove the activation's records and indexes, then the receipt that proves the cleanup.

        A finalized marker is written first and the receipt goes last. The activation's other
        files go before the recovery record, whose identity and plan name them. The receipt is
        found by its record's identity, or through the marker once the index is gone, so an
        interrupted finalization is repeatable until nothing but the marker remains. The marker
        keeps a finalized cleanup distinguishable from a record that never existed. An index is
        removed only while it names this record, whether or not the record is still present.
        The ordering against a concurrent stage rests on the System lane (``refuse_finalized``).
        It runs under the store lock and writes nothing for any activation of a torn-down System,
        so it cannot publish a marker after collection (#3472, #3486). The receipt is read again
        under the lock: one an adoption made ``managed`` or removed leaves everything in place, and
        one that changed otherwise is refused (#3493).
        """
        with self._exclusive():
            if not self.system_torn_down(receipt.binding.system_id):
                self._finalize_cleanup_quarantine(receipt)

    def _finalize_cleanup_quarantine(self, receipt: RemoteCleanupQuarantineV1) -> None:
        current = self._receipt(receipt.recovery_ref)
        if current is None or current.managed:
            return
        if current != receipt:
            raise ProviderRecoveryRefusal(
                "remote cleanup proof differs from the quarantine receipt"
            )
        counted = sum(
            1
            for name in list_directory_descriptor(self._root_fd)
            for suffix in _FINALIZE_COUNTED_SUFFIXES
            if len(name) == 64 + len(suffix)
            and name.endswith(suffix)
            and all(character in "0123456789abcdef" for character in name.removesuffix(suffix))
        )
        if counted > _FINALIZE_SCAN_BOUND:
            raise ProviderRecoveryRefusal(
                "remote preparation store holds more than 4096 request, lifecycle-request and "
                "preparation records, so finalize is refused; tear down Systems on this host so "
                "teardown collects their records, or, once every System on this host is torn "
                "down, follow the runbook procedure Clear store files of torn-down Systems, then "
                "retry"
            )
        identity = _recovery_identity(receipt.recovery_ref)
        record = self._recovery_record(identity)
        if record is not None and (
            record.binding != receipt.binding or record.plan_identity != receipt.plan_identity
        ):
            raise ProviderRecoveryRefusal(
                "remote recovery record differs from the quarantine receipt"
            )
        self._write_finalized_marker(receipt)
        names = self._activation_files(
            receipt.binding,
            receipt.plan_identity,
            keep_plan=self._plan_needed_by_sibling(receipt.binding, receipt.plan_identity),
        )
        owned = identity.encode("ascii")
        if record is not None:
            names += [
                name
                for reference in record.recovery_objects
                if self._read(name := _object_index_name(reference)) == owned
            ]
        index = f"{self._recovery_key(receipt.binding, receipt.plan_identity)}.recovery-index"
        if self._read(index) == owned:
            names.append(index)
        names += [f"{identity}.recovery", self._quarantine_name(receipt.recovery_ref)]
        for name in names:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=self._root_fd)
        os.fsync(self._root_fd)

    def collect_torn_down_activation(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: str,
        *,
        keep_plan: bool = False,
    ) -> None:
        """Remove the activation the teardown proved; keep an adopted receipt and what finds it.

        The store lock makes the ``managed`` read and the unlinks atomic against an adoption
        (ADR-0787, #3472, #3486).
        """
        with self._exclusive():
            self._collect(binding, plan_identity, keep_plan=keep_plan)

    def collect_proven_activation(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: str,
        proven: RemoteFinalizedTeardownRefsV1 | None,
        *,
        keep_plan: bool,
    ) -> bool:
        """Collect another activation of the System while its records name only proven volumes.

        The proof ran outside the lock; ``False`` keeps every file because the records grew.
        """
        with self._exclusive():
            if not _claims_within(self.activation_claims(binding, plan_identity), proven):
                return False
            self._collect(binding, plan_identity, keep_plan=keep_plan)
            return True

    def _collect(
        self, binding: ExternalBootActivationBinding, plan_identity: str, *, keep_plan: bool
    ) -> None:
        """Unlink non-anchors (the recovery index among them), then anchors, records, marker.

        A crash at any unlink leaves an anchor, so a later collection finds what remains: the
        named activation through its ``.teardown`` record, another through
        ``system_activations`` (ADR-0787, #3472, #3486).
        """
        index = f"{self._recovery_key(binding, plan_identity)}.recovery-index"
        marker = self._finalized_name(binding, plan_identity)
        derived, anchors = self._activation_file_groups(binding, plan_identity, keep_plan=keep_plan)
        records: list[str] = []
        adopted = False
        for identity in self._collected_identities(binding, plan_identity, index, marker):
            indexes, files, managed = self._recovery_files(identity, binding, plan_identity)
            derived += indexes
            records += files
            adopted = adopted or managed
        names = [*derived, *([] if adopted else [index]), *anchors, *records, marker]
        for name in names:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=self._root_fd)
        os.fsync(self._root_fd)

    def _collected_identities(
        self,
        binding: ExternalBootActivationBinding,
        plan_identity: str,
        index: str,
        marker: str,
    ) -> list[str]:
        """Recovery identities the index, the marker or the activation's own records name.

        Finding them by content lets the index go before the records it names.
        """
        references: list[OpaqueProviderRef] = []
        if (named := self._read(index)) is not None:
            identity = named.decode("ascii", errors="replace")
            references.append(OpaqueProviderRef(ref=f"remote-external-boot/{identity}"))
        if (data := self._read(marker)) is not None:
            references.append(_parse_finalized_marker(data)[0].recovery_ref)
        identities = [_recovery_identity(reference) for reference in references]

        def owner(value: RemoteExternalBootRecoveryRecord | RemoteCleanupQuarantineV1) -> bool:
            return value.binding == binding and value.plan_identity == plan_identity

        identities += [
            name.removesuffix(".recovery")
            for name in self._scan_owned(
                ".recovery",
                lambda data: owner(RemoteExternalBootRecoveryRecord.from_canonical_json(data)),
            )
        ]
        identities += [
            name.removesuffix(".cleanup-quarantine")
            for name in self._scan_owned(
                ".cleanup-quarantine",
                lambda data: owner(RemoteCleanupQuarantineV1.from_canonical_json(data)),
            )
        ]
        return list(dict.fromkeys(identities))

    def _recovery_files(
        self, identity: str, binding: ExternalBootActivationBinding, plan_identity: str
    ) -> tuple[list[str], list[str], bool]:
        """Object indexes naming the record; the record and receipt unless adopted; ``managed``."""
        reference = OpaqueProviderRef(ref=f"remote-external-boot/{identity}")
        record = self._recovery_record(identity)
        receipt = self._receipt(reference)
        if record is not None and (
            record.binding != binding or record.plan_identity != plan_identity
        ):
            raise ProviderRecoveryRefusal("remote recovery index differs from durable record")
        if receipt is not None and (
            receipt.binding != binding or receipt.plan_identity != plan_identity
        ):
            raise ProviderRecoveryRefusal(
                "remote cleanup quarantine receipt differs from its owner"
            )
        owned = identity.encode("ascii")
        names = (
            []
            if record is None
            else [
                name
                for object_ref in record.recovery_objects
                if self._read(name := _object_index_name(object_ref)) == owned
            ]
        )
        managed = receipt is not None and receipt.managed
        files = [] if managed else [f"{identity}.recovery", self._quarantine_name(reference)]
        return names, files, managed


class RemoteExternalBootRecoveryRecord(BaseModel):
    """Durable facts sufficient to reconstruct one remote activation after process loss."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)

    schema_: Literal["remote-libvirt-external-boot-recovery-v1"] = Field(
        "remote-libvirt-external-boot-recovery-v1", alias="schema"
    )
    binding: ExternalBootActivationBinding
    plan_identity: Digest
    materialization: ExternalBootMaterialization
    definition: RemoteExternalBootDefinition
    module_recovery: RemoteModuleRecoveryRefV2
    source_state: ProviderStateIdentity
    target_state: ProviderStateIdentity
    prior_power: PriorPower
    recovery_objects: Annotated[tuple[OpaqueProviderRef, ...], Field(min_length=1, max_length=32)]

    @field_validator("recovery_objects")
    @classmethod
    def _objects_are_canonical(
        cls, values: tuple[OpaqueProviderRef, ...]
    ) -> tuple[OpaqueProviderRef, ...]:
        encoded = [value.to_canonical_json() for value in values]
        if len(encoded) != len(set(encoded)) or encoded != sorted(encoded):
            raise ValueError("recovery objects must be unique and canonically sorted")
        return values

    @model_validator(mode="after")
    def _identities_are_exact(self) -> Self:
        owner = self.binding
        materialization = self.materialization
        module = self.module_recovery
        definition = self.definition
        if (
            materialization.ownership.system_id != owner.system_id
            or materialization.ownership.run_id != owner.run_id
            or definition.binding != owner
            or module.system_id != owner.system_id
            or module.run_id != owner.run_id
        ):
            raise ValueError("remote recovery ownership differs from activation binding")
        if (
            materialization.plan_identity != self.plan_identity
            or definition.plan_identity != self.plan_identity
            or module.plan_identity != self.plan_identity
            or definition.materialization_identity != materialization.identity
        ):
            raise ValueError("remote recovery plan or materialization identity differs")
        if (
            self.source_state.definition != definition.source_definition
            or self.target_state.definition != definition.target_definition
        ):
            raise ValueError("remote recovery definition state differs")
        source_modules = self.source_state.modules
        target_modules = self.target_state.modules
        if (
            source_modules.state == "present"
            and source_modules.manifest != materialization.source_module_manifest
        ) or (
            target_modules.state != "present"
            or target_modules.manifest != materialization.installed_module_tree
        ):
            raise ValueError("remote recovery module state differs")
        encoded = self.to_canonical_json()
        if len(encoded) > _MAX_RECORD_BYTES:
            # Pydantic rewraps this as ValidationError; the type keeps the reason off plain
            # ValueError, and from_canonical_json raises the same refusal unwrapped.
            raise ProviderRecoveryRefusal("remote recovery record exceeds 1048576 bytes")
        return self

    def to_canonical_json(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_RECORD_BYTES:
            raise ProviderRecoveryRefusal("remote recovery record exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal("remote recovery record is not canonical JSON")
        return value


class RemoteExternalBootMaterializationRecord(BaseModel):
    """Exact plan and materialization retained for provider-host PREPARE restart."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)

    schema_: Literal["remote-libvirt-external-boot-materialization-v1"] = Field(
        "remote-libvirt-external-boot-materialization-v1", alias="schema"
    )
    plan: ExternalBootPlan
    materialization: ExternalBootMaterialization

    @model_validator(mode="after")
    def _materialization_matches_plan(self) -> Self:
        materialization = self.materialization
        if (
            materialization.plan_identity != self.plan.identity
            or materialization.ownership.system_id != self.plan.ownership.system_id
            or materialization.ownership.run_id != self.plan.ownership.run_id
        ):
            raise ValueError("remote materialization differs from its durable plan")
        return self

    def to_canonical_json(self) -> bytes:
        encoded = canonical_model_bytes(self)
        if len(encoded) > _MAX_RECORD_BYTES:
            raise ProviderRecoveryRefusal("remote materialization record exceeds 1048576 bytes")
        return encoded

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        if len(data) > _MAX_RECORD_BYTES:
            raise ProviderRecoveryRefusal("remote materialization record exceeds 1048576 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ProviderRecoveryRefusal("remote materialization record is not canonical JSON")
        return value
