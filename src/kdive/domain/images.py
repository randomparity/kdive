"""Image catalog domain vocabulary."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from kdive.domain._records import DomainModel
from kdive.domain.image_format import ImageFormat
from kdive.domain.ownership import ManagedBy


class ImageVisibility(StrEnum):
    """Resolution scope of an image_catalog row (ADR-0092/0093).

    ``PUBLIC`` images resolve for every project; a ``PRIVATE`` image resolves only within
    its owning project and shadows a same-identity public image there.
    """

    PUBLIC = "public"
    PRIVATE = "private"


class ImageState(StrEnum):
    """Publish lifecycle of an image_catalog row (ADR-0092).

    ``DEFINED`` is seeded baseline metadata with no object yet; ``PENDING`` is a publish in
    flight (row written, object not yet HEAD-confirmed); ``REGISTERED`` is bootable.
    Resolution returns only ``REGISTERED`` rows.
    """

    DEFINED = "defined"
    PENDING = "pending"
    REGISTERED = "registered"


class ImageCatalogEntry(DomainModel):
    """One catalog image row — the single source of truth for a bootable rootfs (ADR-0092).

    Identity is ``(provider, name, arch)`` plus the boot layout (``format``, ``root_device``).
    ``object_key`` is the object-store key of the qcow2 — ``None`` for a ``DEFINED`` row whose
    bytes are not built yet — and ``digest`` is the qcow2 content digest (a rootfs image has no
    kernel ``build_id``), ``None`` until built. ``visibility``/``owner``/``expires_at`` express
    the public-vs-project-private scope (ADR-0093); the DB ``CHECK`` constraints tie ``owner``
    and ``expires_at`` to the private case and ``object_key`` to the non-``DEFINED`` case.
    ``pending_since`` backs the publish-deadline grace window the reconciler keys off.
    """

    provider: str
    name: str
    arch: str
    format: ImageFormat
    root_device: str
    object_key: str | None = None
    digest: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    visibility: ImageVisibility
    owner: str | None = None
    expires_at: datetime | None = None
    state: ImageState = ImageState.DEFINED
    pending_since: datetime
    managed_by: ManagedBy = ManagedBy.RUNTIME
    volume: str | None = None


__all__ = ["ImageCatalogEntry", "ImageState", "ImageVisibility"]
