"""Provider-owned build-host discovery helpers for read surfaces."""

from __future__ import annotations

from kdive.domain.errors import CategorizedError
from kdive.providers.remote_libvirt.config import remote_instance_names


def declared_remote_instance_names() -> list[str]:
    """Return declared remote-libvirt instance names, degrading to empty on config errors.

    Read-only MCP surfaces use this to explain whether ``ephemeral_libvirt`` build-host rows are
    currently resolvable. Build-time provider config still fails closed through the provider's
    runtime path.
    """
    try:
        return remote_instance_names()
    except CategorizedError:
        return []
