"""Provider-scoped fixture catalog loader (ADR-0065)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

import kdive.config as config
from kdive.components.references import ComponentRef
from kdive.components.visibility import PUBLIC_VISIBILITY, Visibility
from kdive.config.core_settings import FIXTURE_CATALOG_PATH
from kdive.domain.catalog.image_format import ImageFormat
from kdive.domain.catalog.images import Capability
from kdive.domain.errors import CategorizedError, ErrorCategory

DEFAULT_FIXTURE_CATALOG_PATH = Path(__file__).parents[3] / "fixtures" / "local-libvirt"


class FixtureStorage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_component_roots: list[Path]
    cache_dir: Path
    overlay_dir: Path


class FixtureManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    provider: str
    storage: FixtureStorage
    rootfs: list[str] = Field(default_factory=list)
    profiles: list[str] = Field(default_factory=list)


class RootfsCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    name: str
    arch: str
    format: ImageFormat
    root_device: str
    source: ComponentRef
    visibility: Visibility
    capabilities: list[Capability] = Field(default_factory=list)


class ProfileCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    name: str
    arch: str


class FixtureCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: FixtureManifest
    rootfs: list[RootfsCatalogEntry]
    profiles: list[ProfileCatalogEntry]

    def rootfs_for_provider(self, provider: str) -> list[RootfsCatalogEntry]:
        return [
            entry
            for entry in self.rootfs
            if entry.provider == provider and entry.visibility == PUBLIC_VISIBILITY
        ]

    def rootfs_entry(self, provider: str, name: str) -> RootfsCatalogEntry | None:
        for entry in self.rootfs:
            if (
                entry.provider == provider
                and entry.name == name
                and entry.visibility == PUBLIC_VISIBILITY
            ):
                return entry
        return None

    def profile(self, provider: str, name: str) -> ProfileCatalogEntry | None:
        for entry in self.profiles:
            if entry.provider == provider and entry.name == name:
                return entry
        return None


def fixture_catalog_path_from_env() -> Path:
    raw = config.get(FIXTURE_CATALOG_PATH)
    if raw is None or raw == "":
        return DEFAULT_FIXTURE_CATALOG_PATH
    return Path(raw)


def load_fixture_catalog(path: Path | None = None) -> FixtureCatalog:
    """Read and validate one provider fixture catalog bundle."""
    path = path or fixture_catalog_path_from_env()
    try:
        manifest = FixtureManifest.model_validate(_load_yaml(path / "manifest.yaml"))
        rootfs = [
            RootfsCatalogEntry.model_validate(_load_yaml(path / manifest_path))
            for manifest_path in manifest.rootfs
        ]
        profiles = [
            ProfileCatalogEntry.model_validate(_load_yaml(path / manifest_path))
            for manifest_path in manifest.profiles
        ]
        return FixtureCatalog(manifest=manifest, rootfs=rootfs, profiles=profiles)
    except (OSError, TypeError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise CategorizedError(
            f"fixture catalog data is unusable: {path}",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)
