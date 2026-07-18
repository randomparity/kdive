"""ASGI middleware that turns a bare-JWT ``Authorization`` header into a clear 401 (ADR-0380).

FastMCP's vendored auth path returns a misleading "token invalid, expired, or no
longer recognized" 401 when a client sends `Authorization` as a bare JWT *without*
the `Bearer ` scheme prefix: the MCP SDK's `BearerAuthBackend.authenticate`
short-circuits to `None` before verifying the token, and FastMCP's
`RequireAuthMiddleware` hard-codes the misleading description for every 401. Both
live in vendored dependencies (#1268).

`RequireAuthMiddleware` is the streamable-HTTP *route endpoint* wrapper, not a
Starlette middleware, so an ASGI middleware injected into the FastMCP HTTP app's
`middleware=` list runs ahead of it. This middleware detects a bare-JWT header and
short-circuits with an accurate, actionable 401 telling the client to add the
`Bearer ` prefix; every other value passes through untouched to the vendored
verifier, so a genuine auth failure is never shadowed.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_BARE_TOKEN_DESCRIPTION = (
    "The Authorization header appears to contain a bare token with no auth scheme. "
    "Prefix the token with 'Bearer ' (e.g. \"Authorization: Bearer <token>\") per "
    "RFC 6750; the server requires the scheme prefix and does not accept a bare token."
)


def _looks_like_bare_jwt(value: str) -> bool:
    """Whether ``value`` is a JWT with no auth-scheme prefix.

    A JWS compact token is three dot-separated base64url segments whose header starts
    ``eyJ`` and contains no whitespace; a scheme-prefixed header (``Bearer eyJ...``)
    always contains a space. The check is deliberately conservative so it never
    shadows a genuine auth failure on some other header shape.
    """
    token = value.strip()
    return (
        bool(token)
        and not token[:1].isspace()
        and " " not in token
        and "\t" not in token
        and token.startswith("eyJ")
        and token.count(".") == 2
    )


def _authorization_header(scope: Scope) -> str | None:
    """The raw ``Authorization`` header value from an ASGI scope, or ``None``."""
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == b"authorization":
            return raw_value.decode("latin-1")
    return None


async def _send_bare_token_error(send: Send) -> None:
    """Emit the RFC 6750-shaped 401 that names the missing scheme prefix."""
    body = json.dumps(
        {"error": "invalid_request", "error_description": _BARE_TOKEN_DESCRIPTION}
    ).encode()
    www_authenticate = (
        f'Bearer error="invalid_request", error_description="{_BARE_TOKEN_DESCRIPTION}"'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", www_authenticate.encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BareBearerHintMiddleware:
    """Short-circuit a bare-JWT ``Authorization`` header with an accurate 401 (ADR-0380)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Intercept a bare-JWT HTTP request; pass everything else through."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        header = _authorization_header(scope)
        if header is not None and _looks_like_bare_jwt(header):
            await _send_bare_token_error(send)
            return
        await self.app(scope, receive, send)
