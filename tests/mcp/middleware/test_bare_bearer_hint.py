"""ASGI bare-JWT hint middleware: detection matrix and short-circuit behavior (ADR-0380)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kdive.mcp.middleware.bare_bearer_hint import (
    BareBearerHintMiddleware,
    _looks_like_bare_jwt,
)

# A JWS-shaped token (header.payload.signature): starts `eyJ`, three segments; never verified.
_BARE_JWT = "eyJhbG.eyJzdWIiOiJ1In0.c2ln"  # pragma: allowlist secret


def _http_scope(*headers: tuple[bytes, bytes]) -> dict[str, Any]:
    return {"type": "http", "headers": list(headers)}


class _Recorder:
    """Collects ASGI send events and whether the wrapped app was invoked."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.downstream_called = False

    async def app(self, scope: Any, receive: Any, send: Any) -> None:
        self.downstream_called = True

    async def send(self, message: dict[str, Any]) -> None:
        self.events.append(message)

    async def receive(self) -> dict[str, Any]:  # pragma: no cover - never awaited here
        return {"type": "http.request"}

    def status(self) -> int:
        return next(e["status"] for e in self.events if e["type"] == "http.response.start")

    def body(self) -> dict[str, Any]:
        raw = next(e["body"] for e in self.events if e["type"] == "http.response.body")
        return json.loads(raw)

    def header(self, name: bytes) -> bytes | None:
        start = next(e for e in self.events if e["type"] == "http.response.start")
        return next((v for k, v in start["headers"] if k.lower() == name), None)


@pytest.mark.parametrize(
    "value",
    [
        _BARE_JWT,
        f"  {_BARE_JWT}  ",  # surrounding whitespace still a bare token
    ],
)
def test_looks_like_bare_jwt_true(value: str) -> None:
    assert _looks_like_bare_jwt(value) is True


@pytest.mark.parametrize(
    "value",
    [
        f"Bearer {_BARE_JWT}",  # correct scheme prefix
        f"bearer {_BARE_JWT}",  # case-insensitive scheme prefix
        f"Basic {_BARE_JWT}",  # a different scheme
        "Basic dXNlcjpwYXNz",  # base64 basic creds, not a JWT
        "eyJhbGciOiJSUzI1NiJ9",  # only one segment, not a full JWT
        "eyJhbG.eyJzdWI",  # two segments only, not a full JWT
        "not-a-token-at-all",
        "",
        "   ",
    ],
)
def test_looks_like_bare_jwt_false(value: str) -> None:
    assert _looks_like_bare_jwt(value) is False


def test_bare_jwt_short_circuits_with_scheme_hint() -> None:
    rec = _Recorder()
    mw = BareBearerHintMiddleware(rec.app)

    asyncio.run(mw(_http_scope((b"authorization", _BARE_JWT.encode())), rec.receive, rec.send))

    assert rec.downstream_called is False
    assert rec.status() == 401
    body = rec.body()
    assert body["error"] == "invalid_request"
    assert "Bearer " in body["error_description"]
    assert "invalid" not in body["error_description"].lower()  # no misleading claim
    www = rec.header(b"www-authenticate")
    assert www is not None and www.startswith(b"Bearer ")


def test_scheme_prefixed_token_passes_through() -> None:
    rec = _Recorder()
    mw = BareBearerHintMiddleware(rec.app)

    asyncio.run(
        mw(
            _http_scope((b"authorization", f"Bearer {_BARE_JWT}".encode())),
            rec.receive,
            rec.send,
        )
    )

    assert rec.downstream_called is True
    assert rec.events == []  # the middleware sent nothing itself


def test_missing_header_passes_through() -> None:
    rec = _Recorder()
    mw = BareBearerHintMiddleware(rec.app)

    asyncio.run(mw(_http_scope(), rec.receive, rec.send))

    assert rec.downstream_called is True


def test_non_http_scope_passes_through() -> None:
    rec = _Recorder()
    mw = BareBearerHintMiddleware(rec.app)

    asyncio.run(mw({"type": "lifespan"}, rec.receive, rec.send))

    assert rec.downstream_called is True
