"""The generator catalog uses production assembly without opening live services (ADR-0836)."""

from __future__ import annotations

import socket
from typing import Any

import pytest
from psycopg_pool import AsyncConnectionPool

import scripts.generate.offline_catalog as offline_catalog
from kdive.assembly import ProcessAssembly


def test_registry_extraction_does_not_open_live_services(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny_live_service(*args: object, **kwargs: object) -> None:
        raise AssertionError("offline catalog attempted to open a live service")

    monkeypatch.setattr(socket.socket, "connect", deny_live_service)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_live_service)
    monkeypatch.setattr(socket, "create_connection", deny_live_service)
    monkeypatch.setattr(AsyncConnectionPool, "open", deny_live_service)
    monkeypatch.setenv("KDIVE_WORKER_DEATH_VERIFIER", "disabled")

    names = {tool.name for tool in offline_catalog.registry_tools()}

    assert {"runs.boot", "ops.build_uses_list", "ops.recover_build_use"} <= names


def test_registry_extraction_uses_process_assembly(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _App:
        async def list_tools(self) -> list[Any]:
            return []

    def _build_app(*args: Any, **kwargs: Any) -> _App:
        captured.update(kwargs)
        return _App()

    monkeypatch.setattr(offline_catalog, "build_app_from_assembly", _build_app)

    assert offline_catalog.registry_tools() == []
    process = captured["process_assembly"]
    assert isinstance(process, ProcessAssembly)
    assert process.object_stores.store is not None
