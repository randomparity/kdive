"""Build-host component/config reference resolution helpers."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import kdive.config as config
from kdive.build_configs.defaults import DEFAULT_CONFIG_REF, CatalogConfigFetch
from kdive.components.catalog import load_fixture_catalog
from kdive.components.local_paths import validate_local_component_path
from kdive.components.references import (
    CatalogComponentRef,
    ComponentRef,
    LocalComponentRef,
)
from kdive.components.requirements import ConfigRequirements
from kdive.config.core_settings import BUILD_COMPONENT_ROOTS
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import ServerBuildProfile

DEFAULT_BUILD_COMPONENT_ROOT = "/var/lib/kdive/build/components"


def missing_config_groups(
    config_text: str, required_config: tuple[tuple[str, ...], ...]
) -> list[tuple[str, ...]]:
    """Return the required OR-groups not satisfied by ``config_text``."""
    enabled = {
        line.split("=", 1)[0]
        for line in config_text.splitlines()
        if line and not line.startswith("#") and line.rstrip().endswith("=y")
    }
    return [group for group in required_config if not any(opt in enabled for opt in group)]


def load_profile_config_requirements(provider: str, name: str) -> ConfigRequirements:
    """Load the named fixture profile's config requirements."""
    profile = load_fixture_catalog().profile(provider, name)
    if profile is None:
        raise CategorizedError(
            "unknown build profile requirements",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider, "name": name},
        )
    return profile.requires.config


def build_component_roots_from_env() -> list[Path]:
    """Read the worker build component root allowlist from ``KDIVE_BUILD_COMPONENT_ROOTS``."""
    raw = config.get(BUILD_COMPONENT_ROOTS)
    if raw is None:
        return [Path(DEFAULT_BUILD_COMPONENT_ROOT)]
    return [Path(part) for part in raw.split(":") if part]


def ref_error(kind: str, message: str) -> CategorizedError:
    """A ``CONFIGURATION_ERROR`` for a bad ref; details name the field, not its value."""
    return CategorizedError(
        message, category=ErrorCategory.CONFIGURATION_ERROR, details={"kind": kind}
    )


def resolve_local_ref(ref: str, *, kind: str) -> Path:
    """Resolve a build-profile ref to an existing local file."""
    parts = urlsplit(ref)
    if parts.scheme == "file":
        if parts.netloc:
            raise ref_error(kind, "config/patch ref must be a local file:// URL (no host)")
        path = Path(parts.path)
    elif parts.scheme == "":
        path = Path(ref)
    else:
        raise ref_error(kind, "config/patch ref scheme is not a local reference")
    if not path.is_absolute():
        raise ref_error(kind, "config/patch ref must be an absolute path")
    if not path.is_file():
        raise ref_error(kind, "config/patch ref does not resolve to a readable file")
    return path


def resolve_config_bytes(
    ref: ComponentRef,
    *,
    allowed_component_roots: list[Path],
    catalog_fetch: CatalogConfigFetch,
) -> bytes:
    """Resolve a config ref to fragment bytes."""
    if isinstance(ref, LocalComponentRef):
        path = validate_local_component_path(
            ref.path, allowed_roots=allowed_component_roots, sha256=ref.sha256
        )
        try:
            return path.read_bytes()
        except OSError as exc:
            raise CategorizedError(
                "config component ref could not be read",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"kind": "config", "path": str(path), "error": type(exc).__name__},
            ) from exc
    if isinstance(ref, CatalogComponentRef):
        return catalog_fetch(ref.name)
    raise ref_error("config", "config component ref must be local or catalog for builds")


def config_refs(profile: ServerBuildProfile) -> list[ComponentRef]:
    """The ordered config refs a build resolves: the default when absent, else the profile's.

    A single ref wraps to a one-element list; a list is returned as-is. This is the single
    source that replaces the scattered ``profile.config or DEFAULT_CONFIG_REF`` idiom, so the
    resolve site and the run-creation validation sites cannot diverge.
    """
    if profile.config is None:
        return [DEFAULT_CONFIG_REF]
    if isinstance(profile.config, list):
        return list(profile.config)
    return [profile.config]


def decode_fragment_text(raw: bytes) -> str:
    """Decode a config fragment as UTF-8, mapping a non-text fragment to ``CONFIGURATION_ERROR``.

    A ``local`` config ref resolves arbitrary on-disk bytes, so a non-UTF-8 fragment must fail
    fast with a categorized envelope rather than an uncaught ``UnicodeDecodeError``.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ref_error("config", "config fragment is not valid UTF-8 text") from exc


def effective_config_fragment(fragments: list[bytes]) -> bytes:
    """Collapse ordered fragments into one canonical fragment, last-writer-wins per symbol.

    Each ``CONFIG_x=<val>`` sets the symbol; each ``# CONFIG_x is not set`` unsets it; a later
    line for the same symbol overrides an earlier one across every fragment. Emitted in
    first-seen order so a composed set merges deterministically (independent of merge_config.sh's
    within-file duplicate handling). Comments and blank lines are inert and dropped.
    """
    values: dict[str, str | None] = {}
    for raw in fragments:
        for line in decode_fragment_text(raw).splitlines():
            stripped = line.strip()
            if stripped.startswith("# CONFIG_") and stripped.endswith(" is not set"):
                values[stripped[len("# ") : -len(" is not set")]] = None
            elif stripped.startswith("CONFIG_") and "=" in stripped:
                symbol, _, value = stripped.partition("=")
                values[symbol] = value
    lines = [
        f"{symbol}={value}" if value is not None else f"# {symbol} is not set"
        for symbol, value in values.items()
    ]
    return ("\n".join(lines) + "\n").encode()


def resolve_config_list_bytes(
    refs: list[ComponentRef],
    *,
    allowed_component_roots: list[Path],
    catalog_fetch: CatalogConfigFetch,
) -> bytes:
    """Resolve ordered config refs to fragment bytes for the merge step.

    A single ref returns its raw resolved bytes unchanged (the default/single-config path stays
    byte-for-byte). Multiple refs are resolved in order and collapsed by
    :func:`effective_config_fragment` so the merged ``.config`` reflects last-writer-wins.
    """
    resolved = [
        resolve_config_bytes(
            ref, allowed_component_roots=allowed_component_roots, catalog_fetch=catalog_fetch
        )
        for ref in refs
    ]
    if len(resolved) == 1:
        return resolved[0]
    return effective_config_fragment(resolved)


def validate_config_ref(ref: ComponentRef, *, allowed_component_roots: list[Path]) -> None:
    """Validate a build config ref shape at run creation."""
    if isinstance(ref, LocalComponentRef):
        validate_local_component_path(
            ref.path, allowed_roots=allowed_component_roots, sha256=ref.sha256
        )
        return
    if isinstance(ref, CatalogComponentRef):
        return
    raise ref_error("config", "config component ref must be local or catalog for builds")
