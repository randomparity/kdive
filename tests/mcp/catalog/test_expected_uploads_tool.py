"""``artifacts.expected_uploads`` — discoverable upload-artifact vocabulary (#551, ADR-0166).

The pure handler is driven directly; a registrar-level test asserts the tool is exposed
``read_only`` and is auth-only (consults the request context). The projection is asserted
against the same constants the upload validator enforces, so the test fails if the
advertised vocabulary ever drifts from the accepted names.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.read_model import RUN_ARTIFACT_NAMES, SYSTEM_ARTIFACT_NAMES
from kdive.build_artifacts.validation import EFFECTIVE_CONFIG_MAX_BYTES
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog.artifacts import registrar as artifacts_registrar
from kdive.mcp.tools.catalog.artifacts.expected_uploads import (
    EXTERNAL_BUILD_UPLOAD_DOC,
    expected_uploads,
)
from kdive.mcp.tools.catalog.artifacts.feature_requirements import (
    FEATURE_CONFIG_REQUIREMENTS_TOOL,
)
from kdive.mcp.tools.catalog.artifacts.uploads import (
    CREATE_RUN_UPLOAD_TOOL,
    CREATE_SYSTEM_UPLOAD_TOOL,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role


def _items(resp: ToolResponse) -> dict[str, dict[str, Any]]:
    return {item.object_id: cast(dict[str, Any], item.data) for item in resp.items}


def test_expected_uploads_projects_both_owner_vocabularies() -> None:
    resp = expected_uploads()
    assert resp.status == "ok"
    items = _items(resp)
    assert set(items) == {"run", "system"}

    run = items["run"]
    assert run["owner_kind"] == "run"
    assert run["accepted_names"] == sorted(RUN_ARTIFACT_NAMES)
    assert run["create_tool"] == CREATE_RUN_UPLOAD_TOOL
    # A contract for exactly each accepted name — the advisory can never name an artifact the
    # upload validator does not accept, nor omit one it does.
    assert set(run["contracts"]) == set(run["accepted_names"])

    system = items["system"]
    assert system["owner_kind"] == "system"
    assert system["accepted_names"] == sorted(SYSTEM_ARTIFACT_NAMES)
    assert system["create_tool"] == CREATE_SYSTEM_UPLOAD_TOOL
    assert system["accepted_names"] == ["rootfs"]
    assert set(system["contracts"]) == set(system["accepted_names"])


def test_run_item_states_the_unified_provider_neutral_contract() -> None:
    run = _items(expected_uploads())["run"]
    assert run["provider_neutral"] is True
    assert run["doc"] == EXTERNAL_BUILD_UPLOAD_DOC

    kernel = run["contracts"]["kernel"]
    assert kernel["requirement"] == "required"
    assert kernel["format"]["container"] == "gzip tar"
    assert kernel["format"]["magic"] == [{"offset": 0, "hex": "1f8b"}]
    member_paths = {member["path"] for member in kernel["layout"]}
    assert member_paths == {"boot/vmlinuz", "lib/modules/"}
    boot = next(m for m in kernel["layout"] if m["path"] == "boot/vmlinuz")
    assert boot["format"]["magic"] == [{"offset": 0x202, "hex": "48647253"}]  # "HdrS"

    assert run["contracts"]["vmlinux"]["requirement"] == "optional"
    assert run["contracts"]["initrd"]["requirement"] == "optional"
    assert "build_id" in " ".join(run["contracts"]["vmlinux"]["notes"])

    effective = run["contracts"]["effective_config"]
    assert effective["requirement"] == "optional"
    assert effective["format"]["max_bytes"] == EFFECTIVE_CONFIG_MAX_BYTES
    assert "never validated" in " ".join(effective["notes"])


def test_system_rootfs_contract_is_minimal() -> None:
    system = _items(expected_uploads())["system"]
    rootfs = system["contracts"]["rootfs"]
    assert rootfs["requirement"] == "required"
    assert rootfs["format"]["container"] == "filesystem image"
    # The combined-tar discoverability fields are run-only; the system item omits them.
    assert "provider_neutral" not in system
    assert "doc" not in system


def test_expected_uploads_items_carry_ok_status() -> None:
    resp = expected_uploads()
    by_id = {item.object_id: item for item in resp.items}
    assert by_id["run"].status == "ok"
    assert by_id["system"].status == "ok"


def test_expected_uploads_chains_to_the_create_tools() -> None:
    resp = expected_uploads()
    assert resp.suggested_next_actions == [
        FEATURE_CONFIG_REQUIREMENTS_TOOL,
        CREATE_RUN_UPLOAD_TOOL,
        CREATE_SYSTEM_UPLOAD_TOOL,
    ]


def _ctx() -> RequestContext:
    return RequestContext(
        principal="expected-uploads-user",
        agent_session="expected-uploads-session",
        projects=("proj",),
        roles={"proj": Role.VIEWER},
        platform_roles=frozenset(),
    )


def _read_only_hint(tool: object) -> bool | None:
    annotations = getattr(tool, "annotations", None)
    value = getattr(annotations, "readOnlyHint", None)
    return value if isinstance(value, bool) else None


def test_expected_uploads_registered_read_only_and_auth_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool is exposed read_only and invokes current_context() (auth-only)."""

    seen: list[bool] = []

    def fake_current_context() -> RequestContext:
        seen.append(True)
        return _ctx()

    async def _run() -> None:
        monkeypatch.setattr(artifacts_registrar, "current_context", fake_current_context)
        pool = AsyncConnectionPool("postgresql://unused", open=False)
        app = FastMCP("artifacts-expected-uploads-test")
        artifacts_registrar.register(app, pool, resolver=cast(ProviderResolver, object()))
        tools = {tool.name: tool for tool in await app.list_tools()}

        assert "artifacts.expected_uploads" in tools
        assert _read_only_hint(tools["artifacts.expected_uploads"]) is True

        fn = cast(Any, tools["artifacts.expected_uploads"]).fn
        resp = await fn()
        assert isinstance(resp, ToolResponse)
        assert resp.status == "ok"
        assert seen == [True]  # auth-only: consulted the request context

    asyncio.run(_run())
