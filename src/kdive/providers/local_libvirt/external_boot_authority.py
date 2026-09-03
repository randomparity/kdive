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
from uuid import UUID, uuid5

from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityOperation,
    ObservationCategory,
    operation_is_permitted,
)
from kdive.providers.external_boot_authority.service import AuthorityServiceError
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    LocalLibvirtExternalBoot,
    LocalObservedState,
)
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    OpaqueProviderRef,
    ProviderStateIdentity,
    RecoveryPoint,
)

# Namespace for deriving a stable observation id from the observed facts, so the same
# observation of the same state is the same id across a retry or an authority restart.
_OBSERVATION_NAMESPACE = UUID("6f3f0f6e-7a1a-4e3b-9a2f-2b6b1d4c8e57")

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

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        """Classify observed provider state against the request's exact identities."""
        return await asyncio.to_thread(self._observe, request)

    async def commit(
        self, request: AuthorityMutationRequestV1, commit_point: str
    ) -> AuthorityObservationV1:
        """Apply one named commit point, then report the resulting observation."""
        operation = self._require_permitted_commit_point(request, commit_point)
        self._require_admissible_generation(request)
        return await asyncio.to_thread(self._commit, request, operation)

    @staticmethod
    def _require_permitted_commit_point(
        request: AuthorityMutationRequestV1, commit_point: str
    ) -> AuthorityOperation:
        """Refuse an illegal commit point before any provider call.

        ``commit_point`` crosses the adapter seam as a bare ``str``, so
        ``AuthorityMutationRequestV1``'s own purpose/operation validator does not cover it
        and this check cannot lean on the model layer.
        """
        try:
            operation = AuthorityOperation(commit_point)
        except ValueError:
            raise AuthorityServiceError("provider_conflict") from None
        if not operation_is_permitted(request.purpose, operation):
            raise AuthorityServiceError("provider_conflict")
        return operation

    def _require_admissible_generation(self, request: AuthorityMutationRequestV1) -> None:
        lane = (str(request.system_id), str(request.activation_id))
        admitted = self._admitted.get(lane)
        if admitted is not None and request.generation < admitted:
            raise AuthorityServiceError("superseded")
        self._admitted[lane] = max(admitted or 0, request.generation)

    def _commit(
        self, request: AuthorityMutationRequestV1, operation: AuthorityOperation
    ) -> AuthorityObservationV1:
        binding = _activation_binding(request)
        authority = _authority_ref(request)
        point = self._resolve_point(binding, authority)
        matched = self._require_matching_identities(request, point)
        if not _ownership_is_proven(request, matched):
            # Quarantine: the objects are neither reused for a mutation nor deleted, and
            # the state is reported as the conflict ADR-0584 calls an unowned observation.
            raise AuthorityServiceError("provider_conflict")
        if operation in _MUTATING_OPERATIONS:
            self._apply(operation, matched, authority)
        return self._observation(request, binding, authority, matched)

    def _apply(
        self,
        operation: AuthorityOperation,
        point: RecoveryPoint,
        authority: OpaqueProviderRef,
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
            else:
                self._ports.cleanup(point, authority)
        except AuthorityServiceError:
            raise
        except Exception:  # noqa: BLE001 - bound provider failure to a closed category
            raise AuthorityServiceError("provider_conflict") from None

    def _observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        binding = _activation_binding(request)
        authority = _authority_ref(request)
        point = self._resolve_point(binding, authority)
        return self._observation(request, binding, authority, point)

    def _resolve_point(
        self, binding: ExternalBootActivationBinding, authority: OpaqueProviderRef
    ) -> RecoveryPoint | None:
        try:
            return self._ports.recovery_point(binding, authority)
        except Exception:  # noqa: BLE001 - an unresolvable point is an unreadable state
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
        return AuthorityObservationV1(
            observation_id=self._observation_id(request, observed, category),
            category=category,
            composite_state=self._composite_state(request, observed),
        )

    def _read_state(
        self, binding: ExternalBootActivationBinding, authority: OpaqueProviderRef
    ) -> LocalObservedState:
        try:
            return self._ports.observe_state(binding, authority)
        except Exception:  # noqa: BLE001 - a failed read is the unreadable classification
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
        """Digest the observed pair together with the requested identities.

        Changing either observed identity, or either requested identity, changes the
        digest. It carries no provider message, path, URI, or secret — only digests and
        closed component states already recorded for this activation.
        """
        payload = json.dumps(
            {
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
        observed: LocalObservedState,
        category: ObservationCategory,
    ) -> UUID:
        return uuid5(
            _OBSERVATION_NAMESPACE,
            "\0".join(
                (
                    str(request.attempt_id),
                    str(request.generation),
                    request.operation.value,
                    category,
                    observed.definition or "",
                )
            ),
        )


def _ownership_is_proven(request: AuthorityMutationRequestV1, point: RecoveryPoint | None) -> bool:
    """Prove every named recovery object is the one this activation actually owns.

    Local recovery ownership is the stable ``(System, activation, recovery reference)``
    triple ADR-0584 requires takeover to preserve. An object naming any other reference
    cannot be proven to belong here, so it is quarantined rather than resumed or deleted.
    """
    if point is None:
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
