"""The external-boot conflict evidence block of ``systems.get`` (ADR-0807, #3331)."""

from __future__ import annotations

from psycopg import AsyncConnection

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.domain.lifecycle.records import System
from kdive.mcp.responses import JsonValue
from kdive.mcp.tools.lifecycle._recovery import iso
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, role_satisfies

#: The most observed objects one response lists; the rest are flagged by ``objects_truncated``.
MAX_CONFLICT_OBJECTS = 64
RESOLVE_CONFLICT_ACTION = "systems.resolve_external_boot_conflict"

_REPOSITORY = ExternalBootActivationRepository()


async def read_conflict_evidence(
    conn: AsyncConnection, ctx: RequestContext, system: System
) -> dict[str, JsonValue] | None:
    """Return the System's recorded conflict evidence, or None when it is not to be shown.

    Shown only to a caller holding admin on the System's project, the role the resolve tool
    requires: the observed objects name provider storage, and a lower role cannot act on them.
    The check does not raise, so a lower role reads exactly what it read before. Only the
    System's own restricting activation is consulted, never a caller-supplied identity.
    """
    if not role_satisfies(ctx.roles.get(system.project), Role.ADMIN):
        return None
    activation = await _REPOSITORY.get_restricting_for_system(conn, system.id)
    if activation is None:
        return None
    attempt = await _REPOSITORY.get_current_recovery_attempt(conn, activation.id)
    # The attempt model holds conflict evidence exactly while the attempt is in conflict.
    if attempt is None or attempt.conflict_evidence is None:
        return None
    evidence = attempt.conflict_evidence
    refs = [ref.ref for ref in evidence.objects]
    return {
        "activation_id": str(activation.id),
        "attempt_id": str(attempt.attempt_id),
        "observation_id": str(evidence.observation_id),
        "composite_state": evidence.composite_state,
        "observed_at": iso(evidence.observed_at),
        "objects": list(refs[:MAX_CONFLICT_OBJECTS]),
        "objects_truncated": len(refs) > MAX_CONFLICT_OBJECTS,
    }
