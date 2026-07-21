"""Tests for the opt-in ASGI transport-trace middleware (ADR-0417)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from kdive import config
from kdive.mcp.middleware.transport_trace import (
    TransportTraceMiddleware,
    _header,
    mcp_trace_enabled,
)

TRACE_LOGGER = "kdive.mcp.transport_trace"


def _scope(headers: list[tuple[bytes, bytes]], method: str = "POST", path: str = "/mcp") -> dict:
    return {"type": "http", "method": method, "path": path, "headers": headers}


async def _noop_send(message) -> None:
    return None


async def _recv() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _ok_app(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


def _records(caplog) -> list[Any]:
    """Trace records; typed ``Any`` so the dynamic ``extra`` attributes read cleanly."""
    return [r for r in caplog.records if r.name == TRACE_LOGGER]


@contextlib.contextmanager
def _preserve_root_logger():
    """Snapshot and fully restore the root logger's level and handlers."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    try:
        yield root
    finally:
        root.setLevel(level)
        root.handlers[:] = handlers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("0", False),
        ("false", False),
        ("off", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("YES", True),
        ("On", True),
    ],
)
def test_mcp_trace_enabled_resolves_truthy_set(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv("KDIVE_MCP_TRACE", raising=False)
    else:
        monkeypatch.setenv("KDIVE_MCP_TRACE", raw)
    config.load()
    assert mcp_trace_enabled() is expected


def test_happy_path_logs_all_fields(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    headers = [(b"mcp-session-id", b"sess-abc"), (b"mcp-protocol-version", b"2025-06-18")]
    asyncio.run(TransportTraceMiddleware(_ok_app)(_scope(headers), _recv, _noop_send))
    (rec,) = _records(caplog)
    assert rec.status == 200
    assert rec.method == "POST"
    assert rec.path == "/mcp"
    assert rec.mcp_session_id == "sess-abc"
    assert rec.mcp_session_id_present is True
    assert rec.mcp_protocol_version == "2025-06-18"
    assert isinstance(rec.duration_ms, float) and rec.duration_ms >= 0.0


def test_no_session_header(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    asyncio.run(TransportTraceMiddleware(_ok_app)(_scope([]), _recv, _noop_send))
    (rec,) = _records(caplog)
    assert rec.mcp_session_id_present is False
    assert rec.mcp_session_id is None
    assert rec.authorization_present is False


def test_authorization_logged_presence_only(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    token = "Bearer super-secret-token-value"
    asyncio.run(
        TransportTraceMiddleware(_ok_app)(
            _scope([(b"authorization", token.encode())]), _recv, _noop_send
        )
    )
    (rec,) = _records(caplog)
    assert rec.authorization_present is True
    assert "super-secret-token-value" not in rec.getMessage()
    assert "super-secret-token-value" not in str(rec.__dict__)


def test_raise_before_response_start_logs_status_none(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)

    async def boom(scope, receive, send) -> None:
        raise RuntimeError("dispatch failed")

    with pytest.raises(RuntimeError):
        asyncio.run(TransportTraceMiddleware(boom)(_scope([]), _recv, _noop_send))
    (rec,) = _records(caplog)
    assert rec.status is None
    assert isinstance(rec.duration_ms, float) and rec.duration_ms >= 0.0


def test_cancel_before_response_start_logs_status_none(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)

    async def cancelled(scope, receive, send) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(TransportTraceMiddleware(cancelled)(_scope([]), _recv, _noop_send))
    (rec,) = _records(caplog)
    assert rec.status is None
    assert isinstance(rec.duration_ms, float)


def test_post_header_disconnect_keeps_opening_line_only(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)

    async def stream_then_cancel(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(TransportTraceMiddleware(stream_then_cancel)(_scope([]), _recv, _noop_send))
    (rec,) = _records(caplog)  # exactly one record
    assert rec.status == 200  # the opening line; no second (close) line


def test_non_http_scope_passthrough(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    seen = {}

    async def app(scope, receive, send) -> None:
        seen["type"] = scope["type"]

    asyncio.run(TransportTraceMiddleware(app)({"type": "lifespan"}, _recv, _noop_send))
    assert seen["type"] == "lifespan"
    assert _records(caplog) == []


def test_level_independent_of_raised_root_floor(caplog) -> None:
    with _preserve_root_logger() as root:
        root.setLevel(logging.WARNING)  # mimic KDIVE_LOG_LEVEL=warning
        caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
        asyncio.run(TransportTraceMiddleware(_ok_app)(_scope([]), _recv, _noop_send))
        assert len(_records(caplog)) == 1


def test_otel_bridge_handler_stays_notset() -> None:
    from opentelemetry.sdk._logs import LoggerProvider

    from kdive.observability.facade import _bridge_root_logger

    with _preserve_root_logger() as root:
        before = list(root.handlers)
        _bridge_root_logger(LoggerProvider(), "warning")
        added = [h for h in root.handlers if h not in before]
        assert added, "expected the bridge handler to be installed"
        assert all(h.level == logging.NOTSET for h in added)


def test_concurrent_requests_do_not_share_state(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    b_entered = asyncio.Event()

    async def app(scope, receive, send) -> None:
        # One shared middleware instance handles both requests; A suspends until B has
        # entered and captured its own state, so a self-attribute impl would cross-log.
        if _header(scope, b"mcp-session-id") == "A":
            await b_entered.wait()
            await send({"type": "http.response.start", "status": 201, "headers": []})
        else:
            b_entered.set()
            await send({"type": "http.response.start", "status": 202, "headers": []})

    mw = TransportTraceMiddleware(app)  # ONE instance, shared across both requests

    async def run() -> None:
        await asyncio.gather(
            mw(_scope([(b"mcp-session-id", b"A")]), _recv, _noop_send),
            mw(_scope([(b"mcp-session-id", b"B")]), _recv, _noop_send),
        )

    asyncio.run(run())
    by_session = {r.mcp_session_id: r.status for r in _records(caplog)}
    assert by_session == {"A": 201, "B": 202}


def test_seam_includes_trace_outermost_when_enabled() -> None:
    from kdive.processes.server import server_http_middleware

    mws = server_http_middleware(trace_enabled=True)
    assert mws[0].cls is TransportTraceMiddleware  # first entry == outermost wrapper


def test_seam_excludes_trace_when_disabled() -> None:
    from kdive.processes.server import server_http_middleware

    mws = server_http_middleware(trace_enabled=False)
    assert all(m.cls is not TransportTraceMiddleware for m in mws)
