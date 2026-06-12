"""Tests for the shared kernel-build fragment helpers (ADR-0096)."""

from __future__ import annotations

from kdive.provider_components.references import CatalogComponentRef
from kdive.providers.build_common import (
    _DEFAULT_CONFIG_REF,
    _dropped_fragment_symbols,
    _fragment_symbols,
)


def test_fragment_symbols_keeps_y_and_m_drops_comments_and_unset() -> None:
    fragment = (
        "CONFIG_CRASH_DUMP=y\n"
        "CONFIG_FOO=m\n"
        "# CONFIG_BAR is not set\n"
        "\n"
        "CONFIG_BAZ=n\n"
        "CONFIG_QUX=128\n"
    )
    assert _fragment_symbols(fragment) == ["CONFIG_CRASH_DUMP", "CONFIG_FOO"]


def test_dropped_fragment_symbols_reports_a_dropped_option() -> None:
    fragment = "CONFIG_CRASH_DUMP=y\nCONFIG_PROC_VMCORE=y\n# a comment\n"
    final = "CONFIG_CRASH_DUMP=y\n# CONFIG_PROC_VMCORE is not set\n"
    assert _dropped_fragment_symbols(fragment, final) == ["CONFIG_PROC_VMCORE"]


def test_dropped_fragment_symbols_empty_when_all_survive() -> None:
    fragment = "CONFIG_CRASH_DUMP=y\n"
    final = "CONFIG_CRASH_DUMP=y\nCONFIG_OTHER=y\n"
    assert _dropped_fragment_symbols(fragment, final) == []


def test_dropped_fragment_symbols_accepts_module_survivor() -> None:
    fragment = "CONFIG_FOO=m\n"
    final = "CONFIG_FOO=m\n"
    assert _dropped_fragment_symbols(fragment, final) == []


def test_default_config_ref_is_the_kdump_catalog_entry() -> None:
    assert (
        CatalogComponentRef(kind="catalog", provider="system", name="kdump") == _DEFAULT_CONFIG_REF
    )
