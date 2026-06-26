"""``runs.profile_examples`` — discoverable, schema-valid example build profiles (#536).

The tool projects the ``build_hosts`` inventory into one ready-to-edit build profile per
registered host (ADR-0158). The pure handler is driven directly with hand-built
``BuildHost`` objects; the tests assert four contracts:

1. **Validity** — every emitted ``data.profile``, as emitted, parses via
   ``BuildProfile.parse`` into a ``ServerBuildProfile``. This is what stops the advertised
   examples rotting.
2. **Source-form/advertised-kind agreement** — for every item,
   ``is_git_source(parse(profile))`` is ``True`` iff ``"git"`` is in
   ``data.supported_source_kinds`` (a string ``kernel_source_ref`` for local, a
   ``{"git": {...}}`` object for remote). The example never advertises a lane it does
   not itself use.
3. **Host compatibility** — every example would survive ``check_source_kind_compatibility``
   for its host's kind.
4. **Shape** — a leading host-independent ``source='external'`` example (the recommended
   default lane, ADR-0234) followed by one item per host, ``object_id == host.name``; the
   collection leads with the external upload loop; an empty host list still yields the
   external example.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool

from kdive.db.build_hosts import BuildHost, BuildHostKind, BuildHostState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.runs import registrar as runs_registrar
from kdive.mcp.tools.lifecycle.runs.profile_examples import build_host_profile_examples
from kdive.profiles.build import (
    BuildProfile,
    ExternalBuildProfile,
    ServerBuildProfile,
    is_git_source,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.services.runs.build_host_selection import (
    accepted_source_kinds,
    check_source_kind_compatibility,
)


def _host(name: str, kind: BuildHostKind) -> BuildHost:
    return BuildHost(
        id=UUID("00000000-0000-0000-0000-00000000ade5"),
        name=name,
        kind=kind,
        address="builder.example" if kind is not BuildHostKind.LOCAL else None,
        ssh_credential_ref="ssh://builder" if kind is BuildHostKind.SSH else None,
        base_image_volume="base.qcow2" if kind is BuildHostKind.EPHEMERAL_LIBVIRT else None,
        workspace_root="/build",
        max_concurrent=1,
        enabled=True,
        state=BuildHostState.READY,
        toolchain_desc=None,
    )


_ALL_KINDS = [
    _host("worker-local", BuildHostKind.LOCAL),
    _host("ssh-host", BuildHostKind.SSH),
    _host("eph-host", BuildHostKind.EPHEMERAL_LIBVIRT),
]


_EXTERNAL_OBJECT_ID = "external-upload"


def _items(resp: ToolResponse) -> dict[str, dict[str, Any]]:
    return {item.object_id: cast(dict[str, Any], item.data) for item in resp.items}


def _host_items(resp: ToolResponse) -> dict[str, dict[str, Any]]:
    """The per-host server examples, excluding the leading external example."""
    return {k: v for k, v in _items(resp).items() if k != _EXTERNAL_OBJECT_ID}


def _profile_of(data: dict[str, Any]) -> dict[str, Any]:
    profile = data["profile"]
    assert isinstance(profile, dict)
    return profile


def test_one_item_per_host_with_name_object_id() -> None:
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    assert resp.status == "ok"
    items = _host_items(resp)
    assert set(items) == {"worker-local", "ssh-host", "eph-host"}
    for host in _ALL_KINDS:
        assert items[host.name]["build_host"] == host.name
        assert items[host.name]["host_kind"] == host.kind.value


def test_external_example_is_first_recommended_and_host_independent() -> None:
    # ADR-0234: the recommended default lane leads the collection as a host-independent
    # source='external' example with the upload-sequence next actions.
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    first = resp.items[0]
    assert first.object_id == _EXTERNAL_OBJECT_ID
    data = cast(dict[str, Any], first.data)
    assert data["recommended"] is True
    assert "build_host" not in data
    profile = _profile_of(data)
    assert profile == {"schema_version": 1, "source": "external"}
    parsed = BuildProfile.parse(profile)
    assert isinstance(parsed, ExternalBuildProfile)
    assert first.suggested_next_actions == [
        "runs.create",
        "artifacts.expected_uploads",
        "artifacts.create_run_upload",
    ]


def test_external_note_discloses_recommended_upload_lane() -> None:
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    note = _items(resp)[_EXTERNAL_OBJECT_ID]["note"]
    assert isinstance(note, str)
    lowered = note.lower()
    assert "recommended" in lowered
    assert "artifacts.expected_uploads" in note
    assert "runs.complete_build" in note


def test_every_host_example_parses_as_server_build_profile() -> None:
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    for data in _host_items(resp).values():
        parsed = BuildProfile.parse(_profile_of(data))
        assert isinstance(parsed, ServerBuildProfile)
        assert parsed.build_host == data["build_host"]


def test_source_form_matches_advertised_kind() -> None:
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    for data in _host_items(resp).values():
        parsed = BuildProfile.parse(_profile_of(data))
        assert isinstance(parsed, ServerBuildProfile)
        # The example's source kind must be one the host advertises (a host may accept more
        # than one kind — e.g. a local host after ADR-0162 — and the example shows one of them).
        example_kind = "git" if is_git_source(parsed) else "warm-tree"
        assert example_kind in data["supported_source_kinds"]


def test_advertised_kinds_match_shared_helper() -> None:
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    items = _items(resp)
    for host in _ALL_KINDS:
        expected = [k.value for k in accepted_source_kinds(host.kind)]
        assert items[host.name]["supported_source_kinds"] == expected


def test_examples_are_compatible_with_their_host() -> None:
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    for host in _ALL_KINDS:
        data = _items(resp)[host.name]
        parsed = BuildProfile.parse(_profile_of(data))
        assert isinstance(parsed, ServerBuildProfile)
        # Does not raise: the emitted example would survive runs.create/runs.build.
        check_source_kind_compatibility(
            host_kind=host.kind, is_git=is_git_source(parsed), build_host=host.name
        )


def test_local_uses_string_remote_uses_git_object() -> None:
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    items = _items(resp)
    assert isinstance(_profile_of(items["worker-local"])["kernel_source_ref"], str)
    for remote in ("ssh-host", "eph-host"):
        ref = _profile_of(items[remote])["kernel_source_ref"]
        assert isinstance(ref, dict)
        assert "git" in ref
        assert set(ref["git"]) == {"remote", "ref"}


def test_note_discloses_warm_tree_is_provenance_only() -> None:
    # D5 (#806): the warm-tree string is a provenance label only — it does not select the
    # tree. The note must say so inline, name KDIVE_KERNEL_SRC as where the operator stages
    # the real source, and cross-reference the post-build data.build_provenance echo so a
    # cold agent need not open the build-source-staging resource to understand the field.
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    note = _items(resp)["worker-local"]["note"]
    assert isinstance(note, str)
    lowered = note.lower()
    assert "provenance" in lowered
    assert "KDIVE_KERNEL_SRC" in note
    assert "build_provenance" in note


def test_collection_leads_with_external_upload_loop() -> None:
    # ADR-0234: the collection-level next actions lead with the external upload loop; the
    # server-build verb (runs.build) trails as the secondary single-host convenience.
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    assert resp.suggested_next_actions == [
        "runs.create",
        "artifacts.expected_uploads",
        "artifacts.create_run_upload",
        "runs.build",
    ]


def test_empty_host_list_still_yields_external_example() -> None:
    # No build hosts still leaves the host-independent external example: a cold agent always
    # gets the recommended default lane.
    resp = build_host_profile_examples([], declared_instances=[])
    assert resp.status == "ok"
    assert [item.object_id for item in resp.items] == [_EXTERNAL_OBJECT_ID]
    assert resp.data["count"] == 1


def test_unresolvable_ephemeral_host_is_omitted() -> None:
    # eph-host names no declared [[remote_libvirt]] instance: it cannot build, so no example.
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=[])
    names = {item.object_id for item in resp.items}
    assert "eph-host" not in names
    assert {"worker-local", "ssh-host"} <= names


def test_resolvable_ephemeral_host_is_emitted() -> None:
    resp = build_host_profile_examples(_ALL_KINDS, declared_instances=["eph-host"])
    names = {item.object_id for item in resp.items}
    assert "eph-host" in names


# --- registrar boundary + pool-backed behavior ---


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _ctx() -> RequestContext:
    return RequestContext(
        principal="examples-user",
        agent_session="examples-session",
        projects=("proj",),
        roles={"proj": Role.VIEWER},
        platform_roles=frozenset(),
    )


def _read_only_hint(tool: object) -> bool | None:
    annotations = getattr(tool, "annotations", None)
    value = getattr(annotations, "readOnlyHint", None)
    return value if isinstance(value, bool) else None


async def _insert_ssh_host(pool: AsyncConnectionPool, name: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO build_hosts (id, name, kind, address, ssh_credential_ref, "
            "workspace_root, max_concurrent) VALUES (%s, %s, 'ssh', '10.0.0.1', "
            "'cred-ref', '/build', 2)",
            (uuid4(), name),
        )


def test_runs_profile_examples_registered_read_only_and_auth_only(
    migrated_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool is exposed read_only and invokes current_context() (auth-only)."""

    seen: list[bool] = []

    def fake_current_context() -> RequestContext:
        seen.append(True)
        return _ctx()

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            monkeypatch.setattr(runs_registrar, "current_context", fake_current_context)
            await _insert_ssh_host(pool, "examples-ssh")
            app = FastMCP("runs-profile-examples-test")
            runs_registrar.register(app, pool, resolver=cast(ProviderResolver, object()))
            tools = {tool.name: tool for tool in await app.list_tools()}

            assert "runs.profile_examples" in tools
            assert _read_only_hint(tools["runs.profile_examples"]) is True

            fn = cast(Any, tools["runs.profile_examples"]).fn
            resp = await fn()

        assert isinstance(resp, ToolResponse)
        assert resp.status == "ok"
        names = {item.object_id for item in resp.items}
        assert "worker-local" in names  # the always-present seed
        assert "examples-ssh" in names
        items = {item.object_id: cast(dict[str, Any], item.data) for item in resp.items}
        assert items["worker-local"]["supported_source_kinds"] == ["warm-tree", "git"]
        assert items["examples-ssh"]["supported_source_kinds"] == ["git"]
        # auth-only: the wrapper consulted the request context.
        assert seen == [True]

    asyncio.run(_run())
