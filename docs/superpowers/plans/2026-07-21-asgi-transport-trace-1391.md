# Opt-in ASGI Transport-Trace Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in ASGI middleware that logs one structured line per HTTP request so an operator can reconstruct an MCP session lifecycle (initialize → requests → 404 → re-initialize-or-not) from server logs.

**Architecture:** A pure ASGI callable `TransportTraceMiddleware` wired outermost through the existing `server_http_middleware()` seam (ADR-0380), gated by a new `KDIVE_MCP_TRACE` flag. It wraps `send` to capture the response status at `http.response.start`, emitting one INFO record via a dedicated `kdive.mcp.transport_trace` logger (own `INFO` level, so it is independent of `KDIVE_LOG_LEVEL`). Per-request state is call-local. The enable flag is resolved once in `__main__` and threaded as a bool.

**Tech Stack:** Python 3.14, FastMCP/Starlette ASGI, stdlib `logging`, `pytest`, `httpx.ASGITransport`.

Spec: `docs/specs/2026-07-21-asgi-transport-trace-1391-design.md`. ADR: `docs/adr/0417-opt-in-asgi-transport-trace.md`.

## Global Constraints

- Python 3.14, `uv`. Ruff line length 100, lint set `E,F,I,UP,B,SIM`. `ty` strict (whole tree, `src`+`tests`).
- Absolute imports only (`kdive....`), no relative imports. Google-style docstrings on public APIs.
- Doc-style: plain, factual prose; never "critical/robust/comprehensive/elegant"; "Milestone" not "Sprint".
- Guardrail suite: `just ci` = `lint type lock-check lint-shell lint-ansible test-ansible lint-workflows check-mermaid docs-links docs-paths served-doc-links adr-status-check docs-check config-docs-check config-guard env-docs-check schema-guard container-arch-check resources-docs-check doc-constants-check chart-version-check test`. CI runs each recipe individually.
- Adding a `KDIVE_*` registry `Setting` requires regenerating the config reference: `just config-docs` then commit `docs/reference/config.md` (guarded by `config-docs-check`).
- Redaction invariant: `Authorization`/bearer logged as presence only, never the value.
- Run a single test: `uv run python -m pytest <path>::<name> -q`.

---

### Task 1: Add the `KDIVE_MCP_TRACE` setting and `mcp_trace_enabled()` helper

**Files:**
- Modify: `src/kdive/config/core_settings.py` (add `MCP_TRACE` Setting next to `COMPACT_RESPONSES`, and to the `CORE_SETTINGS` list ~line 705-713)
- Create: `src/kdive/mcp/middleware/transport_trace.py` (the `mcp_trace_enabled()` helper lives here, co-located with the middleware added in Task 2)
- Test: `tests/config/test_core_settings.py` (or nearest existing settings test) and `tests/mcp/middleware/test_transport_trace.py`
- Regenerate: `docs/reference/config.md` via `just config-docs`

**Interfaces:**
- Produces: `kdive.config.core_settings.MCP_TRACE: Setting[str]` (name `KDIVE_MCP_TRACE`, `parse=_str`, no default, `group="logging"`, `processes=_SERVER`); `kdive.mcp.middleware.transport_trace.mcp_trace_enabled() -> bool`.

- [ ] **Step 1: Write the failing test for the truthy resolver**

Create `tests/mcp/middleware/test_transport_trace.py`:

```python
"""Tests for the opt-in ASGI transport-trace middleware (ADR-0417)."""

from __future__ import annotations

import pytest

from kdive import config
from kdive.mcp.middleware.transport_trace import mcp_trace_enabled


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, False), ("0", False), ("false", False), ("off", False), ("no", False),
     ("1", True), ("true", True), ("YES", True), ("On", True)],
)
def test_mcp_trace_enabled_resolves_truthy_set(monkeypatch, raw, expected) -> None:
    if raw is None:
        monkeypatch.delenv("KDIVE_MCP_TRACE", raising=False)
    else:
        monkeypatch.setenv("KDIVE_MCP_TRACE", raw)
    config.load()
    assert mcp_trace_enabled() is expected
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/mcp/middleware/test_transport_trace.py::test_mcp_trace_enabled_resolves_truthy_set -q`
Expected: FAIL — `ModuleNotFoundError`/`ImportError` for `transport_trace` or `MCP_TRACE`.

- [ ] **Step 3: Add the `MCP_TRACE` Setting**

In `src/kdive/config/core_settings.py`, after the `COMPACT_RESPONSES` setting, add:

```python
MCP_TRACE = Setting(
    name="KDIVE_MCP_TRACE",
    parse=_str,
    group="logging",
    processes=_SERVER,
    help="Presence (1/true/yes) enables opt-in ASGI transport-trace logging (default off).",
)
```

Add `MCP_TRACE,` to the `CORE_SETTINGS` list (the tuple/list ending ~line 713, alongside `COMPACT_RESPONSES`).

- [ ] **Step 4: Create the middleware module with the helper**

Create `src/kdive/mcp/middleware/transport_trace.py` with the header and helper (the middleware class is added in Task 2):

```python
"""Opt-in ASGI transport-trace middleware for MCP session/HTTP debugging (ADR-0417)."""

from __future__ import annotations

import logging

from kdive import config
from kdive.config.core_settings import MCP_TRACE

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_log = logging.getLogger("kdive.mcp.transport_trace")
_log.setLevel(logging.INFO)


def mcp_trace_enabled() -> bool:
    """Whether opt-in transport tracing is enabled (``KDIVE_MCP_TRACE`` truthy)."""
    raw = config.get(MCP_TRACE)
    return bool(raw) and raw.strip().lower() in _TRUTHY
```

- [ ] **Step 5: Run the resolver test to verify it passes**

Run: `uv run python -m pytest tests/mcp/middleware/test_transport_trace.py::test_mcp_trace_enabled_resolves_truthy_set -q`
Expected: PASS (all parametrized cases).

- [ ] **Step 6: Regenerate the config reference**

Run: `just config-docs` then `just config-docs-check`
Expected: `docs/reference/config.md` now lists `KDIVE_MCP_TRACE`; check passes.

- [ ] **Step 7: Commit**

```bash
git add src/kdive/config/core_settings.py src/kdive/mcp/middleware/transport_trace.py \
        tests/mcp/middleware/test_transport_trace.py docs/reference/config.md
git commit -m "feat(mcp): add KDIVE_MCP_TRACE setting and truthy resolver"
```

---

### Task 2: Implement `TransportTraceMiddleware` with its unit-test suite

**Files:**
- Modify: `src/kdive/mcp/middleware/transport_trace.py` (add the class + a header scanner)
- Test: `tests/mcp/middleware/test_transport_trace.py`

**Interfaces:**
- Consumes: `mcp_trace_enabled` (Task 1), `_log` (module logger, level `INFO`).
- Produces: `class TransportTraceMiddleware` — `__init__(self, app: ASGIApp)`, `async __call__(self, scope, receive, send)`. Emits one INFO record with `extra` keys `method, path, mcp_session_id, mcp_session_id_present, mcp_protocol_version, authorization_present, status, duration_ms`.

- [ ] **Step 1: Write the failing happy-path + redaction + presence tests**

Append to `tests/mcp/middleware/test_transport_trace.py`:

```python
import asyncio
import logging

from kdive.mcp.middleware.transport_trace import TransportTraceMiddleware

TRACE_LOGGER = "kdive.mcp.transport_trace"


def _scope(headers: list[tuple[bytes, bytes]], method: str = "POST", path: str = "/mcp") -> dict:
    return {"type": "http", "method": method, "path": path, "headers": headers}


async def _ok_app(scope, receive, send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == TRACE_LOGGER]


def test_happy_path_logs_all_fields(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    headers = [(b"mcp-session-id", b"sess-abc"), (b"mcp-protocol-version", b"2025-06-18")]

    async def run() -> None:
        await TransportTraceMiddleware(_ok_app)(_scope(headers), None, _noop_send)

    asyncio.run(run())
    (rec,) = _records(caplog)
    assert rec.status == 200
    assert rec.method == "POST"
    assert rec.path == "/mcp"
    assert rec.mcp_session_id == "sess-abc"
    assert rec.mcp_session_id_present is True
    assert rec.mcp_protocol_version == "2025-06-18"
    assert isinstance(rec.duration_ms, float) and rec.duration_ms >= 0.0


async def _noop_send(message) -> None:
    return None


def test_no_session_header(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    asyncio.run(TransportTraceMiddleware(_ok_app)(_scope([]), None, _noop_send))
    (rec,) = _records(caplog)
    assert rec.mcp_session_id_present is False
    assert rec.mcp_session_id is None


def test_authorization_logged_presence_only(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    token = "Bearer super-secret-token-value"
    headers = [(b"authorization", token.encode())]
    asyncio.run(TransportTraceMiddleware(_ok_app)(_scope(headers), None, _noop_send))
    (rec,) = _records(caplog)
    assert rec.authorization_present is True
    # The token value must never appear in the record.
    assert "super-secret-token-value" not in rec.getMessage()
    assert "super-secret-token-value" not in str(rec.__dict__)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m pytest tests/mcp/middleware/test_transport_trace.py -q`
Expected: FAIL — `TransportTraceMiddleware` not defined.

- [ ] **Step 3: Implement the middleware**

Append to `src/kdive/mcp/middleware/transport_trace.py`:

```python
import time
from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _header(scope: Scope, name: bytes) -> str | None:
    """The value of header ``name`` (lowercase bytes) from an ASGI scope, or ``None``."""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == name:
            return raw_value.decode("latin-1")
    return None


class TransportTraceMiddleware:
    """Log one structured line per HTTP request for MCP transport debugging (ADR-0417)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Trace one HTTP request; pass non-HTTP scopes straight through."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        start = time.monotonic()
        emitted = False
        session_id = _header(scope, b"mcp-session-id")
        fields: dict[str, Any] = {
            "method": scope.get("method", ""),
            "path": scope.get("path", ""),
            "mcp_session_id": session_id,
            "mcp_session_id_present": session_id is not None,
            "mcp_protocol_version": _header(scope, b"mcp-protocol-version"),
            "authorization_present": _header(scope, b"authorization") is not None,
        }

        def _emit(status: int | None) -> None:
            nonlocal emitted
            emitted = True
            duration_ms = (time.monotonic() - start) * 1000.0
            _log.info(
                "mcp transport %s %s -> %s",
                fields["method"],
                fields["path"],
                status,
                extra={**fields, "status": status, "duration_ms": duration_ms},
            )

        async def _wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start" and not emitted:
                _emit(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, _wrapped_send)
        finally:
            if not emitted:
                _emit(None)
```

- [ ] **Step 4: Run to verify the three tests pass**

Run: `uv run python -m pytest tests/mcp/middleware/test_transport_trace.py -q`
Expected: PASS.

- [ ] **Step 5: Write the failure/cancel/disconnect/exactly-once/non-http tests**

Append:

```python
def test_raise_before_response_start_logs_status_none(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)

    async def boom(scope, receive, send) -> None:
        raise RuntimeError("dispatch failed")

    with pytest.raises(RuntimeError):
        asyncio.run(TransportTraceMiddleware(boom)(_scope([]), None, _noop_send))
    (rec,) = _records(caplog)
    assert rec.status is None
    assert isinstance(rec.duration_ms, float) and rec.duration_ms >= 0.0


def test_cancel_before_response_start_logs_status_none(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)

    async def cancelled(scope, receive, send) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(TransportTraceMiddleware(cancelled)(_scope([]), None, _noop_send))
    (rec,) = _records(caplog)
    assert rec.status is None
    assert isinstance(rec.duration_ms, float)


def test_post_header_disconnect_keeps_opening_line_only(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)

    async def stream_then_cancel(scope, receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(TransportTraceMiddleware(stream_then_cancel)(_scope([]), None, _noop_send))
    (rec,) = _records(caplog)  # exactly one record
    assert rec.status == 200  # the opening line; no second (close) line


def test_non_http_scope_passthrough(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    seen = {}

    async def app(scope, receive, send) -> None:
        seen["type"] = scope["type"]

    asyncio.run(TransportTraceMiddleware(app)({"type": "lifespan"}, None, _noop_send))
    assert seen["type"] == "lifespan"
    assert _records(caplog) == []
```

- [ ] **Step 6: Run to verify they pass**

Run: `uv run python -m pytest tests/mcp/middleware/test_transport_trace.py -q`
Expected: PASS.

- [ ] **Step 7: Write the level-independence, bridge-NOTSET, and concurrency tests**

Append:

```python
def test_level_independent_of_raised_root_floor(caplog) -> None:
    # Mimic KDIVE_LOG_LEVEL=warning: raise the ROOT logger floor.
    logging.getLogger().setLevel(logging.WARNING)
    try:
        caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
        asyncio.run(TransportTraceMiddleware(_ok_app)(_scope([]), None, _noop_send))
        assert len(_records(caplog)) == 1
    finally:
        logging.getLogger().setLevel(logging.WARNING)  # leave as harness had it


def test_otel_bridge_handler_stays_notset() -> None:
    # Regression guard for the documented level-independence invariant: the OTel
    # LoggingHandler that facade installs must remain at NOTSET so a propagated INFO
    # record is not gated by a handler level.
    from opentelemetry.sdk._logs import LoggerProvider

    from kdive.observability.facade import _bridge_root_logger

    root = logging.getLogger()
    before = list(root.handlers)
    try:
        _bridge_root_logger(LoggerProvider(), "warning")
        added = [h for h in root.handlers if h not in before]
        assert added, "expected the bridge handler to be installed"
        assert all(h.level == logging.NOTSET for h in added)
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)


def test_concurrent_requests_do_not_share_state(caplog) -> None:
    caplog.set_level(logging.INFO, logger=TRACE_LOGGER)
    b_entered = asyncio.Event()
    mw = TransportTraceMiddleware  # one instance per request below is wrong; share one

    async def app_a(scope, receive, send) -> None:
        await b_entered.wait()  # A suspends until B has entered and captured its state
        await send({"type": "http.response.start", "status": 201, "headers": []})

    async def app_b(scope, receive, send) -> None:
        b_entered.set()
        await send({"type": "http.response.start", "status": 202, "headers": []})

    trace_a = mw(app_a)
    trace_b = mw(app_b)

    async def run() -> None:
        await asyncio.gather(
            trace_a(_scope([(b"mcp-session-id", b"A")]), None, _noop_send),
            trace_b(_scope([(b"mcp-session-id", b"B")]), None, _noop_send),
        )

    asyncio.run(run())
    by_session = {r.mcp_session_id: r.status for r in _records(caplog)}
    assert by_session == {"A": 201, "B": 202}
```

Note: distinct `TransportTraceMiddleware` instances are fine — the invariant under test is that per-request state is `__call__`-local (nonlocal in the wrapper), which the code satisfies by design. The gather + `b_entered` gate forces A's `__call__` to be suspended while B runs, so a hypothetical `self`-attribute implementation would cross-contaminate and fail this assertion.

- [ ] **Step 8: Run the full module suite**

Run: `uv run python -m pytest tests/mcp/middleware/test_transport_trace.py -q`
Expected: PASS.

- [ ] **Step 9: Lint + type this module**

Run: `just lint && just type`
Expected: clean. (Fix any `ty` complaint about `extra=` dict typing by keeping `fields: dict[str, Any]`.)

- [ ] **Step 10: Commit**

```bash
git add src/kdive/mcp/middleware/transport_trace.py tests/mcp/middleware/test_transport_trace.py
git commit -m "feat(mcp): implement TransportTraceMiddleware with unit suite"
```

---

### Task 3: Wire the middleware through the seam and thread the enable flag

**Files:**
- Modify: `src/kdive/processes/server.py` (`server_http_middleware(trace_enabled: bool)`, `run_server(..., *, trace_enabled)`, `serve_mcp`)
- Modify: `src/kdive/__main__.py` (`_handle_server` resolves `mcp_trace_enabled()` and threads it)
- Test: `tests/mcp/middleware/test_transport_trace.py` (seam tests), and an existing `__main__`/server test if present

**Interfaces:**
- Consumes: `TransportTraceMiddleware`, `mcp_trace_enabled` (Tasks 1-2).
- Produces: `server_http_middleware(trace_enabled: bool) -> list[Middleware]` (was no-arg); `run_server(host, port, secret_registry, telemetry, *, trace_enabled: bool)`.

- [ ] **Step 1: Write the failing seam test**

Append to `tests/mcp/middleware/test_transport_trace.py`:

```python
from kdive.mcp.middleware.transport_trace import TransportTraceMiddleware as _TTM
from kdive.processes.server import server_http_middleware


def test_seam_includes_trace_outermost_when_enabled() -> None:
    mws = server_http_middleware(trace_enabled=True)
    assert mws[0].cls is _TTM  # first entry == outermost wrapper


def test_seam_excludes_trace_when_disabled() -> None:
    mws = server_http_middleware(trace_enabled=False)
    assert all(m.cls is not _TTM for m in mws)
```

(`starlette.middleware.Middleware` exposes the class as `.cls`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run python -m pytest tests/mcp/middleware/test_transport_trace.py -k seam -q`
Expected: FAIL — `server_http_middleware()` takes no `trace_enabled` argument.

- [ ] **Step 3: Update `server_http_middleware`**

In `src/kdive/processes/server.py`, replace the function and thread the flag:

```python
from kdive.mcp.middleware.transport_trace import TransportTraceMiddleware


def server_http_middleware(trace_enabled: bool) -> list[Middleware]:
    """ASGI middleware injected ahead of FastMCP's vendored endpoints (ADR-0380, ADR-0417).

    When ``trace_enabled`` (``KDIVE_MCP_TRACE``), ``TransportTraceMiddleware`` is prepended
    so it is the outermost wrapper and observes every HTTP request — including ones
    ``BareBearerHintMiddleware`` 401s and ones the transport 404s. ``BareBearerHintMiddleware``
    turns a bare-JWT ``Authorization`` header into an accurate 401.
    """
    middleware = [Middleware(BareBearerHintMiddleware)]
    if trace_enabled:
        middleware.insert(0, Middleware(TransportTraceMiddleware))
    return middleware
```

Update `run_server` to accept and pass the flag:

```python
async def run_server(
    host: str,
    port: int,
    secret_registry: SecretRegistry,
    telemetry: Telemetry,
    *,
    trace_enabled: bool,
) -> None:
    ...
    async def serve_mcp(pool, heartbeat, probe) -> None:
        del heartbeat, probe
        app = build_app(pool, secret_registry=secret_registry)
        await app.run_async(
            transport="http",
            host=host,
            port=port,
            uvicorn_config=server_uvicorn_config(),
            middleware=server_http_middleware(trace_enabled=trace_enabled),
        )
    ...
```

- [ ] **Step 4: Thread the flag from `__main__`**

In `src/kdive/__main__.py`, import and resolve in `_handle_server`:

```python
from kdive.mcp.middleware.transport_trace import mcp_trace_enabled
...
def _handle_server(args, secret_registry, telemetry) -> None:
    del args
    initialized = _require_telemetry("server", telemetry)
    host = config.require(HTTP_HOST)
    port = config.require(HTTP_PORT)
    asyncio.run(
        _run_server(host, port, initialized, secret_registry=secret_registry)
        if False else  # placeholder guard removed below
        _run_server(host, port, secret_registry, initialized, trace_enabled=mcp_trace_enabled())
    )
```

Simplify to the single real call (do not keep the placeholder):

```python
    asyncio.run(
        _run_server(host, port, secret_registry, initialized, trace_enabled=mcp_trace_enabled())
    )
```

(`_run_server` is the aliased `run_server`; the keyword `trace_enabled` matches the new signature.)

- [ ] **Step 5: Run the seam tests + the whole trace module**

Run: `uv run python -m pytest tests/mcp/middleware/test_transport_trace.py -q`
Expected: PASS.

- [ ] **Step 6: Fix any other `server_http_middleware()` call sites**

Run: `rg -n "server_http_middleware\(" src tests`
Every call must now pass `trace_enabled=`. Update `tests/mcp/core/test_bare_bearer_ordering.py` (`server_http_middleware()` → `server_http_middleware(trace_enabled=False)`).

- [ ] **Step 7: Run lint + type + the affected suites**

Run: `just lint && just type && uv run python -m pytest tests/mcp/core/test_bare_bearer_ordering.py tests/mcp/middleware/test_transport_trace.py -q`
Expected: clean + PASS.

- [ ] **Step 8: Commit**

```bash
git add src/kdive/processes/server.py src/kdive/__main__.py \
        tests/mcp/middleware/test_transport_trace.py tests/mcp/core/test_bare_bearer_ordering.py
git commit -m "feat(mcp): wire transport trace through the middleware seam"
```

---

### Task 4: Runtime composition — prove trace runs outermost over 401 and the FastMCP transport 404

**Files:**
- Test: `tests/mcp/core/test_transport_trace_ordering.py` (mirrors `test_bare_bearer_ordering.py`)

**Interfaces:**
- Consumes: `build_app`, `server_http_middleware(trace_enabled=True)`, `httpx.ASGITransport`.

- [ ] **Step 1: Write the failing 401 + 404 ordering tests**

Create `tests/mcp/core/test_transport_trace_ordering.py`:

```python
"""End-to-end: the transport trace runs outermost over the real FastMCP http_app (ADR-0417).

Drives the real assembled app in-process (no live server, no DB — the pool is unopened and
the auth/session layers reject before any DB access), proving the trace observes both a
peer-middleware 401 and FastMCP's vendored transport 404 session-miss.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.assembly.app import build_app
from kdive.processes.server import server_http_middleware
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair, mint

TRACE_LOGGER = "kdive.mcp.transport_trace"
_MCP_PATH = "/mcp"


def _post(headers: dict[str, str]) -> tuple[httpx.Response, list[logging.LogRecord]]:
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    http_app = app.http_app(middleware=server_http_middleware(trace_enabled=True))

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=http_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", follow_redirects=True
        ) as client:
            return await client.post(
                _MCP_PATH,
                headers={"Accept": "application/json, text/event-stream", **headers},
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )

    handler_records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = handler_records.append  # type: ignore[method-assign]
    trace_log = logging.getLogger(TRACE_LOGGER)
    trace_log.addHandler(handler)
    try:
        resp = asyncio.run(_run())
    finally:
        trace_log.removeHandler(handler)
    return resp, [r for r in handler_records if r.name == TRACE_LOGGER]


def test_trace_observes_bare_bearer_401() -> None:
    resp, records = _post({"Authorization": mint(make_keypair())})  # bare JWT → 401
    assert resp.status_code == 401
    assert any(r.status == 401 for r in records)


def test_trace_observes_transport_session_miss_404() -> None:
    valid = f"Bearer {mint(make_keypair())}"
    resp, records = _post({"Authorization": valid, "Mcp-Session-Id": "does-not-exist"})
    assert resp.status_code == 404
    assert any(r.status == 404 for r in records)
```

- [ ] **Step 2: Run to verify the 404 assumption holds (and the tests fail only if wiring is wrong)**

Run: `uv run python -m pytest tests/mcp/core/test_transport_trace_ordering.py -q`
Expected initially: PASS if the wiring from Task 3 is correct. If `test_trace_observes_transport_session_miss_404` returns a status other than 404, inspect the real response (`resp.status_code`) and adjust the assertion to the transport's actual session-miss status **and** record the observed status in the test docstring — the point is that the trace records whatever status the transport returns for an unknown session. If FastMCP does not surface the session-miss as an observable `http.response.start` at all (no trace record), fall back to the ASGI stack-position assertion below.

- [ ] **Step 3: (Fallback only, if Step 2 shows no trace record for the 404)** Add a stack-position assertion

If and only if the transport 404 is not observable in-process, add:

```python
def test_trace_wraps_fastmcp_transport_mount() -> None:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, secret_registry=SecretRegistry())
    http_app = app.http_app(middleware=server_http_middleware(trace_enabled=True))
    # The outermost user middleware in the assembled Starlette app is the trace middleware.
    from kdive.mcp.middleware.transport_trace import TransportTraceMiddleware
    assert any(
        getattr(m, "cls", None) is TransportTraceMiddleware
        for m in getattr(http_app, "user_middleware", [])
    )
```

Keep this as a PR gate so the "outermost over FastMCP's transport" assumption is always checked.

- [ ] **Step 4: Run + lint + type**

Run: `just lint && just type && uv run python -m pytest tests/mcp/core/test_transport_trace_ordering.py -q`
Expected: clean + PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/mcp/core/test_transport_trace_ordering.py
git commit -m "test(mcp): prove transport trace observes 401 and transport 404"
```

---

### Task 5: Operator documentation

**Files:**
- Modify: the operator observability/runbook doc that documents `KDIVE_*` logging knobs (find via `rg -l "KDIVE_LOG_LEVEL" docs/`), adding a short "Transport tracing" subsection.

- [ ] **Step 1: Locate the doc**

Run: `rg -ln "KDIVE_LOG_LEVEL" docs/ | head`
Pick the operator-facing observability/logging doc (e.g. under `docs/operating/`).

- [ ] **Step 2: Add a `KDIVE_MCP_TRACE` subsection**

Add prose (plain, factual — no "critical/robust/comprehensive"): what it does (one INFO `kdive.mcp.transport_trace` line per HTTP request with method/path/session-id/protocol-version/status/duration), that it is off by default and debug-only, that it needs a **restart** to take effect (which drops in-memory sessions — arm it *before* reproducing an incident), that it logs the session id but never the bearer value, and that the record is level-independent (emits regardless of `KDIVE_LOG_LEVEL`). Reference ADR-0417.

- [ ] **Step 3: Run doc guards**

Run: `just docs-links && just check-mermaid && just docs-paths`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add docs/
git commit -m "docs(operating): document KDIVE_MCP_TRACE transport tracing"
```

---

### Final verification

- [ ] **Run the full guardrail suite**

Run: `just ci`
Expected: green. In particular `config-docs-check` (Task 1 regen), `docs-check`, and `test` must pass.

- [ ] **Confirm acceptance criteria** against `docs/specs/2026-07-21-asgi-transport-trace-1391-design.md` — AC1-AC7 each map to a committed test or the config-reference regen.

## Self-review notes

- **Spec coverage:** AC1→Task 2 happy-path; AC2→Task 1 resolver + Task 3 seam-excluded; AC3→Task 2 redaction; AC4→Task 4 (401 + 404 through the real stack); AC5→Task 2 raise/cancel; AC6→Task 1 `config-docs`; AC7→Final `just ci`. Level-independence + bridge-NOTSET + concurrency + post-header-disconnect all in Task 2. Operator doc in Task 5.
- **Live proof:** the `live_stack` end-to-end 404 assertion in the spec is additional (non-PR-gate) proof; not required for merge because Task 4 proves the same in-process. If a `live_stack` test is added, gate it with the existing `live_stack` marker so `just test`/`just ci` skip it cleanly.
- **Type note:** `logging.Handler().emit = list.append` in Task 4 needs a `# type: ignore[method-assign]`; prefer a small `logging.handlers.MemoryHandler` or a `caplog`-with-propagation approach if `ty` objects.
