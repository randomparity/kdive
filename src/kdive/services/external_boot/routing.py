"""Resolve the fixed authority identity selected by a provider binding (ADR-0613)."""

from __future__ import annotations

import kdive.config as config
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.external_boot_authority.local_client import local_authority_binding
from kdive.providers.external_boot_authority.settings import AUTHORITY_INSTANCE
from kdive.providers.remote_libvirt.config import remote_config_for_resource


def server_authority_instance(binding: ProviderBinding) -> str | None:
    """Return server-visible routing identity without resolving worker credentials."""
    if binding.kind is ResourceKind.LOCAL_LIBVIRT:
        return config.get(AUTHORITY_INSTANCE)
    if binding.kind is ResourceKind.REMOTE_LIBVIRT and binding.resource_name is not None:
        authority = remote_config_for_resource(binding.resource_name).authority
        return None if authority is None else authority.authority_instance
    return None


def require_worker_authority_route(binding: ProviderBinding, expected: str) -> None:
    """Require the worker's complete fixed route to match the admitted identity."""
    if binding.runtime.authority is None:
        raise _route_error("authority_route_missing")
    if binding.kind is ResourceKind.LOCAL_LIBVIRT:
        local = local_authority_binding()
        actual = None if local is None else local.authority_instance
    elif binding.kind is ResourceKind.REMOTE_LIBVIRT and binding.resource_name is not None:
        remote = remote_config_for_resource(binding.resource_name).authority
        actual = None if remote is None else remote.authority_instance
    else:
        actual = None
    if actual != expected:
        raise _route_error("authority_route_mismatch")


def _route_error(reason: str) -> CategorizedError:
    return CategorizedError(
        "external-boot authority route is missing or does not match admitted install",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": reason},
    )


__all__ = ["require_worker_authority_route", "server_authority_instance"]
