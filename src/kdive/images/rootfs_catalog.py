"""Declarative local-libvirt rootfs catalog loader (ADR-0251).

The file-authoritative ``rootfs_catalog.toml`` maps a ``build-fs --image <name>`` to a
typed row. Each row carries a base ``source`` that is either a virt-builder template or a
sha256-pinned cloud image URL, plus the ``family`` that selects the customizer seam.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kdive.domain.errors import CategorizedError, ErrorCategory

DEFAULT_CATALOG_PATH = (
    Path(__file__).parents[3] / "fixtures" / "local-libvirt" / "rootfs_catalog.toml"
)

_VALID_FAMILIES: frozenset[str] = frozenset({"rhel", "debian", "suse"})


@dataclass(frozen=True, slots=True)
class VirtBuilderSource:
    """A base acquired from a virt-builder template."""

    template: str


@dataclass(frozen=True, slots=True)
class CloudImageSource:
    """A base downloaded from a sha256-pinned cloud-image URL."""

    url: str
    sha256: str


type RootfsSource = VirtBuilderSource | CloudImageSource


@dataclass(frozen=True, slots=True)
class RootfsCatalogEntry:
    """One resolved row of the local rootfs catalog.

    Attributes:
        makedumpfile_version: The curated build-time makedumpfile version this release's repos
            install (e.g. ``"1.7.9"``), verified against distro package indexes — the per-image
            operand of the computed kdump-capability predicate (:mod:`kdive.images.kdump_support`,
            ADR-0253). A snapshot, not live upstream truth; the actual built version is recorded in
            the published image's ``provenance["makedumpfile_version"]``. The kdump capability for a
            given target kernel is *computed* from this, not stored as a kernel-relative bit.
    """

    name: str
    distro: str
    version: str
    family: str
    arch: str
    kind: str
    source: RootfsSource
    makedumpfile_version: str


def _catalog_error(message: str, field: str) -> CategorizedError:
    """A ``CONFIGURATION_ERROR`` whose details name the offending field, not its value."""
    return CategorizedError(
        message,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"field": field},
    )


def _require_str(row: dict[Any, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise _catalog_error(f"rootfs catalog row is missing {field}", field)
    return value


def _parse_source(raw: object) -> RootfsSource:
    if not isinstance(raw, dict):
        raise _catalog_error("rootfs catalog source must be a table", "source")
    kind = raw.get("kind")
    if kind == "virt-builder":
        return VirtBuilderSource(template=_require_str(raw, "template"))
    if kind == "cloud-image":
        return CloudImageSource(url=_require_str(raw, "url"), sha256=_require_str(raw, "sha256"))
    raise _catalog_error("rootfs catalog source.kind is not recognized", "source.kind")


def _parse_entry(row: dict[str, Any]) -> RootfsCatalogEntry:
    family = _require_str(row, "family")
    if family not in _VALID_FAMILIES:
        raise _catalog_error("rootfs catalog family is not recognized", "family")
    return RootfsCatalogEntry(
        name=_require_str(row, "name"),
        distro=_require_str(row, "distro"),
        version=_require_str(row, "version"),
        family=family,
        arch=_require_str(row, "arch"),
        kind=_require_str(row, "kind"),
        source=_parse_source(row.get("source")),
        makedumpfile_version=_require_str(row, "makedumpfile_version"),
    )


def load_rootfs_catalog(path: Path | None = None) -> dict[str, RootfsCatalogEntry]:
    """Read and validate the rootfs catalog, keyed by ``name``.

    Args:
        path: Catalog file to read; defaults to the packaged fixture.

    Returns:
        A mapping of catalog ``name`` to its parsed :class:`RootfsCatalogEntry`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` on an unreadable or malformed catalog;
            the ``details["field"]`` names the offending field.
    """
    path = path or DEFAULT_CATALOG_PATH
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _catalog_error(f"rootfs catalog is unreadable: {path}", "catalog") from exc
    catalog: dict[str, RootfsCatalogEntry] = {}
    for row in raw.get("image", []):
        entry = _parse_entry(row)
        if entry.name in catalog:
            raise _catalog_error("rootfs catalog name is duplicated", "name")
        catalog[entry.name] = entry
    return catalog


def resolve_rootfs_entry(name: str, path: Path | None = None) -> RootfsCatalogEntry:
    """Resolve one catalog entry by ``name``.

    Args:
        name: The ``build-fs --image`` value to resolve.
        path: Catalog file to read; defaults to the packaged fixture.

    Returns:
        The matching :class:`RootfsCatalogEntry`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` naming ``name`` and the available names
            when ``name`` is absent from the catalog.
    """
    catalog = load_rootfs_catalog(path)
    entry = catalog.get(name)
    if entry is None:
        raise CategorizedError(
            f"unknown rootfs image: {name}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"field": "name", "available": sorted(catalog)},
        )
    return entry
