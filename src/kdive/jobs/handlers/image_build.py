"""Worker handler for the ``IMAGE_BUILD`` job: build -> validate -> publish (ADR-0092, #285).

An operator ``images build``/``publish`` enqueues an ``IMAGE_BUILD`` job; the worker runs this
handler. It drives the provider's :class:`RootfsBuildPlane` (the blocking, minutes-long
libguestfs build is offloaded via ``asyncio.to_thread`` so it never stalls the worker event
loop), validates the built image against the guest contract, then publishes it through the
row-first :func:`publish_image` two-write. A guest-contract validation failure raises a
``CategorizedError(CONFIGURATION_ERROR)``, which the worker turns into a dead-letter with that
named category (no half-published row: validation gates the publish).
"""

from __future__ import annotations

import asyncio

from psycopg import AsyncConnection

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.images.planes.base import RootfsBuildPlane
from kdive.images.rootfs_specs import CatalogRootfsBuild, catalog_rootfs_build
from kdive.images.validation import (
    DEFAULT_INSPECT,
    GUEST_CONTRACT_PATHS,
    InspectSeam,
    validate_guest_contract,
)
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ImageBuildPayload, load_payload
from kdive.providers.core.resolver import ProviderResolver
from kdive.services.images.publish import (
    ImageObjectStore,
    PublishRequest,
    publish_image,
)


async def image_build_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    store: ImageObjectStore,
    inspect: InspectSeam = DEFAULT_INSPECT,
) -> str:
    """Build, guest-contract-validate, and publish a catalog image; return its object key.

    Args:
        conn: The worker dispatch connection.
        job: The claimed ``IMAGE_BUILD`` job.
        resolver: Runtime resolver used by production assembly.
        store: The image object store.
        inspect: The libguestfs inspection seam threaded into the validator (tests inject a stub).

    Returns:
        The registered image's object key (the job ``result_ref``).

    Raises:
        CategorizedError: the build, guest-contract validation (``CONFIGURATION_ERROR`` naming
            the missing element), or publish fails — the worker dead-letters with the category.
    """
    payload = load_payload(job, ImageBuildPayload)
    catalog_build = _resolve_catalog_build(payload)
    build_plane = _resolve_build_plane(resolver, payload.provider)
    output = await asyncio.to_thread(build_plane.build, catalog_build.spec)
    # Only capabilities with an in-guest path marker are verifiable as a guest contract; the
    # build-fact tags without one (ssh/selinux/apparmor, ADR-0287) are not guest-contract
    # elements and are skipped here.
    guest_contract = [c for c in catalog_build.spec.capabilities if c in GUEST_CONTRACT_PATHS]
    await asyncio.to_thread(
        validate_guest_contract,
        output.qcow2_path,
        required=guest_contract,
        inspect=inspect,
    )
    request = PublishRequest(
        provider=payload.provider,
        name=payload.name,
        arch=catalog_build.spec.arch,
        format=catalog_build.format,
        root_device=catalog_build.root_device,
        digest=output.digest,
        capabilities=catalog_build.spec.capabilities,
        provenance=output.provenance,
        visibility=payload.visibility,
        owner=payload.owner,
        expires_at=payload.expires_at,
    )
    entry = await publish_image(conn, store, request=request, source=output.qcow2_path)
    if entry.object_key is None:  # Invariant: a registered row always carries its object key.
        raise RuntimeError(f"published image {entry.id} has no object_key")
    return entry.object_key


def _resolve_catalog_build(payload: ImageBuildPayload) -> CatalogRootfsBuild:
    """Resolve catalog-owned image identity for the provider-specific build plane."""
    if payload.provider == ResourceKind.LOCAL_LIBVIRT.value:
        return catalog_rootfs_build(payload.provider, payload.name, packages=payload.packages)
    raise CategorizedError(
        "provider-specific image build request is not implemented",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"provider": payload.provider},
    )


def _resolve_build_plane(resolver: ProviderResolver, provider: str) -> RootfsBuildPlane:
    try:
        kind = ResourceKind(provider)
    except ValueError as exc:
        raise CategorizedError(
            "unsupported image build provider",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider},
        ) from exc
    plane = resolver.resolve(kind).rootfs_build_plane
    if plane is None:
        raise CategorizedError(
            "provider runtime does not support rootfs image builds",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider},
        )
    return plane


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    store: ImageObjectStore,
    inspect: InspectSeam = DEFAULT_INSPECT,
) -> None:
    """Bind the ``IMAGE_BUILD`` job handler."""
    registry.register(
        JobKind.IMAGE_BUILD,
        lambda conn, job: image_build_handler(
            conn,
            job,
            resolver=resolver,
            store=store,
            inspect=inspect,
        ),
    )
