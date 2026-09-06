"""Resolve the fixed authority identity selected by a provider binding (ADR-0613)."""

from __future__ import annotations

from dataclasses import dataclass

import kdive.config as config
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.external_boot_authority.local_client import local_authority_binding
from kdive.providers.external_boot_authority.settings import (
    AUTHORITY_INSTANCE,
    AUTHORITY_RECOVERY_MAX_BYTES,
    AUTHORITY_RECOVERY_RESERVE_BYTES,
    AUTHORITY_STORE_IDENTITY,
)
from kdive.providers.local_libvirt.settings import LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES
from kdive.providers.remote_libvirt.config import remote_config_for_resource


@dataclass(frozen=True, slots=True)
class AuthorityReservationGeometry:
    store_identity: str
    reserve_bytes: int
    max_bytes: int


def server_authority_instance(binding: ProviderBinding) -> str | None:
    """Return server-visible routing identity without resolving worker credentials."""
    if binding.kind is ResourceKind.LOCAL_LIBVIRT:
        return config.get(AUTHORITY_INSTANCE)
    if binding.kind is ResourceKind.REMOTE_LIBVIRT and binding.resource_name is not None:
        authority = remote_config_for_resource(binding.resource_name).authority
        return None if authority is None else authority.authority_instance
    return None


def authority_reservation_geometry(binding: ProviderBinding) -> AuthorityReservationGeometry:
    """Return complete validated reservation geometry for the selected fixed route."""
    if binding.kind is ResourceKind.LOCAL_LIBVIRT:
        store = config.get(AUTHORITY_STORE_IDENTITY)
        reserve = config.get(AUTHORITY_RECOVERY_RESERVE_BYTES)
        maximum = config.get(AUTHORITY_RECOVERY_MAX_BYTES)
        worker_reserve = config.get(LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES)
        if None in (store, reserve, maximum) or reserve != worker_reserve:
            raise _route_error("authority_reservation_geometry_incomplete")
    elif binding.kind is ResourceKind.REMOTE_LIBVIRT and binding.resource_name is not None:
        authority = remote_config_for_resource(binding.resource_name).authority
        if (
            authority is None
            or authority.store_identity is None
            or authority.recovery_reserve_bytes is None
            or authority.recovery_max_bytes is None
        ):
            raise _route_error("authority_reservation_geometry_incomplete")
        store = authority.store_identity
        reserve = authority.recovery_reserve_bytes
        maximum = authority.recovery_max_bytes
    else:
        raise _route_error("authority_reservation_geometry_incomplete")
    assert isinstance(store, str) and isinstance(reserve, int) and isinstance(maximum, int)
    if reserve <= 0 or maximum <= 0 or reserve > maximum:
        raise _route_error("authority_reservation_geometry_invalid")
    return AuthorityReservationGeometry(store, reserve, maximum)


def require_worker_authority_route(binding: ProviderBinding, expected: str) -> None:
    """Require the worker's complete fixed route to match the admitted identity."""
    if binding.kind is ResourceKind.LOCAL_LIBVIRT:
        local = local_authority_binding()
        actual = None if local is None else local.authority_instance
    elif binding.kind is ResourceKind.REMOTE_LIBVIRT and binding.resource_name is not None:
        if binding.runtime.authority is None:
            raise _route_error("authority_route_missing")
        remote = remote_config_for_resource(binding.resource_name).authority
        actual = None if remote is None else remote.authority_instance
    else:
        raise _route_error("authority_route_missing")
    if actual != expected:
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
