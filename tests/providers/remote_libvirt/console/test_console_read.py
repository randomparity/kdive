"""Tests for the worker-side strict remote console reader (ADR-0429).

The reader assembles a running System's already-uploaded S3 console parts over a caller-specified
window and reports a freshness/error contract distinct from the best-effort boot-window
snapshotter. These tests inject fakes for the part store and the leader-liveness probe, so they
exercise the seam's contract (freshness flag, cursor, redaction, error propagation) without an
object store or a Postgres backend.
"""

from __future__ import annotations

import asyncio
import threading
from typing import cast
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection

from kdive.providers.remote_libvirt.console.read import RemoteLibvirtConsoleReader
from kdive.security.secrets.secret_registry import SecretRegistry

# Opaque stand-in: the injected leader probe never touches the connection in these tests.
_CONN = cast("AsyncConnection", object())


class FakeParts:
    """An in-memory ``_PartReader`` keyed by ``(system_id, index)``."""

    def __init__(self, parts: dict[int, bytes] | None = None, *, fail: Exception | None = None):
        self._parts = dict(parts or {})
        self._fail = fail

    def list_part_indices(self, system_id: UUID) -> list[int]:
        if self._fail is not None:
            raise self._fail
        return sorted(self._parts)

    def assemble(self, system_id: UUID, start_index: int = 0) -> bytes:
        if self._fail is not None:
            raise self._fail
        return b"".join(self._parts[i] for i in sorted(self._parts) if i >= start_index)


def _reader(
    parts: FakeParts, *, pumped: bool = True, registry: SecretRegistry | None = None
) -> RemoteLibvirtConsoleReader:
    async def probe(conn: object, name: str) -> bool:
        return pumped

    return RemoteLibvirtConsoleReader(
        parts=parts,
        secret_registry=registry or SecretRegistry(),
        leader_probe=probe,  # type: ignore[arg-type]
    )


def test_read_window_assembles_and_reports_cursor() -> None:
    reader = _reader(FakeParts({0: b"boot ", 1: b"more ", 2: b"tail"}))
    result = asyncio.run(reader.read_window(_CONN, uuid4()))
    assert result.data == b"boot more tail"
    assert result.next_index == 3
    assert result.pumped is True


def test_read_window_slices_to_requested_start_index() -> None:
    reader = _reader(FakeParts({0: b"prior ", 1: b"boot ", 2: b"live ", 3: b"tail"}))
    result = asyncio.run(reader.read_window(_CONN, uuid4(), start_index=2))
    assert result.data == b"live tail"
    assert result.next_index == 4


def test_empty_window_does_not_rewind_the_cursor() -> None:
    # No parts at or past the requested start: the cursor stays where the caller asked to read,
    # so a poller never re-reads earlier parts on the next poll.
    reader = _reader(FakeParts({0: b"prior", 1: b"boot"}))
    result = asyncio.run(reader.read_window(_CONN, uuid4(), start_index=5))
    assert result.data == b""
    assert result.next_index == 5


def test_pumped_true_with_empty_data_is_a_silent_console() -> None:
    reader = _reader(FakeParts({}), pumped=True)
    result = asyncio.run(reader.read_window(_CONN, uuid4()))
    assert result.data == b""
    assert result.pumped is True


def test_pumped_false_marks_an_unpumped_console() -> None:
    # Same empty read, but no leader is pumping: the caller must treat the emptiness as
    # "could not read", not "the kernel printed nothing".
    reader = _reader(FakeParts({}), pumped=False)
    result = asyncio.run(reader.read_window(_CONN, uuid4()))
    assert result.data == b""
    assert result.pumped is False


def test_store_read_failure_propagates() -> None:
    # Unlike the best-effort snapshotter, a store failure is surfaced, never swallowed as empty.
    reader = _reader(FakeParts(fail=ConnectionError("s3 unreachable")))
    with pytest.raises(ConnectionError, match="s3 unreachable"):
        asyncio.run(reader.read_window(_CONN, uuid4()))


def test_output_is_redacted_at_the_seam() -> None:
    registry = SecretRegistry()
    registry.register("hunter2", scope=None)
    reader = _reader(FakeParts({0: b"login password=hunter2 ok"}), registry=registry)
    result = asyncio.run(reader.read_window(_CONN, uuid4()))
    assert b"hunter2" not in result.data
    assert b"ok" in result.data


def test_non_utf8_console_bytes_do_not_raise() -> None:
    reader = _reader(FakeParts({0: b"\xff\xfe panic"}))
    result = asyncio.run(reader.read_window(_CONN, uuid4()))
    assert b"panic" in result.data


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("pumped", [False, True])
def test_redaction_yields_after_probe_with_current_secrets(
    monkeypatch: pytest.MonkeyPatch, cancel: bool, pumped: bool
) -> None:
    registry = SecretRegistry()
    secret = "registered-during-probe"  # pragma: allowlist secret — synthetic redaction fixture
    reader = _reader(FakeParts({2: b"\xff " + secret.encode() + b" console"}), registry=registry)
    redact = reader._redact
    release = threading.Event()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered = asyncio.Event()
        probed = False

        async def probe(conn: AsyncConnection, name: str) -> bool:
            nonlocal probed
            registry.register(secret, scope=None)
            probed = True
            return pumped

        def slow_redact(data: bytes) -> bytes:
            assert probed, "redaction must follow the leader probe"
            assert threading.get_ident() != loop_thread, "redaction blocks the event loop"
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "event loop did not release the redactor"
            return redact(data)

        monkeypatch.setattr(reader, "_leader_probe", probe)
        monkeypatch.setattr(reader, "_redact", slow_redact)
        task = asyncio.create_task(reader.read_window(_CONN, uuid4(), start_index=2))
        waiting = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait(
                (task, waiting), timeout=5, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                await task
                pytest.fail("read finished before the event loop released redaction")
            assert waiting in done, "redaction never started"
            assert not task.done(), "event loop must advance during redaction"
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            if not task.done():
                await asyncio.wait_for(task, timeout=5)
        if not cancel:
            result = await task
            assert result.data == "\ufffd [REDACTED] console".encode()
            assert result.next_index == 3
            assert result.pumped is pumped

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_at", ["probe", "redaction"])
def test_probe_and_redactor_failures_propagate(
    monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    reader = _reader(FakeParts({0: b"console"}))
    error = RuntimeError(f"{failure_at} failed")
    redacted = False

    async def probe(conn: AsyncConnection, name: str) -> bool:
        if failure_at == "probe":
            raise error
        return True

    def redact(data: bytes) -> bytes:
        nonlocal redacted
        redacted = True
        raise error

    monkeypatch.setattr(reader, "_leader_probe", probe)
    monkeypatch.setattr(reader, "_redact", redact)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(reader.read_window(_CONN, uuid4()))
    assert caught.value is error
    assert redacted is (failure_at == "redaction")
