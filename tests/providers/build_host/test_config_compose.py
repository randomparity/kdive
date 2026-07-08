"""Compose helpers for build-config fragments (ADR-0316, #1036).

``config_refs`` normalizes the single/list/absent ``config`` forms; ``effective_config_fragment``
collapses ordered fragments with last-writer-wins; ``resolve_config_list_bytes`` keeps the
single-ref path byte-for-byte and normalizes only when composing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.build_configs.defaults import DEFAULT_CONFIG_REF
from kdive.components.references import CatalogComponentRef
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.shared.build_host.configuration.config import (
    config_refs,
    effective_config_fragment,
    resolve_config_list_bytes,
)


def _profile(config: object) -> ServerBuildProfile:
    profile = BuildProfile.parse(
        {"schema_version": 1, "kernel_source_ref": "warm-ref", "config": config}
    )
    assert isinstance(profile, ServerBuildProfile)
    return profile


def _cat(name: str) -> dict[str, object]:
    return {"kind": "catalog", "provider": "system", "name": name}


def test_config_refs_absent_yields_default() -> None:
    assert config_refs(_profile(None)) == [DEFAULT_CONFIG_REF]


def test_config_refs_single_wraps_in_list() -> None:
    refs = config_refs(_profile(_cat("kdump")))
    assert refs == [CatalogComponentRef(kind="catalog", provider="system", name="kdump")]


def test_config_refs_list_preserves_order() -> None:
    refs = config_refs(_profile([_cat("kdump"), _cat("faultinject")]))
    names = []
    for ref in refs:
        assert isinstance(ref, CatalogComponentRef)
        names.append(ref.name)
    assert names == ["kdump", "faultinject"]


def test_effective_fragment_later_value_wins() -> None:
    frags = [b"CONFIG_FOO=y\nCONFIG_BAR=y\n", b"CONFIG_FOO=m\n"]
    out = effective_config_fragment(frags).decode()
    assert "CONFIG_FOO=m" in out
    assert "CONFIG_FOO=y" not in out
    assert "CONFIG_BAR=y" in out


def test_effective_fragment_later_disable_wins() -> None:
    frags = [b"CONFIG_FOO=y\n", b"# CONFIG_FOO is not set\n"]
    out = effective_config_fragment(frags).decode()
    assert "# CONFIG_FOO is not set" in out
    assert "CONFIG_FOO=y" not in out


def test_effective_fragment_drops_comments_and_blanks() -> None:
    out = effective_config_fragment([b"# a comment\n\nCONFIG_FOO=y\n"]).decode()
    assert out.strip() == "CONFIG_FOO=y"


def test_resolve_single_ref_is_raw_bytes_unchanged() -> None:
    raw = b"# comment kept verbatim\nCONFIG_FOO=y\n"
    got = resolve_config_list_bytes(
        [CatalogComponentRef(kind="catalog", provider="system", name="kdump")],
        allowed_component_roots=[Path("/nonexistent")],
        catalog_fetch=lambda _n: raw,
    )
    assert got == raw  # single-ref path must not normalize


def test_effective_fragment_non_utf8_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as caught:
        effective_config_fragment([b"CONFIG_FOO=y\n", b"\xff\xfe not utf-8\n"])
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_resolve_multi_ref_returns_effective_fragment() -> None:
    fetches = {"a": b"CONFIG_FOO=y\n", "b": b"CONFIG_FOO=m\n"}
    got = resolve_config_list_bytes(
        [
            CatalogComponentRef(kind="catalog", provider="system", name="a"),
            CatalogComponentRef(kind="catalog", provider="system", name="b"),
        ],
        allowed_component_roots=[Path("/nonexistent")],
        catalog_fetch=lambda n: fetches[n],
    ).decode()
    assert "CONFIG_FOO=m" in got and "CONFIG_FOO=y" not in got
