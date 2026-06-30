"""DocExposureMiddleware: role-gates the doc-resource list and read (#940)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.app import build_app
from kdive.mcp.middleware import doc_exposure
from kdive.mcp.resources import registrar
from kdive.mcp.resources.registrar import DOC_RESOURCES
from kdive.security.authz.errors import AuthError
from kdive.security.authz.rbac import AuthorizationError
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.test_tool_index import _verifier

_OPERATOR_URI = "resource://kdive/docs/guide/agent-index-operator.md"
_ALL_URI = "resource://kdive/docs/guide/agent-index.md"


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

    with pytest.raises(AuthorizationError):
        asyncio.run(mw.on_read_resource(ctx, _call_next))


def test_read_rejects_operator_doc_on_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mw = _patch(monkeypatch, AuthError("no token"))
    ctx = SimpleNamespace(message=SimpleNamespace(uri=_OPERATOR_URI))

    async def _call_next(_c: object) -> str:
        return "should-not-reach"

    with pytest.raises(AuthorizationError):
        asyncio.run(mw.on_read_resource(ctx, _call_next))


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


def test_operator_doc_denied_end_to_end_through_built_app(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-platform caller (no auth context => fail-closed) must neither see nor read an
    # operator-audience doc through the real middleware chain, and the read must raise the
    # repo's AuthorizationError (a clean denial), not surface as an opaque internal error.
    operator_uri = "resource://kdive/docs/guide/agent-index-operator.md"
    fixture = replace(
        DOC_RESOURCES[0], uri=operator_uri, name="agent-index-operator", audience="operator"
    )
    monkeypatch.setattr(registrar, "DOC_RESOURCES", (*DOC_RESOURCES, fixture))
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _listed() -> set[str]:
        return {str(r.uri) for r in await app.list_resources()}

    listed = asyncio.run(_listed())
    assert operator_uri not in listed

    with pytest.raises(AuthorizationError):
        asyncio.run(app.read_resource(operator_uri))
