"""Tests for the TCP-connect gdbstub_acl probe (ADR-0164)."""

from __future__ import annotations

import asyncio

import pytest

from kdive.diagnostics.gdbstub_acl import gdbstub_acl_probe


@pytest.mark.parametrize(
    ("raiser", "expected"),
    [
        (None, True),  # connect succeeds -> admits
        (ConnectionRefusedError(), True),  # fast refusal -> SYN reached host -> admits
        (TimeoutError(), False),  # DROP (incl. socket.timeout, its alias) -> blocked
        (OSError("dns"), None),  # indeterminate
    ],
)
def test_probe_classifies(raiser: Exception | None, expected: bool | None) -> None:
    captured: dict[str, object] = {}

    def fake_connector(host: str, port: int) -> None:
        captured["host"], captured["port"] = host, port
        if raiser is not None:
            raise raiser

    probe = gdbstub_acl_probe(connector=fake_connector)

    async def _run() -> bool | None:
        return await probe("host.example", "47000-47099")

    assert asyncio.run(_run()) is expected
    assert captured == {"host": "host.example", "port": 47000}


def test_empty_host_is_indeterminate_without_connecting() -> None:
    # An unset gdb_addr ("") must report error (None), not silently probe localhost (ADR-0164).
    called = False

    def fake_connector(host: str, port: int) -> None:
        nonlocal called
        called = True

    probe = gdbstub_acl_probe(connector=fake_connector)

    async def _run() -> bool | None:
        return await probe("", "47000-47099")

    assert asyncio.run(_run()) is None
    assert called is False
