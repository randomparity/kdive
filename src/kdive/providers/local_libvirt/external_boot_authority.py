"""Local-libvirt binding of the provider-host external-boot authority (ADR-0584).

Maps the authority's ``observe``/``commit`` lanes onto the local six-port coordinator
``LocalLibvirtExternalBoot`` and, through it, onto the named local external-boot commit
points in ``lifecycle/boot/external_boot.py``. No generic libvirt power operation
(``create``, ``destroy``, ``reset``, ``shutdown``) and no synthesized domain-XML identity
appears here: identity is always a recorded boot-identity digest read back from the
provider, never XML this module composed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from uuid import UUID, uuid5

from kdive.providers.external_boot_authority.protocol import (
    AuthorityCommitContextV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    ObservationCategory,
    operation_is_permitted,
)
from kdive.providers.external_boot_authority.service import AuthorityServiceError
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    FinalizeCleanupProof,
    LocalLibvirtExternalBoot,
    LocalObservedState,
)
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    OpaqueProviderRef,
    ProviderStateIdentity,
    RecoveryPoint,
)

logger = logging.getLogger(__name__)

# Namespace for deriving a stable observation id from the observed facts, so the same
# observation of the same state is the same id across a retry or an authority restart.
_OBSERVATION_NAMESPACE = UUID("6f3f0f6e-7a1a-4e3b-9a2f-2b6b1d4c8e57")

# Bound on the per-activation admission watermarks retained in process. The journal is the
# authoritative watermark; dropping the oldest entry here can only make this check
# under-reject, which the service's own ``resolve_current`` still catches.
_MAX_ADMITTED_LANES = 256

# Commit points that reach a provider mutation. Every other legal operation is a
# bookkeeping edge whose provider effect is exactly one observation: ADR-0584 makes
# conflict resolution, release, and teardown each allocate their own later generation
# before mutating, and names "no provider mutation began" as a terminal condition.
_MUTATING_OPERATIONS = frozenset(
    {
        AuthorityOperation.ACTIVATE,
        AuthorityOperation.RECOVER,
        AuthorityOperation.RECOVERY_ATTEMPT,
        AuthorityOperation.CLEANUP,
        AuthorityOperation.TEARDOWN,
    }
)

# Commit points that destroy recovery evidence. These must positively name the recovery
# object they destroy, not merely fail to name a foreign one.
_DELETING_OPERATIONS = frozenset({AuthorityOperation.CLEANUP, AuthorityOperation.TEARDOWN})


def _authority_ref(request: AuthorityMutationRequestV1) -> OpaqueProviderRef:
    """Derive the opaque provider reference for a request's authority binding.

    Built only from closed identity fields the request already carries. Nothing a peer
    supplies can steer it toward a path, command, or credential: ``OpaqueProviderRef``
    rejects a leading separator, ``.``/``..`` segments, and ``\\``, ``:`` and ``@``.
    """
    return OpaqueProviderRef(
        ref=f"authority/{request.authority_id}/{request.generation}/{request.attempt_id}"
    )


def _activation_binding(
    request: AuthorityMutationRequestV1,
) -> ExternalBootActivationBinding:
    return ExternalBootActivationBinding(
        system_id=str(request.system_id),
        run_id=str(request.run_id),
        activation_id=str(request.activation_id),
    )


def _identity_of(state: ProviderStateIdentity) -> str:
    return state.definition


class LocalExternalBootAuthorityAdapter:
    """``AuthorityMutationAdapter`` over the local external-boot coordinator.

    Construction takes only the coordinator. No constructor argument and no protocol
    field can select a libvirt URI, filesystem path, command, XML document, or
    credential: the URI is resolved once by
    ``local_libvirt.composition.build_external_boot_session_factory`` through
    ``config.require(LIBVIRT_URI)``, and every host resource this adapter reaches is
    already bound inside the injected coordinator.
    """

    def __init__(self, ports: LocalLibvirtExternalBoot) -> None:
        self._ports = ports
        # Highest generation this adapter has admitted per activation lane. It complements
        # the journal watermark the service enforces through ``resolve_current``; it does
        # not replace it, and it is deliberately in-process because a restarted adapter
        # must re-derive admission from the journal rather than trust its own memory.
        self._admitted: dict[tuple[str, str], int] = {}
        self._pending_cleanup_finalization: dict[str, RecoveryPoint] = {}

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        """Classify observed provider state against the request's exact identities."""
        return await asyncio.to_thread(self._observe, request)

    async def commit(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityObservationV1:
        """Apply one named commit point, then report the resulting observation."""
        operation = self._require_permitted_commit_point(request, context)
        self._require_admissible_generation(request)
        return await asyncio.to_thread(self._commit, request, operation, context)

    async def finalize(
        self, request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> None:
        """Remove a cleanup tombstone only after the authority anchored its terminal receipt."""
        if request.operation is not AuthorityOperation.CLEANUP:
            return
        authority = _authority_ref(request)
        point = self._pending_cleanup_finalization.pop(request.operation_identity, None)
        if point is None:
            # A restarted authority keeps the durable tombstone as its accounted-absence
            # receipt. Core convergence does not depend on deleting that receipt.
            return
        await asyncio.to_thread(
            self._ports.finalize_cleanup_tombstone,
            point,
            _cleanup_proof(context, point),
            authority,
        )

    @staticmethod
    def _require_permitted_commit_point(
        request: AuthorityMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthorityOperation:
        """Refuse an illegal commit point before any provider call.

        ``context.commit_point`` is an ``AuthorityOperation``, so the model layer guarantees
        the member itself. Two cross-model facts it does not guarantee must still hold: the
        operation is legal for the request's purpose, and it is the same operation the request
        carries. Without the second, a request could journal one operation while driving
        the provider through another, and the journal record — the evidence ADR-0584 makes
        authoritative for what mutation may have happened — would name the wrong one.
        """
        operation = context.commit_point
        if not operation_is_permitted(request.purpose, operation):
            raise AuthorityServiceError("provider_conflict")
        if operation is not request.operation:
            raise AuthorityServiceError("provider_conflict")
        return operation

    def _require_admissible_generation(self, request: AuthorityMutationRequestV1) -> None:
        lane = (str(request.system_id), str(request.activation_id))
        admitted = self._admitted.get(lane)
        if admitted is not None and request.generation < admitted:
            raise AuthorityServiceError("superseded")
        if lane not in self._admitted and len(self._admitted) >= _MAX_ADMITTED_LANES:
            # Insertion-ordered, so this drops the least recently first-seen lane.
            del self._admitted[next(iter(self._admitted))]
        self._admitted[lane] = max(admitted or 0, request.generation)

    def _commit(
        self,
        request: AuthorityMutationRequestV1,
        operation: AuthorityOperation,
        context: AuthorityCommitContextV1,
    ) -> AuthorityObservationV1:
        binding = _activation_binding(request)
        authority = _authority_ref(request)
        point = self._resolve_point(
            binding, authority, allow_cleanup_receipt=operation is AuthorityOperation.CLEANUP
        )
        if point is None and request.operation is AuthorityOperation.CLEANUP:
            point = self._pending_cleanup_finalization.get(request.operation_identity)
        matched = self._require_matching_identities(request, point)
        if not _ownership_is_proven(
            request, matched, require_named=operation in _DELETING_OPERATIONS
        ):
            # Quarantine: the objects are neither reused for a mutation nor deleted, and
            # the state is reported as the conflict ADR-0584 calls an unowned observation.
            raise AuthorityServiceError("provider_conflict")
        if operation in _MUTATING_OPERATIONS:
            self._apply(operation, matched, authority, context)
        return self._observation(request, binding, authority, matched)

    def _apply(
        self,
        operation: AuthorityOperation,
        point: RecoveryPoint,
        authority: OpaqueProviderRef,
        context: AuthorityCommitContextV1,
    ) -> None:
        """Drive the named local commit points for one mutating operation.

        Each coordinator call is phase-resumable: it re-reads the durable recovery phase
        and returns early on work already done. A commit interrupted after the provider
        mutation but before acknowledgement therefore stays re-observable and does not
        double-apply when the authority retries it.
        """
        try:
            if operation is AuthorityOperation.ACTIVATE:
                self._ports.activate(point, authority)
            elif operation in {
                AuthorityOperation.RECOVER,
                AuthorityOperation.RECOVERY_ATTEMPT,
            }:
                self._ports.recover(point, authority)
            elif operation is AuthorityOperation.CLEANUP:
                if not self._ports.cleanup_is_accounted(point, authority):
                    self._pending_cleanup_finalization[context.operation_identity] = point
                self._ports.cleanup(point, authority)
            elif operation is AuthorityOperation.TEARDOWN:
                # Teardown publishes a tombstone through the same primitive and still has no
                # finalizer. That gap is recorded in ADR-0592 and routed to #2212; it is not
                # this seam's to close.
                self._ports.cleanup(point, authority)
            else:
                # An operation added to _MUTATING_OPERATIONS without a mapping here must
                # not fall through to whichever branch happens to be last.
                raise AuthorityServiceError("provider_conflict")
        except AuthorityServiceError:
            raise
        except Exception:  # noqa: BLE001 - bound provider failure to a closed category
            logger.exception("external-boot provider commit failed", extra={"operation": operation})
            raise AuthorityServiceError("provider_conflict") from None

    def _observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        binding = _activation_binding(request)
        authority = _authority_ref(request)
        point = self._resolve_point(
            binding,
            authority,
            allow_cleanup_receipt=request.operation is AuthorityOperation.CLEANUP,
        )
        if point is None and request.operation is AuthorityOperation.CLEANUP:
            point = self._pending_cleanup_finalization.get(request.operation_identity)
        if (
            request.operation is AuthorityOperation.CLEANUP
            and point is not None
            and self._ports.cleanup_is_accounted(point, authority)
        ):
            observed = self._read_state(binding, authority)
            composite_state = self._composite_state(request, observed)
            return AuthorityObservationV1(
                observation_id=self._observation_id(request, composite_state, "absent"),
                category="absent",
                composite_state=composite_state,
            )
        return self._observation(request, binding, authority, point)

    def _resolve_point(
        self,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
        *,
        allow_cleanup_receipt: bool,
    ) -> RecoveryPoint | None:
        try:
            return self._ports.recovery_point(binding, authority)
        except Exception:  # noqa: BLE001 - an unresolvable point is an unreadable state
            # Logged in full inside the authority, where the diagnostic is allowed to
            # exist; only the bounded category ever crosses the boundary.
            logger.exception("external-boot recovery point is unresolvable")
        if not allow_cleanup_receipt:
            return None
        try:
            return self._ports.cleanup_receipt(binding, authority)
        except Exception:  # noqa: BLE001 - malformed or unreadable receipt fails closed
            logger.exception("external-boot cleanup receipt is unresolvable")
            return None

    @staticmethod
    def _require_matching_identities(
        request: AuthorityMutationRequestV1, point: RecoveryPoint | None
    ) -> RecoveryPoint:
        """Return the point only when its recorded identities are exactly as requested.

        A mismatch is never a silent overwrite: it is a bounded ``provider_conflict``
        carrying no provider output and no observed value.
        """
        if point is None:
            raise AuthorityServiceError("provider_conflict")
        if (
            request.plan_identity != point.plan_identity
            or request.expected_source_identity != _identity_of(point.source_state)
            or request.intended_target_identity != _identity_of(point.target_state)
        ):
            raise AuthorityServiceError("provider_conflict")
        return point

    def _observation(
        self,
        request: AuthorityMutationRequestV1,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
        point: RecoveryPoint | None,
    ) -> AuthorityObservationV1:
        observed = self._read_state(binding, authority)
        category = self._categorize(request, point, observed)
        composite_state = self._composite_state(request, observed)
        return AuthorityObservationV1(
            observation_id=self._observation_id(request, composite_state, category),
            category=category,
            composite_state=composite_state,
        )

    def _read_state(
        self, binding: ExternalBootActivationBinding, authority: OpaqueProviderRef
    ) -> LocalObservedState:
        try:
            return self._ports.observe_state(binding, authority)
        except Exception:  # noqa: BLE001 - a failed read is the unreadable classification
            logger.exception("external-boot provider state is unreadable")
            return LocalObservedState(definition=None, modules=None, active=None)

    @staticmethod
    def _categorize(
        request: AuthorityMutationRequestV1,
        point: RecoveryPoint | None,
        observed: LocalObservedState,
    ) -> ObservationCategory:
        """Derive the observation category from a real read of source and target state.

        Both halves of the provider state are classified independently — the domain
        definition's recorded boot identity and the live module tree — and the pair
        decides the category. Requested identities are compared exactly; anything that
        matches neither recorded side is a conflict, never an assumed success.
        """
        if observed.definition is None or observed.modules is None or point is None:
            return "unreadable"
        if (
            request.expected_source_identity != _identity_of(point.source_state)
            or request.intended_target_identity != _identity_of(point.target_state)
            or not _ownership_is_proven(request, point)
        ):
            return "conflict"
        definition = _side_of(
            observed.definition,
            _identity_of(point.source_state),
            _identity_of(point.target_state),
        )
        modules = _side_of(
            observed.modules.model_dump_json(),
            point.source_state.modules.model_dump_json(),
            point.target_state.modules.model_dump_json(),
        )
        if definition is None or modules is None:
            return "conflict"
        if definition == modules:
            return definition
        return "mixed"

    @staticmethod
    def _composite_state(request: AuthorityMutationRequestV1, observed: LocalObservedState) -> str:
        """Digest the observed state together with the activation it was observed on.

        The digest is a proof-of-observation token: ``recover_from_conflict`` refuses a
        conflict recovery unless the acknowledged value equals the recorded conflict
        evidence, so it has to identify *which* activation was observed, not only what was
        seen. Observed content alone is not sufficient to do that — a source-state domain
        contributes a boot identity derived solely from the ``<os>`` kernel, initrd and
        cmdline, which are all absent on a plain domain and therefore constant fleet-wide,
        and an unreadable observation contributes nothing at all. Binding System,
        activation, Run and plan keeps two identically provisioned systems from minting
        interchangeable tokens.

        Changing any observed value, any requested identity, or any owner identity changes
        the digest. It carries no provider message, path, URI, or secret — only closed
        identifiers and digests already recorded for this activation.
        """
        payload = json.dumps(
            {
                "system_id": str(request.system_id),
                "activation_id": str(request.activation_id),
                "run_id": str(request.run_id),
                "plan_identity": request.plan_identity,
                "definition": observed.definition,
                "modules": (
                    None if observed.modules is None else observed.modules.model_dump(mode="json")
                ),
                "active": observed.active,
                "expected_source_identity": request.expected_source_identity,
                "intended_target_identity": request.intended_target_identity,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return (
            "sha256:"
            + hashlib.sha256(b"kdive-local-external-boot-composite-v1\0" + payload).hexdigest()
        )

    @staticmethod
    def _observation_id(
        request: AuthorityMutationRequestV1,
        composite_state: str,
        category: ObservationCategory,
    ) -> UUID:
        """Identify an observation by everything that distinguishes it.

        Keyed on the composite digest rather than any single observed field, so two
        observations that differ in the module tree alone — or in which half was
        unreadable — cannot collide on one id.
        """
        return uuid5(
            _OBSERVATION_NAMESPACE,
            "\0".join(
                (
                    str(request.attempt_id),
                    str(request.generation),
                    request.operation.value,
                    category,
                    composite_state,
                )
            ),
        )


def _cleanup_proof(context: AuthorityCommitContextV1, point: RecoveryPoint) -> FinalizeCleanupProof:
    """Tie this cleanup to the exact authority record the service anchored for it.

    No field is defaulted — ``phase`` included, which is why ``FinalizeCleanupProof`` no
    longer defaults it. Provenance is not uniform, though, and the store treats these fields
    accordingly: ``point_digest`` and ``binding`` come from the recovery point this adapter
    resolved and are what ``finalize_tombstone`` actually compares; ``journal_sequence`` and
    ``journal_digest`` are the authority's own, and are what make the caller unable to forge a
    finalization; ``operation_id`` and ``attempt_id`` are peer-sent values carried through the
    anchored record, so they identify the operation but authenticate nothing.
    """
    return FinalizeCleanupProof(
        point_digest=LocalLibvirtExternalBoot.point_digest(point),
        binding=point.binding,
        operation_id=context.operation_identity,
        attempt_id=str(context.attempt_id),
        journal_sequence=context.journal_sequence,
        journal_digest=context.journal_digest,
        phase=context.phase.value,
    )


def _ownership_is_proven(
    request: AuthorityMutationRequestV1,
    point: RecoveryPoint | None,
    *,
    require_named: bool = False,
) -> bool:
    """Check every named recovery object against the one this activation owns.

    Local recovery ownership is the stable ``(System, activation, recovery reference)``
    triple ADR-0584 requires takeover to preserve. An object naming any other reference
    cannot be proven to belong here, so it is quarantined rather than resumed or deleted.

    ``require_named`` additionally demands that the activation's own recovery reference be
    among those named. ADR-0584 permits deleting a recovery object "only when the journal
    and provider observation prove that stable binding", so an operation that destroys
    recovery evidence must name what it destroys. Without it the check is purely negative
    and an empty set stands in for a proof.

    What this does and does not establish, stated precisely because the reference arm reads
    stronger than it is: ``point.recovery_ref`` is derived from the binding the peer
    supplied, and ``protocol.py`` already binds each object's System and activation to the
    request, so comparing the reference cannot by itself distinguish an owned object from a
    fabricated one. The load-bearing part is ``point``: it exists only because a durable
    recovery record was read back from the owner-derived path under an authenticated lease.
    An empty set on a non-deleting operation asserts nothing and so disproves nothing;
    that is why deletion is the case that must name its object.
    """
    if point is None:
        return False
    if require_named and point.recovery_ref.ref not in {
        item.reference for item in request.recovery_objects
    }:
        return False
    return all(
        item.reference == point.recovery_ref.ref
        and str(item.system_id) == point.binding.system_id
        and str(item.activation_id) == point.binding.activation_id
        for item in request.recovery_objects
    )


def _side_of(observed: str, source: str, target: str) -> ObservationCategory | None:
    """Name which recorded side an observed value is, or ``None`` when it is neither."""
    if observed == source:
        return "source"
    if observed == target:
        return "target"
    return None
