"""Default build-config catalog references and the shared ``system`` convention (#1032)."""

from __future__ import annotations

from kdive.build_configs.defaults import DEFAULT_CONFIG_REF, catalog_config_ref
from kdive.components.references import CatalogComponentRef, parse_component_ref


def test_catalog_config_ref_dumps_the_canonical_shape() -> None:
    # Literal assertion, not factory-vs-factory: a drift of the provider value
    # (e.g. to "seed") must fail here.
    assert catalog_config_ref("x").model_dump() == {
        "kind": "catalog",
        "provider": "system",
        "name": "x",
    }


def test_catalog_config_ref_returns_a_component_ref() -> None:
    ref = catalog_config_ref("kdump")
    assert isinstance(ref, CatalogComponentRef)
    assert ref.name == "kdump"
    assert ref.provider == "system"


def test_default_config_ref_shares_the_factory_convention() -> None:
    # The seed default and the echoed convention derive from one provider value.
    assert catalog_config_ref("kdump") == DEFAULT_CONFIG_REF
    assert DEFAULT_CONFIG_REF.provider == catalog_config_ref("kdump").provider


def test_catalog_config_ref_round_trips_through_parse_component_ref() -> None:
    dumped = catalog_config_ref("inotify-fi").model_dump()
    assert parse_component_ref(dumped) == catalog_config_ref("inotify-fi")
