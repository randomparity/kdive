"""DocExposureMiddleware: role-gates the doc-resource list and read (#940)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError
from mcp.shared.exceptions import McpError
from psycopg_pool import AsyncConnectionPool
from pydantic import AnyUrl

from kdive.mcp.assembly.app import build_app
from kdive.mcp.middleware import doc_exposure
from kdive.mcp.resources import registrar
from kdive.mcp.resources.registrar import DOC_RESOURCES
from kdive.security.authz.errors import AuthError
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.test_tool_index import _verifier

_OPERATOR_URI = "resource://kdive/docs/guide/agent-index-operator.md"
_ALL_URI = "resource://kdive/docs/guide/agent-index.md"
_ABSENT_URI = "resource://kdive/docs/guide/no-such-doc.md"


def _resources() -> list[SimpleNamespace]:
    return [SimpleNamespace(uri=_ALL_URI), SimpleNamespace(uri=_OPERATOR_URI)]


def _audience_map() -> dict[str, str]:
    return {_ALL_URI: "all", _OPERATOR_URI: "operator"}


class _Ctx:
    def __init__(self, platform_roles: set[str]) -> None:
        self.platform_roles = frozenset(platform_roles)


def _patch(
    monkeypatch: pytest.MonkeyPatch, ctx_or_exc: object
) -> doc_exposure.DocExposureMiddleware:
    monkeypatch.setattr(doc_exposure, "audience_by_uri", _audience_map)

    def _ctx() -> object:
        if isinstance(ctx_or_exc, Exception):
            raise ctx_or_exc
        return ctx_or_exc

    monkeypatch.setattr(doc_exposure, "request_context", _ctx)
    return doc_exposure.DocExposureMiddleware()


def test_list_hides_operator_doc_from_project_only_token(monkeypatch: pytest.MonkeyPatch) -> None:
    mw = _patch(monkeypatch, _Ctx(platform_roles=set()))

    async def _call_next(_c: object) -> list[SimpleNamespace]:
        return _resources()

    out = asyncio.run(mw.on_list_resources(SimpleNamespace(), _call_next))
    assert {str(r.uri) for r in out} == {_ALL_URI}


def test_list_shows_operator_doc_to_platform_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    mw = _patch(monkeypatch, _Ctx(platform_roles={"platform_auditor"}))

    async def _call_next(_c: object) -> list[SimpleNamespace]:
        return _resources()

    out = asyncio.run(mw.on_list_resources(SimpleNamespace(), _call_next))
    assert {str(r.uri) for r in out} == {_ALL_URI, _OPERATOR_URI}


def test_list_fails_closed_on_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mw = _patch(monkeypatch, AuthError("no token"))

    async def _call_next(_c: object) -> list[SimpleNamespace]:
        return _resources()

    out = asyncio.run(mw.on_list_resources(SimpleNamespace(), _call_next))
    assert {str(r.uri) for r in out} == {_ALL_URI}


def test_read_rejects_operator_doc_for_project_only_token(monkeypatch: pytest.MonkeyPatch) -> None:
    mw = _patch(monkeypatch, _Ctx(platform_roles=set()))
    ctx = SimpleNamespace(message=SimpleNamespace(uri=_OPERATOR_URI))

    async def _call_next(_c: object) -> str:
        return "should-not-reach"

    with pytest.raises(NotFoundError):
        asyncio.run(mw.on_read_resource(ctx, _call_next))


def test_read_rejects_operator_doc_on_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mw = _patch(monkeypatch, AuthError("no token"))
    ctx = SimpleNamespace(message=SimpleNamespace(uri=_OPERATOR_URI))

    async def _call_next(_c: object) -> str:
        return "should-not-reach"

    with pytest.raises(NotFoundError):
        asyncio.run(mw.on_read_resource(ctx, _call_next))


def test_read_denial_message_does_not_disclose_the_role_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The concealment is carried by the exception *text*, not just its class: FastMCP's
    # `_read_resource_mcp` interpolates `{e}` straight into the wire message, so a message
    # naming the gate would republish on the wire exactly what `on_list_resources` withheld
    # (ADR-0499). Reddens if the raise site says "requires a platform role".
    mw = _patch(monkeypatch, _Ctx(platform_roles=set()))
    ctx = SimpleNamespace(message=SimpleNamespace(uri=_OPERATOR_URI))

    async def _call_next(_c: object) -> str:
        return "should-not-reach"

    with pytest.raises(NotFoundError) as raised:
        asyncio.run(mw.on_read_resource(ctx, _call_next))
    # Asserted as an exact equality against FastMCP's own never-registered wording
    # (`server.py`'s `Unknown resource: {uri!r}`) rather than as a "role" substring check: the
    # equality subsumes every such check and pins the wording the next assertion depends on.
    assert str(raised.value) == f"Unknown resource: {_OPERATOR_URI!r}"


def test_read_allows_operator_doc_for_platform_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    mw = _patch(monkeypatch, _Ctx(platform_roles={"platform_operator"}))
    ctx = SimpleNamespace(message=SimpleNamespace(uri=_OPERATOR_URI))

    async def _call_next(_c: object) -> str:
        return "ok"

    assert asyncio.run(mw.on_read_resource(ctx, _call_next)) == "ok"


def test_read_allows_all_audience_doc_for_anyone(monkeypatch: pytest.MonkeyPatch) -> None:
    mw = _patch(monkeypatch, AuthError("no token"))
    ctx = SimpleNamespace(message=SimpleNamespace(uri=_ALL_URI))

    async def _call_next(_c: object) -> str:
        return "ok"

    assert asyncio.run(mw.on_read_resource(ctx, _call_next)) == "ok"


def _gated_app(monkeypatch: pytest.MonkeyPatch) -> FastMCP:
    """A real ``build_app`` whose doc set carries one ``audience="operator"`` fixture.

    No auth context is installed, so ``_is_elevated`` fails closed and the caller is a
    non-platform principal on every plane.
    """
    fixture = replace(
        DOC_RESOURCES[0], uri=_OPERATOR_URI, name="agent-index-operator", audience="operator"
    )
    monkeypatch.setattr(registrar, "DOC_RESOURCES", (*DOC_RESOURCES, fixture))
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    return build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())


def _wire_error(app: FastMCP, uri: str) -> Any:
    """The JSON-RPC ``ErrorData`` a ``resources/read`` of ``uri`` produces.

    Driven through ``_read_resource_mcp`` — the handler FastMCP registers with the MCP SDK —
    because that is the frame that decides the client-visible error code. The kind of
    exception the middleware raises only matters through what this frame does with it.
    """
    with pytest.raises(McpError) as raised:
        asyncio.run(app._read_resource_mcp(uri))
    return raised.value.error


def test_operator_doc_denied_end_to_end_through_built_app(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-platform caller must neither see nor read an operator-audience doc through the real
    # middleware chain. The read answers in the resources plane's own vocabulary — FastMCP's
    # `NotFoundError`, the same class its own component-auth filter yields — so the two arms
    # agree instead of the read confirming what the listing concealed (ADR-0499).
    app = _gated_app(monkeypatch)

    async def _listed() -> set[str]:
        return {str(r.uri) for r in await app.list_resources()}

    assert _OPERATOR_URI not in asyncio.run(_listed())

    with pytest.raises(NotFoundError):
        asyncio.run(app.read_resource(_OPERATOR_URI))


def test_denied_operator_doc_read_is_not_an_internal_error_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The defect #1682 actually produced: the bare `AuthorizationError` matched no `except` in
    # `_read_resource_mcp`, escaped to the SDK's `Server._handle_request` catch-all, and reached
    # the client as `ErrorData(code=0)` — the internal-error shape, not a denial. Reddens
    # against the unfixed raise site, which never reaches `McpError` at all.
    error = _wire_error(_gated_app(monkeypatch), _OPERATOR_URI)
    assert error.code == -32002
    # Independent of the code: `_read_resource_mcp` interpolates the exception into the wire
    # message, so a `NotFoundError` naming the gate would still carry code -32002 and leak.
    assert "role" not in error.message.lower()


def test_denied_and_absent_doc_reads_are_indistinguishable_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The no-leak property, asserted as an equality against real behaviour rather than as a
    # hand-written expectation: a gated doc and a URI that was never registered must produce
    # the same code and the same message modulo the URI itself. Otherwise `resources/read`
    # stays an existence oracle for a doc `on_list_resources` deliberately hid (ADR-0097's
    # no-leak invariant, applied to this plane by ADR-0499).
    app = _gated_app(monkeypatch)
    gated = _wire_error(app, _OPERATOR_URI)
    absent = _wire_error(app, _ABSENT_URI)

    assert gated.code == absent.code
    redacted = gated.message.replace(str(AnyUrl(_OPERATOR_URI)), "<uri>").replace(
        _OPERATOR_URI, "<uri>"
    )
    assert redacted == absent.message.replace(str(AnyUrl(_ABSENT_URI)), "<uri>").replace(
        _ABSENT_URI, "<uri>"
    )
    # Guards the redaction above from silently becoming a no-op: if the gated message stopped
    # naming the resource, the equality would degrade into comparing two constants and would
    # no longer be evidence that the URI-bearing part of the two answers matches.
    assert redacted != gated.message
