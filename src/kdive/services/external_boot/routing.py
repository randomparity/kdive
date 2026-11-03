"""Read fixed authority metadata from the resolved provider runtime (ADR-0654)."""

from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.ports.authority import AuthorityReservationGeometry
from kdive.providers.system_authority.routing import authority_instance_for_resource
from kdive.services.systems.authority_owned import authority_system_binding


def server_authority_instance(binding: ProviderBinding) -> str | None:
    """Return server-visible routing identity without resolving worker credentials."""
    capability = binding.runtime.authority
    return None if capability is None else capability.authority_instance


async def admit_authority_route(
    conn: AsyncConnection, system_id: UUID, binding: ProviderBinding
) -> str | None:
    """Return the authority a Run step may use, or refuse a route that does not own the System.

    The server-wide route (``server_authority_instance``) is not per-System: a fixed-worker
    System can sit on a server that routes elsewhere, and its boot would strand an activation on
    an authority that cannot find the domain (ADR-0752). Admit only when the System's own
    Resource names the same authority and any recorded authority binding agrees.
    """
    authority = server_authority_instance(binding)
    if authority is None:
        return None
    resource_route = (
        None
        if binding.resource_name is None
        else authority_instance_for_resource(binding.kind, binding.resource_name)
    )
    if resource_route != authority:
        raise _ownership_error("authority_resource_route_mismatch")
    owner = await authority_system_binding(conn, system_id)
    if owner is not None and owner.authority_instance != authority:
        raise _ownership_error("authority_system_binding_mismatch")
    return authority


def authority_reservation_geometry(binding: ProviderBinding) -> AuthorityReservationGeometry:
    """Return complete validated reservation geometry for the selected fixed route."""
    capability = binding.runtime.authority
    geometry = None if capability is None else capability.geometry
    if geometry is None:
        raise _route_error("authority_reservation_geometry_incomplete")
    if (
        geometry.reserve_bytes <= 0
        or geometry.max_bytes <= 0
        or geometry.reserve_bytes > geometry.max_bytes
    ):
        raise _route_error("authority_reservation_geometry_invalid")
    return geometry


def require_worker_authority_route(binding: ProviderBinding, expected: str) -> None:
    """Require the worker's complete fixed route to match the admitted identity."""
    capability = binding.runtime.authority
    if capability is None:
        raise _route_error("authority_route_missing")
    if capability.sender is None:
        raise _route_error(capability.missing_route_reason)
    if capability.authority_instance != expected:
        raise _route_error("authority_route_mismatch")


def _route_error(reason: str) -> CategorizedError:
    return CategorizedError(
        "external-boot authority route is missing or does not match admitted install",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": reason},
    )


def _ownership_error(reason: str) -> CategorizedError:
    return CategorizedError(
        "configured external-boot authority does not own this System's Resource; align the "
        "Resource's authority_instance with the server's authority route, or provision a new "
        "System on the intended Resource",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": reason},
    )


__all__ = [
    "AuthorityReservationGeometry",
    "admit_authority_route",
    "authority_reservation_geometry",
    "require_worker_authority_route",
    "server_authority_instance",
]
