"""Catalog-owned rootfs build identity resolution."""

from __future__ import annotations

from dataclasses import dataclass

from kdive.domain.catalog.image_format import ImageFormat
from kdive.images.families import family_for
from kdive.images.planes.base import RootfsBuildSpec
from kdive.images.rootfs.catalog import (
    CloudImageSource,
    RootfsSource,
    resolve_rootfs_entry,
)

ROOTFS_FORMAT: ImageFormat = "qcow2"
ROOTFS_ROOT_DEVICE = "/dev/vda"


@dataclass(frozen=True, slots=True)
class CatalogRootfsBuild:
    """A catalog-derived build spec plus the catalog row fields used at publish."""

    spec: RootfsBuildSpec
    format: ImageFormat
    root_device: str


def source_image_digest(source: RootfsSource) -> str:
    """Render the provenance ``source_image_digest`` for a resolved catalog base source."""
    if isinstance(source, CloudImageSource):
        return f"cloud-image:{source.url}@sha256:{source.sha256}"
    return f"virt-builder:{source.template}"


def catalog_rootfs_build(
    provider: str, name: str, *, packages: tuple[str, ...] = ()
) -> CatalogRootfsBuild:
    """Resolve a local-libvirt catalog image into the rootfs build and publish fields."""
    entry = resolve_rootfs_entry(name)
    family = family_for(entry.family)
    build_packages = packages or family.packages(entry.kind, entry.distro, entry.version)
    spec = RootfsBuildSpec(
        provider=provider,
        name=entry.name,
        arch=entry.arch,
        releasever=entry.version,
        packages=build_packages,
        source_image_digest=source_image_digest(entry.source),
        capabilities=family.capabilities(entry.kind, entry.distro, entry.version),
        distro=entry.distro,
    )
    return CatalogRootfsBuild(spec=spec, format=ROOTFS_FORMAT, root_device=ROOTFS_ROOT_DEVICE)
