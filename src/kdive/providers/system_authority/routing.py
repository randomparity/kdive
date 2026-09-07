"""Server-side selection of explicitly configured System authority routes (ADR-0623)."""

from __future__ import annotations

from kdive.domain.catalog.resources import ResourceKind
from kdive.providers.local_libvirt.config import local_authority_instance_for_resource
from kdive.providers.remote_libvirt.config import remote_config_for_resource


def authority_instance_for_resource(kind: ResourceKind, resource_name: str) -> str | None:
    """Return the Resource's authority identity, or ``None`` when its route is disabled."""
    if kind is ResourceKind.LOCAL_LIBVIRT:
        return local_authority_instance_for_resource(resource_name)
    if kind is ResourceKind.REMOTE_LIBVIRT:
        binding = remote_config_for_resource(resource_name).authority
        return None if binding is None else binding.authority_instance
    return None
