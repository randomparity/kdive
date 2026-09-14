"""Read fixed authority metadata from the resolved provider runtime (ADR-0654)."""

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.ports.authority import AuthorityReservationGeometry


def server_authority_instance(binding: ProviderBinding) -> str | None:
    """Return server-visible routing identity without resolving worker credentials."""
    capability = binding.runtime.authority
    return None if capability is None else capability.authority_instance


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


__all__ = [
    "AuthorityReservationGeometry",
    "authority_reservation_geometry",
    "require_worker_authority_route",
    "server_authority_instance",
]
