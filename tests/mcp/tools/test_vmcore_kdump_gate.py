"""Direct unit tests for the computed kdump admission gate (ADR-0361, #958)."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from psycopg import AsyncConnection

from kdive.domain.catalog.images import ImageCatalogEntry
from kdive.domain.lifecycle.records import System
from kdive.mcp.tools import _vmcore_kdump_gate as gate
from kdive.serialization import JsonValue

_CONN = cast(AsyncConnection, object())
_ENTRY = cast(ImageCatalogEntry, object())


class _StubSystem:
    def __init__(self, provisioning_profile: object) -> None:
        self.provisioning_profile = provisioning_profile


def _system(profile: object) -> System:
    return cast(System, _StubSystem(profile))


def _patch_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    entry: object,
    block: dict[str, JsonValue] | None,
) -> None:
    async def _resolve(conn: object, system: object) -> object:
        return entry

    def _render(entry: object, target: object) -> dict[str, JsonValue]:
        assert block is not None
        return block

    monkeypatch.setattr(gate, "_resolve_catalog_rootfs", _resolve)
    monkeypatch.setattr(gate, "render_kdump_signal", _render)


def _refusing(system: System) -> dict[str, JsonValue] | None:
    return asyncio.run(gate.refusing_kdump_capability(_CONN, system))


def test_unresolvable_rootfs_passes_without_computing_a_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, entry=None, block=None)
    assert _refusing(_system("p")) is None


@pytest.mark.parametrize("status", ["incapable", "not_applicable"])
def test_confident_negative_statuses_refuse(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    block: dict[str, JsonValue] = {"capability": status, "reason": "x"}
    _patch_resolution(monkeypatch, entry=_ENTRY, block=block)
    assert _refusing(_system("p")) == block


@pytest.mark.parametrize("status", ["capable", "unverified", "surprise"])
def test_non_negative_statuses_pass(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    _patch_resolution(monkeypatch, entry=_ENTRY, block={"capability": status})
    assert _refusing(_system("p")) is None


def test_non_string_capability_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolution(monkeypatch, entry=_ENTRY, block={"capability": None})
    assert _refusing(_system("p")) is None


def test_refusing_statuses_are_exactly_the_two_confident_negatives() -> None:
    assert frozenset({"incapable", "not_applicable"}) == gate._REFUSING_STATUSES


def test_resolve_returns_none_for_an_unparsable_profile() -> None:
    entry = asyncio.run(gate._resolve_catalog_rootfs(_CONN, _system("::not-a-profile::")))
    assert entry is None
