"""The six enqueueable external-boot operation handlers (spec §7).

Each is the shared runner parameterized by its row of the specification's §7 table: the activation
states the **commit** admits, the evidence columns it reads, its footnoted prerequisites, its port
call, and its result variant. Nothing here calls ``commit_external_boot_authority_result``; the
handler returns its result and the worker commits it under ``_authority_binding_matches``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

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
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.runner import (
    OperationContext,
    authority_ref,
    run_operation,
)
from kdive.jobs.models import (
    ExternalBootAuthorityMarkerV1,
    ExternalBootAuthoritySuccessV1,
)
from kdive.providers.ports.external_boot import RecoveryPoint, RunningKernelObservation

__all__ = [
    "ACTIVATION_READINESS_WINDOW",
    "activate_handler",
    "cleanup_handler",
    "recover_handler",
    "release_handler",
    "resolve_conflict_handler",
    "teardown_handler",
]

ACTIVATION_READINESS_WINDOW: Final[timedelta] = timedelta(minutes=15)
"""How far ahead of ``now(UTC)`` an ``activate`` result reports its readiness deadline.

Unit: a wall-clock interval. Reference clock: the worker's ``now(UTC)``, which the commit stores
verbatim after a parse check only (``0122_external_boot_authority.sql:1471-1488``).

**Nothing reads this value today.** The schema bounds it in no way and no reader exists in ``src/``
outside the model definition, so stating a consequence of violation or a recovery action would
document a feature that does not exist. Enforcing it is #2202's, which the charter excludes as
"deadline reuse". The value must still be emitted because
``_ActivateResult.activation_readiness_deadline`` is required.
"""

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


def _require_observed_kernel_matches(
    context: OperationContext, observation: RunningKernelObservation | None
) -> None:
    """The ``observe`` call is a post-mutation liveness precondition, and this is its whole point.

    Nothing in ``ExternalBootTerminalEvidenceV1`` consumes the observation — ``composite_state`` is
    the acknowledgement's digest, not one derived from it. The call exists so the operation can
    refuse to emit terminal evidence when the running kernel is not the one the activation's
    persisted ``materialization.kernel_observation`` records. The value is discarded after this
    comparison.
    """
    materialization = context.activation.materialization
    if observation is None or materialization is None:
        raise _refuse(
            f"activation {context.marker.activation_id} produced no kernel observation to verify"
        )
    if observation != materialization.kernel_observation:
        raise _refuse(
            f"the running kernel observed for activation {context.marker.activation_id} is not the "
            "one its persisted materialization records"
        )


async def _no_preconditions(
    _conn: AsyncConnection,
    _activation: ExternalBootActivation,
    _marker: ExternalBootAuthorityMarkerV1,
) -> Mapping[str, Any]:
    return {}


async def _attempt_state(conn: AsyncConnection, activation: ExternalBootActivation) -> str | None:
    if activation.current_attempt_id is None:
        return None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT state FROM external_boot_recovery_attempts WHERE attempt_id = %s",
            (activation.current_attempt_id,),
        )
        row = await cur.fetchone()
    return None if row is None else str(row["state"])


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
        state = await _attempt_state(conn, activation)
        if state != expected:
            raise _refuse(
                f"{marker.operation!r} requires the current recovery attempt in {expected!r}, "
                f"not {state!r}"
            )
        return {}

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
    call_port: Callable[[OperationContext], RunningKernelObservation | None],
    build_result: Callable[
        [OperationContext, RunningKernelObservation | None], ExternalBootAuthoritySuccessV1
    ],
) -> ExternalBootOperationHandler:
    async def handler(
        conn: AsyncConnection, job: Job, marker: ExternalBootAuthorityMarkerV1
    ) -> ExternalBootAuthoritySuccessV1:
        return await run_operation(
            conn,
            job,
            marker,
            ports=ports,
            require_activation_state=require_activation_state,
            require_activation_evidence=require_activation_evidence,
            require_preconditions=require_preconditions,
            call_port=call_port,
            build_result=build_result,
        )

    return handler


def activate_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Activate the prepared external boot, then confirm the running kernel is the expected one.

    The admitted state is ``activating`` alone, taken from the **commit** preconditions
    (``0122…sql:1302-1306``) rather than ``allocate``'s looser ``prepared``-or-``activating``
    (``:482-485``). Failing early on a ``prepared`` activation the server has not advanced is
    cheaper and safer than allocating, acknowledging, mutating a live System, and only then being
    refused at commit.
    """

    def call(context: OperationContext) -> RunningKernelObservation:
        recovery = _recovery(context)
        reference = authority_ref(context)
        context.port.activate(recovery, reference)
        return context.port.observe(recovery, reference)

    def build(
        context: OperationContext, observation: RunningKernelObservation | None
    ) -> ExternalBootAuthoritySuccessV1:
        _require_observed_kernel_matches(context, observation)
        deadline = datetime.now(UTC) + ACTIVATION_READINESS_WINDOW
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

    return _handler(
        ports,
        require_activation_state=frozenset({State.ACTIVATING}),
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=_no_preconditions,
        call_port=call,
        build_result=build,
    )


def _recovering_handler(
    ports: ExternalBootHandlerPorts, *, operation: str, state: State, attempt_state: str
) -> ExternalBootOperationHandler:
    """``recover`` and ``resolve-conflict`` differ in three parameters, so they share a body."""

    def call(context: OperationContext) -> RunningKernelObservation:
        recovery = _recovery(context)
        reference = authority_ref(context)
        context.port.recover(recovery, reference)
        return context.port.observe(recovery, reference)

    def build(
        context: OperationContext, observation: RunningKernelObservation | None
    ) -> ExternalBootAuthoritySuccessV1:
        _require_observed_kernel_matches(context, observation)
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
        call_port=call,
        build_result=build,
    )


def recover_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """Re-run the recovery point and confirm the kernel, under a ``recovering`` attempt row."""
    return _recovering_handler(
        ports, operation="recover", state=State.RECOVERING, attempt_state="recovering"
    )


def resolve_conflict_handler(ports: ExternalBootHandlerPorts) -> ExternalBootOperationHandler:
    """The same recovery, from ``recovery_conflict`` under an attempt row in ``conflict``."""
    return _recovering_handler(
        ports,
        operation="resolve-conflict",
        state=State.RECOVERY_CONFLICT,
        attempt_state="conflict",
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

    def call(context: OperationContext) -> RunningKernelObservation:
        recovery = _recovery(context)
        return context.port.observe(recovery, authority_ref(context))

    def build(
        context: OperationContext, observation: RunningKernelObservation | None
    ) -> ExternalBootAuthoritySuccessV1:
        _require_observed_kernel_matches(context, observation)
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
        # `materialization` as well as `recovery_point`, because this operation observes: it admits
        # `abandoned`, whose row is legal with `materialization` NULL, and the observation check
        # reads `materialization.kernel_observation`. Without it the refusal lands in build_result,
        # *after* allocation and the port call, and reports "produced no kernel observation to
        # verify" — blaming the provider for a missing persisted column. Listing the column here
        # moves the same refusal to step 2b, pre-allocation, with the accurate message.
        require_activation_evidence=_ACTIVATION_EVIDENCE,
        require_preconditions=_require_releasable,
        call_port=call,
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

    def call(context: OperationContext) -> None:
        # cleanup has no observation control: its port call is `cleanup`, and ExternalBootPorts
        # offers nothing to observe a deletion with. Stated rather than left to be inferred.
        context.port.cleanup(_recovery(context), authority_ref(context))
        return None

    def build(
        context: OperationContext, _observation: RunningKernelObservation | None
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
        call_port=call,
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

    def call(context: OperationContext) -> None:
        context.port.cleanup(_recovery(context), authority_ref(context))
        return None

    def build(
        context: OperationContext, _observation: RunningKernelObservation | None
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
        call_port=call,
        build_result=build,
    )
