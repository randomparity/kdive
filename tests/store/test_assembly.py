"""Direct unit tests for the object-store assembly composer (#1405, #665 pattern).

``store/assembly.py`` was previously imported only cross-file (conftest, test_app),
so mutmut could not attribute a killing test to it. These tests import the module by
dotted path and pin the two branches of ``build_object_store_assembly``: a provided
``store_factory`` is used verbatim, and the default path falls through to
``object_store_from_env``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from kdive.store import assembly as assembly_module
from kdive.store.assembly import ObjectStoreAssembly, build_object_store_assembly


def test_provided_store_factory_is_used_and_its_store_lands_in_the_assembly() -> None:
    sentinel_store = cast(Any, object())
    calls: list[None] = []

    def _fake_factory() -> Any:
        calls.append(None)
        return sentinel_store

    result = build_object_store_assembly(store_factory=_fake_factory)

    assert isinstance(result, ObjectStoreAssembly)
    assert result.store is sentinel_store
    assert len(calls) == 1


def test_default_store_factory_falls_through_to_object_store_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_store = cast(Any, object())
    calls: list[None] = []

    def _fake_from_env() -> Any:
        calls.append(None)
        return sentinel_store

    monkeypatch.setattr(assembly_module, "object_store_from_env", _fake_from_env)

    result = build_object_store_assembly()

    assert result.store is sentinel_store
    assert len(calls) == 1
