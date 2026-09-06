"""Closed provider-neutral authority and journal values (ADR-0584)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from kdive.domain.external_boot_activation import (
    ExternalBootCleanupEvidenceV1,
    ExternalBootReleaseEvidenceV1,
    ExternalBootTeardownEvidenceV1,
)
from kdive.providers.ports.external_boot import (
    ExternalBootPlan,
    ExternalBootPreparationObservation,
    KernelIdentity,
    RunningKernelObservation,
)

MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
MAX_MESSAGE_BYTES = 1_048_576
MAX_ENVELOPE_BYTES = MAX_MESSAGE_BYTES
MAX_RECOVERY_OBJECTS = 1_024
GENESIS_DIGEST = "sha256:" + "0" * 64
_TEARDOWN_PROOF_IDENTITY_PREFIX = b"kdive-external-boot-teardown-proof-v1\0"

type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type PositiveBigInt = Annotated[int, Field(ge=1, le=MAX_SIGNED_BIGINT)]
type Purpose = Literal["activate", "recover", "resolve-conflict", "release", "teardown"]
type ObservationCategory = Literal["absent", "source", "target", "mixed", "unreadable", "conflict"]
type RecoveryOrphanDisposition = Literal["delete", "adopt"]


def authority_server_name(authority_instance: str) -> str:
    """Derive the stable reserved DNS name bound into an authority server certificate."""
    digest = hashlib.sha256(authority_instance.encode("utf-8")).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"{encoded}.authority.kdive.invalid"


async def read_frame(reader: asyncio.StreamReader, *, maximum: int) -> bytes:
    """Read one network-order frame after rejecting its bound before allocation."""
    size = int.from_bytes(await reader.readexactly(4), "big")
    if size < 1 or size > maximum:
        raise ValueError("invalid-request")
    return await reader.readexactly(size)


def _bounded_text(value: str, *, maximum: int = 255) -> str:
    if not value or len(value.encode("utf-8")) > maximum:
        raise ValueError(f"value must contain 1 through {maximum} UTF-8 bytes")
    return value


def _canonical_bytes(value: BaseModel) -> bytes:
    encoded = json.dumps(
        value.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("authority value exceeds 1048576 bytes")
    return encoded


def canonical_teardown_proof_bytes(proof: AuthorityTeardownProofV1) -> bytes:
    """Encode the exact bounded UTF-8 proof document authenticated by the journal head."""
    return _canonical_bytes(proof)


def teardown_proof_digest(proof: AuthorityTeardownProofV1) -> str:
    """Name one closed teardown disposition with the authority observation digest."""
    return (
        "sha256:"
        + hashlib.sha256(
            _TEARDOWN_PROOF_IDENTITY_PREFIX + canonical_teardown_proof_bytes(proof)
        ).hexdigest()
    )


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True)

    @model_validator(mode="after")
    def _serialized_size_is_bounded(self) -> Self:
        _canonical_bytes(self)
        return self


class AuthorityOperation(StrEnum):
    MATERIALIZE = "materialize"
    PREPARE = "prepare"
    ACTIVATE = "activate"
    RECOVER = "recover"
    RESOLVE_CONFLICT = "resolve-conflict"
    RELEASE = "release"
    CLEANUP = "cleanup"
    TEARDOWN = "teardown"
    DEADLINE = "deadline"
    RECOVERY_ATTEMPT = "recovery-attempt"
    FAIL = "fail"


_PURPOSE_OPERATIONS: dict[str, frozenset[AuthorityOperation]] = {
    "activate": frozenset(
        {
            AuthorityOperation.MATERIALIZE,
            AuthorityOperation.PREPARE,
            AuthorityOperation.ACTIVATE,
            AuthorityOperation.DEADLINE,
            AuthorityOperation.FAIL,
        }
    ),
    "recover": frozenset(
        {
            AuthorityOperation.RECOVER,
            AuthorityOperation.DEADLINE,
            AuthorityOperation.RECOVERY_ATTEMPT,
            AuthorityOperation.FAIL,
        }
    ),
    "resolve-conflict": frozenset({AuthorityOperation.RESOLVE_CONFLICT, AuthorityOperation.FAIL}),
    "release": frozenset(
        {
            AuthorityOperation.RECOVER,
            AuthorityOperation.RELEASE,
            AuthorityOperation.CLEANUP,
            AuthorityOperation.FAIL,
        }
    ),
    "teardown": frozenset({AuthorityOperation.TEARDOWN, AuthorityOperation.FAIL}),
}


def operation_is_permitted(purpose: str, operation: AuthorityOperation) -> bool:
    """Return whether ``operation`` is a legal commit point for ``purpose``.

    Provider adapters receive the commit point inside ``AuthorityCommitContextV1``, whose
    ``commit_point`` is an ``AuthorityOperation``, so the model layer guarantees the member
    itself. It does not guarantee the two cross-model facts each adapter must still check:
    that the operation is legal for the request's purpose, and that it is the same operation
    the request carries. This exposes the one table rather than letting every adapter copy it.
    """
    return operation in _PURPOSE_OPERATIONS.get(purpose, frozenset())


class _AuthorityBinding(_ClosedValue):
    schema_: Literal["external-boot-authority-v1"] = Field(
        "external-boot-authority-v1", alias="schema"
    )
    authority_id: UUID
    generation: PositiveBigInt
    system_id: UUID
    activation_id: UUID
    run_id: UUID
    plan_identity: Digest
    purpose: Purpose
    operation: AuthorityOperation
    provider_kind: str
    authority_instance: str
    operation_identity: str
    operation_digest: Digest

    @field_validator("provider_kind", "authority_instance", "operation_identity")
    @classmethod
    def _identifiers_are_bounded(cls, value: str) -> str:
        return _bounded_text(value)

    @model_validator(mode="after")
    def _operation_matches_purpose(self) -> Self:
        if self.operation not in _PURPOSE_OPERATIONS[self.purpose]:
            raise ValueError("authority operation is not allowed for its purpose")
        return self


class AuthorityTakeoverRequestV1(_AuthorityBinding):
    """Immutable allocating-authority facts used to install a takeover watermark."""

    @model_validator(mode="after")
    def _takeover_is_not_a_preparation_commit(self) -> Self:
        if self.operation in {AuthorityOperation.MATERIALIZE, AuthorityOperation.PREPARE}:
            raise ValueError("takeover request requires the immutable root operation")
        return self


class RecoveryObjectBindingV1(_ClosedValue):
    """Stable provider recovery-object ownership across authority takeover."""

    system_id: UUID
    activation_id: UUID
    reference: str

    @field_validator("reference")
    @classmethod
    def _reference_is_bounded(cls, value: str) -> str:
        return _bounded_text(value, maximum=1024)


def _canonical_recovery_objects(
    values: tuple[RecoveryObjectBindingV1, ...],
) -> tuple[RecoveryObjectBindingV1, ...]:
    encoded = [_canonical_bytes(value) for value in values]
    if len(encoded) != len(set(encoded)):
        raise ValueError("recovery objects must be duplicate-free")
    if encoded != sorted(encoded):
        raise ValueError("recovery objects must be sorted by canonical bytes")
    return values


class AuthorityMutationRequestV1(_AuthorityBinding):
    """One current-authority provider mutation request."""

    attempt_id: UUID
    expected_source_identity: str
    intended_target_identity: str
    recovery_objects: Annotated[
        tuple[RecoveryObjectBindingV1, ...], Field(max_length=MAX_RECOVERY_OBJECTS)
    ]

    @field_validator("expected_source_identity", "intended_target_identity")
    @classmethod
    def _provider_identity_is_bounded(cls, value: str) -> str:
        return _bounded_text(value, maximum=1024)

    _objects_are_canonical = field_validator("recovery_objects")(_canonical_recovery_objects)

    @model_validator(mode="after")
    def _recovery_objects_belong_to_request(self) -> Self:
        if any(
            item.system_id != self.system_id or item.activation_id != self.activation_id
            for item in self.recovery_objects
        ):
            raise ValueError("recovery object does not belong to request binding")
        return self

    @model_validator(mode="after")
    def _ordinary_request_is_not_preparation(self) -> Self:
        if self.operation in {AuthorityOperation.MATERIALIZE, AuthorityOperation.PREPARE}:
            raise ValueError("preparation operation requires its exact plan")
        return self


class AuthorityTeardownMutationRequestV1(_AuthorityBinding):
    """One authority-owned System teardown without recovery-point assumptions."""

    schema_: Literal["external-boot-authority-teardown-request-v1"] = Field(
        "external-boot-authority-teardown-request-v1", alias="schema"
    )
    attempt_id: UUID

    @model_validator(mode="after")
    def _is_only_the_teardown_commit(self) -> Self:
        if self.purpose != "teardown" or self.operation is not AuthorityOperation.TEARDOWN:
            raise ValueError("teardown request requires the teardown purpose and operation")
        return self


class AuthorityConflictResolutionRequestV1(AuthorityMutationRequestV1):
    """Closed conflict mutation carrying the caller observation the authority must recheck."""

    expected_observed_composite: Digest

    @model_validator(mode="after")
    def _is_only_the_conflict_resolution_commit(self) -> Self:
        if (
            self.purpose != "resolve-conflict"
            or self.operation is not AuthorityOperation.RESOLVE_CONFLICT
        ):
            raise ValueError("expected observed composite requires resolve-conflict")
        return self


class AuthorityPreparationMutationRequestV1(_AuthorityBinding):
    """A materialize or prepare mutation carrying its trusted durable plan projection."""

    attempt_id: UUID
    expected_source_identity: str
    intended_target_identity: str
    recovery_objects: Annotated[
        tuple[RecoveryObjectBindingV1, ...], Field(max_length=MAX_RECOVERY_OBJECTS)
    ]
    plan: ExternalBootPlan

    @field_validator("expected_source_identity", "intended_target_identity")
    @classmethod
    def _provider_identity_is_bounded(cls, value: str) -> str:
        return _bounded_text(value, maximum=1024)

    _objects_are_canonical = field_validator("recovery_objects")(_canonical_recovery_objects)

    @model_validator(mode="after")
    def _preparation_shape_is_bound(self) -> Self:
        if self.operation not in {AuthorityOperation.MATERIALIZE, AuthorityOperation.PREPARE}:
            raise ValueError("preparation request requires a preparation operation")
        if self.plan.identity != self.plan_identity:
            raise ValueError("preparation plan does not match its bound identity")
        if self.plan.ownership.system_id != str(
            self.system_id
        ) or self.plan.ownership.run_id != str(self.run_id):
            raise ValueError("preparation plan ownership does not match request binding")
        if any(
            item.system_id != self.system_id or item.activation_id != self.activation_id
            for item in self.recovery_objects
        ):
            raise ValueError("recovery object does not belong to request binding")
        return self


class AuthorityHealthRequestV1(_ClosedValue):
    """Authentication-only request with no provider operation (ADR-0606)."""

    schema_: Literal["external-boot-authority-health-v1"] = Field(
        "external-boot-authority-health-v1", alias="schema"
    )


class AuthorityHealthAcknowledgementV1(_ClosedValue):
    """Authentication succeeded for the current worker incarnation (ADR-0606)."""

    schema_: Literal["external-boot-authority-health-v1"] = Field(
        "external-boot-authority-health-v1", alias="schema"
    )


class AuthorityRecoveryOrphanDispositionRequestV1(_ClosedValue):
    """Closed fixed-route request for one claimed quarantine disposition job."""

    schema_: Literal["external-boot-authority-orphan-disposition-v1"] = Field(
        "external-boot-authority-orphan-disposition-v1", alias="schema"
    )
    request_id: UUID
    job_id: UUID
    job_attempt: PositiveBigInt


class AuthorityRecoveryOrphanDispositionResponseV1(_ClosedValue):
    """Durable result for the same exact orphan disposition request."""

    schema_: Literal["external-boot-authority-orphan-disposition-v1"] = Field(
        "external-boot-authority-orphan-disposition-v1", alias="schema"
    )
    request_id: UUID
    disposition: RecoveryOrphanDisposition
    objects: Annotated[int, Field(ge=0, le=64)]


type AuthorityRequestV1 = (
    AuthorityTakeoverRequestV1
    | AuthorityConflictResolutionRequestV1
    | AuthorityMutationRequestV1
    | AuthorityTeardownMutationRequestV1
    | AuthorityPreparationMutationRequestV1
    | AuthorityHealthRequestV1
    | AuthorityRecoveryOrphanDispositionRequestV1
)
_AUTHORITY_REQUEST_ADAPTER = TypeAdapter(AuthorityRequestV1)


def _parse_authority_request_bytes(payload: bytes) -> AuthorityRequestV1:
    return _AUTHORITY_REQUEST_ADAPTER.validate_json(payload)


def decode_authority_request(payload: bytes) -> AuthorityRequestV1:
    """Decode one canonical bounded provider-neutral authority request."""
    if type(payload) is not bytes:
        raise TypeError("external-boot authority request must be bytes")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("external-boot authority request exceeds configured byte maximum")
    try:
        request = _parse_authority_request_bytes(payload)
        if _canonical_bytes(request) != payload:
            raise ValueError
    except ValidationError, ValueError:
        raise ValueError("invalid external-boot authority request") from None
    return request


class AuthorityAcknowledgementV1(_ClosedValue):
    """Anchored takeover acknowledgement returned for migration 0122 promotion."""

    schema_: Literal["external-boot-authority-v1"] = Field(
        "external-boot-authority-v1", alias="schema"
    )
    authority_id: UUID
    generation: PositiveBigInt
    system_id: UUID
    journal_sequence: PositiveBigInt
    journal_digest: Digest
    positive_quiescence_digest: Digest


class AuthorityObservationV1(_ClosedValue):
    """Bounded provider state observation."""

    schema_: Literal["external-boot-authority-v1"] = Field(
        "external-boot-authority-v1", alias="schema"
    )
    observation_id: UUID
    category: ObservationCategory
    composite_state: Digest


class AuthorityRunningObservationV1(_ClosedValue):
    """Read-only kernel evidence; separate from retained version-1 journal observations."""

    schema_: Literal["external-boot-running-observation-v1"] = Field(
        "external-boot-running-observation-v1", alias="schema"
    )
    identity: KernelIdentity
    cmdline_hex: Annotated[str, Field(max_length=4096, pattern=r"^(?:[0-9a-f]{2})*$")]
    expected_cmdline_hex: Annotated[str, Field(max_length=4096, pattern=r"^(?:[0-9a-f]{2})*$")]

    @classmethod
    def from_observation(cls, value: RunningKernelObservation) -> Self:
        return cls(
            identity=value.identity,
            cmdline_hex=value.cmdline.hex(),
            expected_cmdline_hex=value.expected_cmdline.hex(),
        )

    def to_observation(self) -> RunningKernelObservation:
        return RunningKernelObservation(
            identity=self.identity,
            cmdline=bytes.fromhex(self.cmdline_hex),
            expected_cmdline=bytes.fromhex(self.expected_cmdline_hex),
        )


class AuthorityPreparationResponseV1(_ClosedValue):
    """Exact terminal checkpoint and provider receipt for one preparation phase."""

    schema_: Literal["external-boot-authority-preparation-response-v1"] = Field(
        "external-boot-authority-preparation-response-v1", alias="schema"
    )
    observation: AuthorityObservationV1
    receipt: ExternalBootPreparationObservation
    journal_sequence: PositiveBigInt
    journal_digest: Digest

    @model_validator(mode="after")
    def _receipt_matches_observation(self) -> Self:
        if self.receipt.identity != self.observation.composite_state:
            raise ValueError("preparation receipt does not match its journal observation")
        return self


class AuthorityTeardownCompleteReadyV1(_ClosedValue):
    """Authority proved domain and owned storage absence for a ready debit."""

    disposition: Literal["complete_ready"]
    teardown_evidence: ExternalBootTeardownEvidenceV1
    release_evidence: ExternalBootReleaseEvidenceV1
    release_identity: Digest
    cleanup_evidence: ExternalBootCleanupEvidenceV1

    @model_validator(mode="after")
    def _ready_evidence_is_closed(self) -> Self:
        if (
            self.release_identity != self.release_evidence.identity
            or self.cleanup_evidence.mode != "system_teardown"
            or self.cleanup_evidence.release_identity != self.release_identity
            or self.cleanup_evidence.teardown_identity != self.teardown_evidence.identity
        ):
            raise ValueError("ready teardown proof evidence does not match")
        return self


class AuthorityTeardownCompletePendingV1(_ClosedValue):
    """Authority proved absence before a pending debit ever became creditable."""

    disposition: Literal["complete_pending"]
    teardown_evidence: ExternalBootTeardownEvidenceV1
    cleanup_evidence: ExternalBootCleanupEvidenceV1

    @model_validator(mode="after")
    def _pending_evidence_is_closed(self) -> Self:
        if (
            self.cleanup_evidence.mode != "pending_system_teardown"
            or self.cleanup_evidence.release_identity is not None
            or self.cleanup_evidence.teardown_identity != self.teardown_evidence.identity
        ):
            raise ValueError("pending teardown proof evidence does not match")
        return self


class AuthorityTeardownCompleteReleasedV1(_ClosedValue):
    """Authority proved domain absence after a prior release was already finalized."""

    disposition: Literal["complete_released"]
    teardown_evidence: ExternalBootTeardownEvidenceV1


class AuthorityTeardownRetainedQuarantineV1(_ClosedValue):
    """Authority retained unresolved residue; this is deliberately not a teardown proof."""

    disposition: Literal["retained_quarantine"]


type AuthorityTeardownProofV1 = Annotated[
    AuthorityTeardownCompleteReadyV1
    | AuthorityTeardownCompletePendingV1
    | AuthorityTeardownCompleteReleasedV1
    | AuthorityTeardownRetainedQuarantineV1,
    Field(discriminator="disposition"),
]


class AuthorityTeardownResponseV1(_ClosedValue):
    """One terminal journal receipt plus a closed, digest-bound teardown disposition."""

    schema_: Literal["external-boot-authority-teardown-response-v1"] = Field(
        "external-boot-authority-teardown-response-v1", alias="schema"
    )
    observation: AuthorityObservationV1
    proof: AuthorityTeardownProofV1
    journal_sequence: PositiveBigInt
    journal_digest: Digest

    @model_validator(mode="after")
    def _proof_is_the_observation(self) -> Self:
        if teardown_proof_digest(self.proof) != self.observation.composite_state:
            raise ValueError("teardown proof does not match observation composite state")
        complete = self.proof.disposition != "retained_quarantine"
        if complete != (self.observation.category == "absent"):
            raise ValueError("teardown disposition does not match observation category")
        return self


class JournalPhase(StrEnum):
    WATERMARK_INSTALLED = "watermark-installed"
    TAKEOVER_SUPERSEDED = "takeover-superseded"
    TAKEOVER_ACKNOWLEDGED = "takeover-acknowledged"
    ADMITTED = "admitted"
    MUTATION_STARTED = "mutation-started"
    PROVIDER_RETURNED = "provider-returned"
    OBSERVED = "observed"
    TERMINAL = "terminal"


_TAKEOVER_PHASES = frozenset(
    {
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_SUPERSEDED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    }
)


class JournalRecordV1(_AuthorityBinding):
    """One canonical append-only authority journal record."""

    sequence: PositiveBigInt
    previous_digest: Digest
    phase: JournalPhase
    attempt_id: UUID
    predecessor_generation: PositiveBigInt | None = None
    watermark_sequence: PositiveBigInt | None = None
    watermark_digest: Digest | None = None
    expected_source_identity: str | None = None
    intended_target_identity: str | None = None
    recovery_objects: Annotated[
        tuple[RecoveryObjectBindingV1, ...], Field(max_length=MAX_RECOVERY_OBJECTS)
    ] = ()
    observation: AuthorityObservationV1 | None = None
    outcome: Literal["never-began", "absent", "source", "target", "conflict"] | None = None

    @field_validator("expected_source_identity", "intended_target_identity")
    @classmethod
    def _optional_provider_identity_is_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _bounded_text(value, maximum=1024)

    _objects_are_canonical = field_validator("recovery_objects")(_canonical_recovery_objects)

    @model_validator(mode="after")
    def _phase_shape_is_closed(self) -> JournalRecordV1:
        has_mutation_fields = (
            self.expected_source_identity is not None
            or self.intended_target_identity is not None
            or bool(self.recovery_objects)
            or self.observation is not None
            or self.outcome is not None
        )
        if self.phase in _TAKEOVER_PHASES:
            if has_mutation_fields:
                raise ValueError("takeover records forbid mutation fields")
            if (self.phase is JournalPhase.TAKEOVER_SUPERSEDED) != (
                self.predecessor_generation is not None
            ):
                raise ValueError("predecessor generation must match takeover supersession")
            watermark_link = (self.watermark_sequence, self.watermark_digest)
            expected_link_presence = self.phase is not JournalPhase.WATERMARK_INSTALLED
            if any(value is not None for value in watermark_link) != expected_link_presence or (
                expected_link_presence and any(value is None for value in watermark_link)
            ):
                raise ValueError("takeover completion must carry the exact watermark link")
        else:
            if any(
                value is not None
                for value in (
                    self.predecessor_generation,
                    self.watermark_sequence,
                    self.watermark_digest,
                )
            ):
                raise ValueError("mutation records forbid takeover linkage")
            missing_identities = (
                self.expected_source_identity is None,
                self.intended_target_identity is None,
            )
            if any(missing_identities) and (
                missing_identities != (True, True)
                or self.purpose != "teardown"
                or self.operation is not AuthorityOperation.TEARDOWN
                or self.recovery_objects
            ):
                raise ValueError("mutation records require paired identities")
            if any(
                item.system_id != self.system_id or item.activation_id != self.activation_id
                for item in self.recovery_objects
            ):
                raise ValueError("recovery objects must belong to the record binding")
            if self.phase in {
                JournalPhase.ADMITTED,
                JournalPhase.MUTATION_STARTED,
                JournalPhase.PROVIDER_RETURNED,
            } and (self.observation is not None or self.outcome is not None):
                raise ValueError("pre-observation mutation phases forbid result evidence")
            if self.phase is JournalPhase.OBSERVED and (
                self.observation is None or self.outcome is not None
            ):
                raise ValueError("observed records require only observation evidence")
            if self.phase is JournalPhase.TERMINAL:
                if self.outcome is None:
                    raise ValueError("terminal records require an outcome")
                if (self.outcome == "never-began") != (self.observation is None):
                    raise ValueError("terminal observation must match the outcome")
        return self


def canonical_record_bytes(record: JournalRecordV1) -> bytes:
    """Return compact sorted UTF-8 JSON without a trailing newline."""
    return _canonical_bytes(record)


def record_digest(record: JournalRecordV1) -> str:
    """Return the SHA-256 identity of one canonical record."""
    return "sha256:" + hashlib.sha256(canonical_record_bytes(record)).hexdigest()


class AuthorityCommitContextV1(_ClosedValue):
    """Service-constructed proof of the anchored ``mutation-started`` record (ADR-0592).

    Carried across the ``AuthorityMutationAdapter`` seam so a provider adapter can tie its own
    commit to the exact authority journal record without reading the journal itself.

    Provenance differs per field, and the distinction is the point of the value:

    - ``journal_sequence`` and ``journal_digest`` are **service-owned**. They are the anchored
      record's own sequence and digest, computed here, and are unreachable from
      ``AuthorityMutationRequestV1`` — which carries no journal field and is closed — so a
      peer cannot assert a journal position it did not cause.
    - ``commit_point`` and ``phase`` are **pinned**: the phase to a single literal, and the
      commit point to the operation the anchored record carries.
    - ``operation_identity`` and ``attempt_id`` are **peer-sent values that round-trip through
      the anchored record**. ``_binding_matches`` requires the identity to equal the trusted
      binding before the record is anchored, so it is constrained; ``attempt_id`` is
      peer-chosen and carried, not verified. Neither is an authenticity token, and a
      downstream proof must not treat them as one.
    """

    schema_: Literal["external-boot-authority-v1"] = Field(
        "external-boot-authority-v1", alias="schema"
    )
    commit_point: AuthorityOperation
    operation_identity: str
    attempt_id: UUID
    journal_sequence: PositiveBigInt
    journal_digest: Digest
    phase: Literal[JournalPhase.MUTATION_STARTED] = JournalPhase.MUTATION_STARTED

    @field_validator("operation_identity")
    @classmethod
    def _identity_is_bounded(cls, value: str) -> str:
        return _bounded_text(value)

    @classmethod
    def for_record(cls, record: JournalRecordV1) -> AuthorityCommitContextV1:
        """Build the context for one anchored record, refusing every other phase.

        The phase gate is what makes a downstream proof's ``mutation-started`` claim proven
        rather than assumed: a ``provider-returned`` or ``observed`` record describes a
        mutation that already reached the provider, and cannot authorize one that has not.
        """
        if record.phase is not JournalPhase.MUTATION_STARTED:
            raise ValueError("commit context requires an anchored mutation-started record")
        return cls(
            commit_point=record.operation,
            operation_identity=record.operation_identity,
            attempt_id=record.attempt_id,
            journal_sequence=record.sequence,
            journal_digest=record_digest(record),
        )


class AuthorityCleanupEvidenceContextV1(_ClosedValue):
    """Trusted short-transaction result authorizing one exact remote cleanup."""

    schema_: Literal["external-boot-authority-cleanup-evidence-v1"] = Field(
        "external-boot-authority-cleanup-evidence-v1", alias="schema"
    )
    operation_identity: str
    attempt_id: UUID
    operation_nonce: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    cleanup_state: Literal["open", "discharged"]
    recovery_reference_json: Annotated[str, Field(min_length=2, max_length=65_536)]

    @field_validator("operation_identity")
    @classmethod
    def _operation_is_bounded(cls, value: str) -> str:
        return _bounded_text(value)


class AuthorityRecoveryObservationContextV1(_ClosedValue):
    """Service proof that a teardown observation recovers an anchored mutation."""

    schema_: Literal["external-boot-authority-v1"] = Field(
        "external-boot-authority-v1", alias="schema"
    )
    commit_point: Literal[AuthorityOperation.TEARDOWN]
    operation_identity: str
    attempt_id: UUID
    journal_sequence: PositiveBigInt
    journal_digest: Digest
    phase: Literal[JournalPhase.MUTATION_STARTED, JournalPhase.PROVIDER_RETURNED]

    @field_validator("operation_identity")
    @classmethod
    def _identity_is_bounded(cls, value: str) -> str:
        return _bounded_text(value)

    @classmethod
    def for_record(cls, record: JournalRecordV1) -> AuthorityRecoveryObservationContextV1:
        if record.operation is not AuthorityOperation.TEARDOWN or record.phase not in {
            JournalPhase.MUTATION_STARTED,
            JournalPhase.PROVIDER_RETURNED,
        }:
            raise ValueError("recovery observation context requires an anchored teardown record")
        return cls(
            commit_point=AuthorityOperation.TEARDOWN,
            operation_identity=record.operation_identity,
            attempt_id=record.attempt_id,
            journal_sequence=record.sequence,
            journal_digest=record_digest(record),
            phase=record.phase,
        )
