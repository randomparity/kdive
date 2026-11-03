"""Admit a Run step to the external-boot authority only for a System it serves (ADR-0752).

Server-side only: it reads operator inventory, which worker handlers must not import.
"""

from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.system_authority.routing import authority_admission_routes_for_resource
from kdive.services.external_boot.routing import server_authority_instance
from kdive.services.systems.authority_owned import authority_system_binding


async def admit_authority_route(
    conn: AsyncConnection, system_id: UUID, binding: ProviderBinding
) -> str | None:
    """Return the authority a Run step may use, or refuse a route that does not serve the System.

    ``server_authority_instance`` is one server-wide setting, not per-System: a fixed-worker
    System on a Resource that does not name the authority would strand an activation there
    (#3138). A recorded authority binding decides first; otherwise the Resource's own route or
    fixture relocation declaration must name the authority.
    """
    authority = server_authority_instance(binding)
    if authority is None:
        return None
    owner = await authority_system_binding(conn, system_id)
    if owner is not None:
        if owner.authority_instance != authority:
            raise _refusal("authority_system_binding_mismatch")
        return authority
    routes = (
        frozenset()
        if binding.resource_name is None
        else authority_admission_routes_for_resource(binding.kind, binding.resource_name)
    )
    if authority not in routes:
        raise _refusal("authority_resource_route_mismatch")
    return authority


def _refusal(reason: str) -> CategorizedError:
    return CategorizedError(
        "the configured external-boot authority does not serve this System's Resource; declare "
        "relocated_authority_instance only if every System on it is moved onto that authority's "
        "daemon, otherwise end this System and provision a new one on a Resource that routes to "
        "the authority",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": reason},
    )
