"""Stable SQL projection for the image-catalog domain model (ADR-0088)."""

from typing import LiteralString

IMAGE_CATALOG_ENTRY_PROJECTION: LiteralString = """
    id, created_at, updated_at, provider, name, arch, format, root_device,
    object_key, digest, capabilities, provenance, provenance_attested,
    visibility, owner, expires_at, state, pending_since, managed_by, volume,
    path, description, kernel_config_key, size_bytes
"""

__all__ = ["IMAGE_CATALOG_ENTRY_PROJECTION"]
