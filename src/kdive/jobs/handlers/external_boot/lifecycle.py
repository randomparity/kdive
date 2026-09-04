"""The six enqueueable external-boot operation handlers (spec §7).

Each is the shared runner parameterized by its row of the specification's §7 table: the activation
states the **commit** admits, the evidence columns it reads, its footnoted prerequisites, its port
call, and its result variant. Nothing here calls ``commit_external_boot_authority_result``; the
handler returns its result and the worker commits it under ``_authority_binding_matches``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final, cast
from uuid import NAMESPACE_URL, uuid5

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.capacity.state import ExternalBootActivationState as State
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.external_boot.evidence import (
    authority_result,
    evidence_digest,
    terminal_evidence,
)
from kdive.jobs.handlers.external_boot.operations import ExternalBootOperationHandler
from kdive.jobs.handlers.external_boot.ports import (
    ExternalBootAuthorityExecutor,
    ExternalBootHandlerPorts,
)
from kdive.jobs.handlers.external_boot.runner import (
    OperationContext,
    _CommandLineMismatch,
    authority_ref,
    run_operation,
)
from kdive.jobs.models import (
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthoritySuccessV1,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    RecoveryObjectBindingV1,
)
from kdive.providers.ports.external_boot import RecoveryPoint, RunningKernelObservation

__all__ = [
    "activate_handler",
    "cleanup_handler",
    "recover_handler",
    "release_handler",
    "resolve_conflict_handler",
    "teardown_handler",
]

_ACTIVATION_EVIDENCE: Final = frozenset({"materialization", "recovery_point"})
_RECOVERY_STATES: Final = frozenset(
    {State.ACTIVE, State.RECOVERED, State.ABANDONED, State.RECOVERY_CONFLICT, State.RECOVERY_FAILED}
)
_CLEANUP_STATES: Final = frozenset(
    {State.RECOVERED, State.ABANDONED, State.RECOVERY_CONFLICT, State.RECOVERY_FAILED}
)
_TEARDOWN_STATES: Final = frozenset({State.RECOVERY_CONFLICT, State.RECOVERY_FAILED})
_ORDINARY_CLEANUP_STATES: Final = frozenset({State.RECOVERED, State.ABANDONED})


def _refuse(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFIGURATION_ERROR, terminal=True)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _recovery(context: OperationContext) -> RecoveryPoint:
    """The persisted recovery point, guaranteed present by ``require_activation_evidence``."""
    recovery = context.activation.recovery_point
    if recovery is None:  # pragma: no cover - the evidence check refuses before the port is called
        raise _refuse(f"activation {context.marker.activation_id} has no recovery_point")
    return recovery


def _require_category(
    context: OperationContext, observation: AuthorityObservationV1, expected: str
) -> None:
    """The ``observe`` call is a post-mutation liveness precondition, and this is its whole point.

    Nothing in ``ExternalBootTerminalEvidenceV1`` consumes the observation — ``composite_state`` is
    the acknowledgement's digest, not one derived from it. The call exists so the operation can
    refuse to emit terminal evidence when the running kernel is not the one the activation's
    persisted ``materialization.kernel_observation`` records. The value is discarded after this
    comparison.
    """
    if observation.category != expected:
        raise _refuse(
            f"authority observed {observation.category!r} for {context.marker.operation!r}; "
            f"expected {expected!r}"
        )


def _require_observed_kernel_matches(
    context: OperationContext, observation: RunningKernelObservation | None
) -> None:
    materialization = context.activation.materialization
    if observation is None or materialization is None:
        raise _refuse(
            f"activation {context.marker.activation_id} produced no kernel observation to verify"
        )
    if observation.identity != materialization.kernel_observation:
        raise _refuse(
            f"the running kernel observed for activation {context.marker.activation_id} is not the "
            "one its persisted materialization records"
        )
    if observation.cmdline != observation.expected_cmdline:
        limit = min(len(observation.cmdline), len(observation.expected_cmdline))
        offset = next(
            (
                index
                for index in range(limit)
                if observation.cmdline[index] != observation.expected_cmdline[index]
            ),
            limit,
        )
        raise _CommandLineMismatch(
            observation.expected_cmdline[:2048], observation.cmdline[:2048], offset
        )


def _mutation_request(context: OperationContext) -> AuthorityMutationRequestV1:
    recovery = _recovery(context)
    attempt_id = context.activation.current_attempt_id or uuid5(
        NAMESPACE_URL, f"kdive/external-boot/{context.marker.operation_identity}"
    )
    objects = ()
    if context.marker.operation in {"cleanup", "teardown"}:
        objects = (
            RecoveryObjectBindingV1(
                system_id=context.marker.system_id,
                activation_id=context.marker.activation_id,
                reference=recovery.recovery_ref.ref,
            ),
        )
    return AuthorityMutationRequestV1.model_validate(
        {
            "authority_id": context.authority.authority_id,
            "generation": context.authority.generation,
            "system_id": context.marker.system_id,
            "activation_id": context.marker.activation_id,
            "run_id": context.marker.run_id,
            "plan_identity": context.marker.plan_identity,
            "purpose": context.marker.purpose,
            "operation": context.marker.operation,
            "provider_kind": context.marker.provider_kind,
            "authority_instance": context.marker.authority_instance,
            "operation_identity": context.marker.operation_identity,
            "operation_digest": context.authority.operation_digest,
            "attempt_id": attempt_id,
            "expected_source_identity": recovery.source_state.definition,
            "intended_target_identity": recovery.target_state.definition,
            "recovery_objects": objects,
        }
    )


async def _execute(
    context: OperationContext,
) -> tuple[AuthorityObservationV1, RunningKernelObservation | None]:
    executor = context.prerequisites.get("authority_executor")
    if executor is None:
        raise _refuse("no external-boot authority executor is configured")
    authority_observation = await cast(ExternalBootAuthorityExecutor, executor).execute(
        _mutation_request(context)
    )
    kernel_observation = None
    if authority_observation.category == "target":
        kernel_observation = context.port.observe(_recovery(context), authority_ref(context))
    return authority_observation, kernel_observation


async def _no_preconditions(
    _conn: AsyncConnection,
    _activation: ExternalBootActivation,
    _marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    return {}


async def _attempt_state(
    conn: AsyncConnection, activation: ExternalBootActivation
) -> tuple[str, datetime | None] | None:
    if activation.current_attempt_id is None:
        return None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT state, recovery_readiness_deadline "
            "FROM external_boot_recovery_attempts WHERE attempt_id = %s",
            (activation.current_attempt_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return str(row["state"]), row["recovery_readiness_deadline"]


def _require_attempt_state(expected: str) -> Callable[..., Awaitable[Mapping[str, Any]]]:
    """Footnote †: the current recovery-attempt row must already be in ``expected``.

    **Nothing in this change creates or advances that row.** It is written by the
    ``recovery-attempt`` operation, which ADR-0593 decision 4 excludes as #2202's. So in production
    the ``recover`` and ``resolve-conflict`` handlers are registered and reachable but cannot reach
    an applied commit until #2202 supplies the attempt row; their tests seed it directly. This is
    stated rather than worked around.
    """

    async def check(
        conn: AsyncConnection,
        activation: ExternalBootActivation,
        marker: ExternalBootAuthorityMarkerV1,
    ) -> Mapping[str, Any]:
        attempt = await _attempt_state(conn, activation)
        state = None if attempt is None else attempt[0]
        if state != expected:
            raise _refuse(
                f"{marker.operation!r} requires the current recovery attempt in {expected!r}, "
                f"not {state!r}"
            )
        return {"attempt_deadline": attempt[1] if attempt is not None else None}

    return check


async def _reservation(conn: AsyncConnection, activation_id: Any) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT store_identity, owner_key, reserved_bytes FROM external_boot_reservations "
            "WHERE activation_id = %s AND state = 'ready'",
            (activation_id,),
        )
        row = await cur.fetchone()
    return None if row is None else dict(row)


async def _release_row(conn: AsyncConnection, activation_id: Any) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT release_identity FROM external_boot_reservation_releases "
            "WHERE activation_id = %s",
            (activation_id,),
        )
        row = await cur.fetchone()
    return None if row is None else dict(row)


async def _require_releasable(
    conn: AsyncConnection,
    activation: ExternalBootActivation,
    marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    """Footnote ‡ for ``release``: a ready reservation, and no release recorded yet."""
    if await _release_row(conn, activation.id) is not None:
        raise _refuse(f"activation {marker.activation_id} already has a recorded release")
    reservation = await _reservation(conn, activation.id)
    if reservation is None:
        raise _refuse(f"activation {marker.activation_id} has no ready recovery-store reservation")
    return {"reservation": reservation}


async def _require_cleanable(
    conn: AsyncConnection,
    activation: ExternalBootActivation,
    marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    """Footnote ‡ for ``cleanup``/``teardown``: not already cleaned, and a release to name."""
    if activation.cleanup_complete:
        raise _refuse(f"activation {marker.activation_id} is already cleanup_complete")
    release = await _release_row(conn, activation.id)
    if release is None:
        raise _refuse(
            f"{marker.operation!r} requires a recorded release for activation "
            f"{marker.activation_id}"
        )
    return {"release_identity": release["release_identity"]}


async def _require_torn_down_system(
    conn: AsyncConnection,
    activation: ExternalBootActivation,
    marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    """Footnote § : ``teardown`` additionally requires ``systems.state = 'failed'``."""
    cleanable = await _require_cleanable(conn, activation, marker)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (activation.system_id,))
        row = await cur.fetchone()
    if row is None or row["state"] != "failed":
        raise _refuse(
            f"teardown requires system {activation.system_id} in 'failed', "
            f"not {row and row['state']!r}"
        )
    return cleanable


def _handler(
    ports: ExternalBootHandlerPorts,
    *,
    require_activation_state: frozenset[State],
    require_activation_evidence: frozenset[str],
    require_preconditions: Callable[..., Awaitable[Mapping[str, Any]]],
    expected_observation: str,
    build_result: Callable[
        [OperationContext, AuthorityObservationV1], ExternalBootAuthoritySuccessV1
    ],
    before_port: Callable[[OperationContext], ExternalBootAuthoritySuccessV1 | None] | None = None,
) -> ExternalBootOperationHandler:
    async def handler(
        conn: AsyncConnection, job: Job, marker: ExternalBootAuthorityMarkerV1
    ) -> ExternalBootAuthoritySuccessV1:
        def checked_build(
            context: OperationContext,
            observations: tuple[AuthorityObservationV1, RunningKernelObservation | None],
        ) -> ExternalBootAuthoritySuccessV1:
            authority_observation, kernel_observation = observations
            _require_category(context, authority_observation, expected_observation)
            if expected_observation == "target":
                _require_observed_kernel_matches(context, kernel_observation)
            return build_result(context, authority_observation)

        return await run_operation(
            conn,
            job,
            marker,
            ports=ports,
            require_activation_state=require_activation_state,
            require_activation_evidence=require_activation_evidence,
            require_preconditions=lambda conn, activation, marker: _with_executor(
                require_preconditions, ports, conn, activation, marker
            ),
            call_port=_execute,
            build_result=checked_build,
            before_port=before_port,
        )

    return handler


async def _with_executor(
    preconditions: Callable[..., Awaitable[Mapping[str, Any]]],
    ports: ExternalBootHandlerPorts,
    conn: AsyncConnection,
    activation: ExternalBootActivation,
    marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    values = dict(await preconditions(conn, activation, marker))
    values["authority_executor"] = ports.authority_executor
    return values


def activate_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Activate the prepared external boot, then confirm the running kernel is the expected one.

    The admitted state is ``activating`` alone, taken from the **commit** preconditions
    (``0122…sql:1302-1306``) rather than ``allocate``'s looser ``prepared``-or-``activating``
    (``:482-485``). Failing early on a ``prepared`` activation the server has not advanced is
    cheaper and safer than allocating, acknowledging, mutating a live System, and only then being
    refused at commit.
    """

    def build(
        context: OperationContext, observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        deadline = context.activation.activation_readiness_deadline
        if deadline is None:
            raise _refuse("activating row has no activation readiness deadline")
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "activate",
                "result_ref": None,
                "evidence": terminal_evidence(context, "active"),
                "activation_readiness_deadline": deadline.isoformat().replace("+00:00", "Z"),
            },
        )

    def before_port(context: OperationContext) -> ExternalBootAuthoritySuccessV1 | None:
        if context.activation.state is not State.PREPARED:
            deadline = context.activation.activation_readiness_deadline
            if deadline is not None and ports.clock() >= deadline:
                raise CategorizedError(
                    "activation readiness deadline expired",
                    category=ErrorCategory.BOOT_TIMEOUT,
                    terminal=True,
                )
            return None
        deadline = ports.clock() + ports.activation_readiness_timeout
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "deadline",
                "deadline": deadline.isoformat().replace("+00:00", "Z"),
            },
        )

    return _handler(
        ports,
        require_activation_state=frozenset({State.PREPARED, State.ACTIVATING}),
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=_no_preconditions,
        expected_observation="target",
        build_result=build,
        before_port=before_port,
    )


def _recovering_handler(
    ports: ExternalBootHandlerPorts,
    *,
    operation: str,
    state: State,
    attempt_state: str,
) -> ExternalBootOperationHandler:
    """``recover`` and ``resolve-conflict`` differ in three parameters, so they share a body."""

    def build(
        context: OperationContext, observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": operation,
                "result_ref": None,
                "evidence": terminal_evidence(context, "recovered"),
            },
        )

    return _handler(
        ports,
        require_activation_state=frozenset({state}),
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=_require_attempt_state(attempt_state),
        expected_observation="source",
        build_result=build,
    )


def recover_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Re-run the recovery point and confirm the kernel, under a ``recovering`` attempt row."""

    def build(
        context: OperationContext, _observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "recover",
                "result_ref": None,
                "evidence": terminal_evidence(context, "recovered"),
            },
        )

    def before_port(context: OperationContext) -> ExternalBootAuthoritySuccessV1 | None:
        if context.activation.state is not State.ACTIVE:
            deadline = context.prerequisites.get("attempt_deadline")
            if deadline is not None and ports.clock() >= deadline:
                raise CategorizedError(
                    "recovery readiness deadline expired",
                    category=ErrorCategory.READINESS_FAILURE,
                    terminal=True,
                )
            return None
        deadline = ports.clock() + ports.recovery_readiness_timeout
        attempt_id = uuid5(
            NAMESPACE_URL, f"kdive/external-boot/{context.marker.operation_identity}"
        )
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "recovery-attempt",
                "attempt_id": str(attempt_id),
                "recovery_basis": "recovery_point",
                "deadline": deadline.isoformat().replace("+00:00", "Z"),
            },
        )

    async def require_attempt(
        conn: AsyncConnection,
        activation: ExternalBootActivation,
        marker: ExternalBootAuthorityMarkerV1,
    ) -> Mapping[str, Any]:
        if activation.state is State.ACTIVE:
            return {}
        return await _require_attempt_state("recovering")(conn, activation, marker)

    return _handler(
        ports,
        require_activation_state=frozenset({State.ACTIVE, State.RECOVERING}),
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=require_attempt,
        expected_observation="source",
        build_result=build,
        before_port=before_port,
    )


def resolve_conflict_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Start a new recovery window before resolving a parked conflict."""

    def build(
        context: OperationContext, _observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "resolve-conflict",
                "result_ref": None,
                "evidence": terminal_evidence(context, "recovered"),
            },
        )

    def before_port(context: OperationContext) -> ExternalBootAuthoritySuccessV1 | None:
        if context.activation.state is State.RECOVERING:
            deadline = context.prerequisites.get("attempt_deadline")
            if deadline is not None and ports.clock() >= deadline:
                raise CategorizedError(
                    "recovery readiness deadline expired",
                    category=ErrorCategory.READINESS_FAILURE,
                    terminal=True,
                )
            return None
        deadline = ports.clock() + ports.recovery_readiness_timeout
        attempt_id = uuid5(
            NAMESPACE_URL, f"kdive/external-boot/{context.marker.operation_identity}"
        )
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "recovery-attempt",
                "attempt_id": str(attempt_id),
                "recovery_basis": "pre_recovery",
                "deadline": deadline.isoformat().replace("+00:00", "Z"),
            },
        )

    async def require_attempt(
        conn: AsyncConnection,
        activation: ExternalBootActivation,
        marker: ExternalBootAuthorityMarkerV1,
    ) -> Mapping[str, Any]:
        if activation.state is State.RECOVERY_CONFLICT:
            await _require_attempt_state("conflict")(conn, activation, marker)
            return {}
        return await _require_attempt_state("recovering")(conn, activation, marker)

    return _handler(
        ports,
        require_activation_state=frozenset({State.RECOVERY_CONFLICT, State.RECOVERING}),
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=require_attempt,
        expected_observation="source",
        build_result=build,
        before_port=before_port,
    )


def release_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Release the recovery-store reservation, copying its three fields from the row verbatim.

    ``objects`` is **always empty, and that is the truthful value.** Release performs no deletion:
    ADR-0584's merged adapter lists ``RELEASE`` in neither its mutating nor its deleting operation
    set, because deletion belongs to ``cleanup`` under a later generation. So at release time no
    owned object is absent, and ``_ReleaseObject`` can represent only an absent object. Nor could
    the handler check: ``ExternalBootPorts`` has no method reporting per-object absence, and
    ``observe`` returns a ``RunningKernelObservation`` carrying no object identity.
    ``enumeration_complete`` is truthful because the domain it can check is empty, not because it
    checked and found nothing.
    The handler never asserts ``absent`` for an object it did not check, and this design gives it no
    way to; a store-side enumeration needs a port that does not exist (#2199/#2200).

    Crediting the reservation back while the objects still exist departs from ADR-0583's ordering;
    that is deferral record 0010, which this handler neither introduces nor resolves.
    """

    def build(
        context: OperationContext, observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        reservation = context.prerequisites["reservation"]
        evidence = {
            "schema": "external-boot-release-evidence-v1",
            "activation_id": str(context.marker.activation_id),
            "system_id": str(context.marker.system_id),
            "store_identity": {"ref": reservation["store_identity"]},
            "owner_key": {"ref": reservation["owner_key"]},
            "reserved_bytes": reservation["reserved_bytes"],
            "enumeration_complete": True,
            "objects": [],
            "verified_at": _now(),
        }
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "release",
                "result_ref": None,
                "release_identity": evidence_digest(evidence),
                "evidence": evidence,
            },
        )

    return _handler(
        ports,
        require_activation_state=_RECOVERY_STATES,
        # Require the complete persisted activation evidence pair before releasing its reservation.
        # Only `recovery_point` feeds the source-authority observation, but the domain model binds
        # it to `materialization`; checking both keeps a partial row from reaching allocation.
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=_require_releasable,
        expected_observation="source",
        build_result=build,
    )


def _cleanup_evidence(
    context: OperationContext, *, teardown_identity: str | None
) -> dict[str, Any]:
    ordinary = context.activation.state in _ORDINARY_CLEANUP_STATES
    evidence: dict[str, Any] = {
        "schema": "external-boot-cleanup-evidence-v1",
        "activation_id": str(context.marker.activation_id),
        "system_id": str(context.marker.system_id),
        "release_identity": context.prerequisites["release_identity"],
        "mode": "ordinary" if ordinary else "system_teardown",
        "completed_at": _now(),
    }
    if teardown_identity is not None:
        evidence["teardown_identity"] = teardown_identity
    return evidence


def cleanup_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Delete the recovery-store objects the release credited, naming the release it completes.

    ``mode`` is derived from the activation state rather than chosen: ``ordinary`` for
    ``recovered``/``abandoned`` and ``system_teardown`` for ``recovery_conflict``/
    ``recovery_failed``, which is exactly the pairing the commit re-checks
    (``0122…sql:1400-1413``) — and ``teardown_identity`` must be present for the second and absent
    for the first.
    """

    def build(
        context: OperationContext, _observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        ordinary = context.activation.state in _ORDINARY_CLEANUP_STATES
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "cleanup",
                "result_ref": None,
                "evidence": _cleanup_evidence(
                    context,
                    teardown_identity=(
                        None if ordinary else evidence_digest(_teardown_evidence(context))
                    ),
                ),
            },
        )

    return _handler(
        ports,
        require_activation_state=_CLEANUP_STATES,
        require_activation_evidence=frozenset({"recovery_point"}),
        require_preconditions=_require_cleanable,
        expected_observation="absent",
        build_result=build,
    )


def _teardown_evidence(context: OperationContext) -> dict[str, Any]:
    return {
        "schema": "external-boot-teardown-evidence-v1",
        "system_id": str(context.marker.system_id),
        "system_state": "torn_down",
        "observed_at": _now(),
    }


def teardown_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """The only operation on the ``teardown`` kind; carries both evidences in one result."""

    def build(
        context: OperationContext, _observation: AuthorityObservationV1
    ) -> ExternalBootAuthoritySuccessV1:
        # Built once and digested, so `teardown_identity` names the exact document this result
        # carries. The commit persists that document verbatim
        # (`0122…sql:1454-1458`), so an auditor holding the stored row can recompute the digest
        # and check it. Digesting a *different* document — an earlier version of this handler
        # digested `{schema, system_id, system_state, generation}`, which is not what it emits —
        # produces an identity that names nothing recoverable, and nothing in the schema would
        # ever catch it: the commit only checks the digest's shape.
        teardown_evidence = _teardown_evidence(context)
        return authority_result(
            context,
            {
                "schema": "external-boot-authority-result-v1",
                "operation": "teardown",
                "result_ref": None,
                "teardown_evidence": teardown_evidence,
                "cleanup_evidence": _cleanup_evidence(
                    context, teardown_identity=evidence_digest(teardown_evidence)
                ),
            },
        )

    return _handler(
        ports,
        require_activation_state=_TEARDOWN_STATES,
        require_activation_evidence=frozenset({"recovery_point"}),
        require_preconditions=_require_torn_down_system,
        expected_observation="absent",
        build_result=build,
    )
