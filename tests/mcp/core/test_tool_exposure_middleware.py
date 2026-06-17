"""`ToolExposureMiddleware`: per-connection `list_tools` filtering (#506, ADR-0148).

The filter reads the connection's `RequestContext` (injected here by monkeypatching
`current_context`) and reduces the advertised catalog. It is advisory and fail-open: a
missing/invalid context or any internal error returns the unfiltered catalog so tool
discovery never breaks. The end-to-end transport proof (that the token resolves inside
`on_list_tools` over real HTTP) is the live-tier assertion in
`tests/integration/test_wire_harness.py` — the in-memory transport carries no token.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import pytest

from kdive.mcp.middleware import ToolExposureMiddleware
from kdive.security.authz.context import RequestContext
from kdive.security.authz.errors import AuthError
from kdive.security.authz.rbac import PlatformRole, Role


@dataclass
class _Tool:
    name: str


_ALL = [
    _Tool("projects.list"),  # public
    _Tool("jobs.get"),  # project viewer
    _Tool("allocations.request"),  # project operator
    _Tool("control.force_crash"),  # project admin
    _Tool("ops.reconcile_now"),  # platform operator
]


async def _passthrough(_ctx: object) -> Sequence[_Tool]:
    return _ALL


def _run_filter(mw: ToolExposureMiddleware) -> set[str]:
    out = asyncio.run(mw.on_list_tools(object(), _passthrough))
    return {t.name for t in out}


def _ctx(
    *, roles: dict[str, Role] | None = None, platform: frozenset[PlatformRole] = frozenset()
) -> RequestContext:
    roles = roles or {}
    return RequestContext(
        principal="p",
        agent_session=None,
        projects=tuple(roles),
        roles=roles,
        platform_roles=platform,
    )


def test_viewer_catalog_is_reduced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kdive.mcp.middleware.current_context", lambda: _ctx(roles={"a": Role.VIEWER})
    )
    names = _run_filter(ToolExposureMiddleware())
    assert names == {"projects.list", "jobs.get"}
    assert names < {t.name for t in _ALL}


def test_operator_sees_operator_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kdive.mcp.middleware.current_context", lambda: _ctx(roles={"a": Role.OPERATOR})
    )
    names = _run_filter(ToolExposureMiddleware())
    assert {"projects.list", "jobs.get", "allocations.request"} <= names
    assert "control.force_crash" not in names  # admin-only
    assert "ops.reconcile_now" not in names  # platform


def test_platform_operator_sees_platform_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kdive.mcp.middleware.current_context",
        lambda: _ctx(platform=frozenset({PlatformRole.PLATFORM_OPERATOR})),
    )
    names = _run_filter(ToolExposureMiddleware())
    assert "ops.reconcile_now" in names
    assert "jobs.get" not in names  # no project grant


def test_fail_open_when_context_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _raise() -> RequestContext:
        raise AuthError("no token")

    monkeypatch.setattr("kdive.mcp.middleware.current_context", _raise)
    with caplog.at_level(logging.WARNING):
        names = _run_filter(ToolExposureMiddleware())
    assert names == {t.name for t in _ALL}  # unfiltered — discovery never breaks
