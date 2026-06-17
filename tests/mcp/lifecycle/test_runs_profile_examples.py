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
4. **Shape** — one item per host, ``object_id == host.name``; the collection chains into
   ``runs.create``/``runs.build``; an empty host list yields a valid empty collection.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from kdive.db.build_hosts import BuildHost, BuildHostKind, BuildHostState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.runs.profile_examples import build_host_profile_examples
from kdive.profiles.build import BuildProfile, ServerBuildProfile, is_git_source
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
    )


_ALL_KINDS = [
    _host("worker-local", BuildHostKind.LOCAL),
    _host("ssh-host", BuildHostKind.SSH),
    _host("eph-host", BuildHostKind.EPHEMERAL_LIBVIRT),
]


def _items(resp: ToolResponse) -> dict[str, dict[str, Any]]:
    return {item.object_id: cast(dict[str, Any], item.data) for item in resp.items}


def _profile_of(data: dict[str, Any]) -> dict[str, Any]:
    profile = data["profile"]
    assert isinstance(profile, dict)
    return profile


def test_one_item_per_host_with_name_object_id() -> None:
    resp = build_host_profile_examples(_ALL_KINDS)
    assert resp.status == "ok"
    items = _items(resp)
    assert set(items) == {"worker-local", "ssh-host", "eph-host"}
    for host in _ALL_KINDS:
        assert items[host.name]["build_host"] == host.name
        assert items[host.name]["host_kind"] == host.kind.value


def test_every_example_parses_as_server_build_profile() -> None:
    resp = build_host_profile_examples(_ALL_KINDS)
    for data in _items(resp).values():
        parsed = BuildProfile.parse(_profile_of(data))
        assert isinstance(parsed, ServerBuildProfile)
        assert parsed.build_host == data["build_host"]


def test_source_form_matches_advertised_kind() -> None:
    resp = build_host_profile_examples(_ALL_KINDS)
    for data in _items(resp).values():
        parsed = BuildProfile.parse(_profile_of(data))
        assert isinstance(parsed, ServerBuildProfile)
        advertised_git = "git" in data["supported_source_kinds"]
        assert is_git_source(parsed) is advertised_git


def test_advertised_kinds_match_shared_helper() -> None:
    resp = build_host_profile_examples(_ALL_KINDS)
    items = _items(resp)
    for host in _ALL_KINDS:
        expected = [k.value for k in accepted_source_kinds(host.kind)]
        assert items[host.name]["supported_source_kinds"] == expected


def test_examples_are_compatible_with_their_host() -> None:
    resp = build_host_profile_examples(_ALL_KINDS)
    for host in _ALL_KINDS:
        data = _items(resp)[host.name]
        parsed = BuildProfile.parse(_profile_of(data))
        assert isinstance(parsed, ServerBuildProfile)
        # Does not raise: the emitted example would survive runs.create/runs.build.
        check_source_kind_compatibility(
            host_kind=host.kind, is_git=is_git_source(parsed), build_host=host.name
        )


def test_local_uses_string_remote_uses_git_object() -> None:
    resp = build_host_profile_examples(_ALL_KINDS)
    items = _items(resp)
    assert isinstance(_profile_of(items["worker-local"])["kernel_source_ref"], str)
    for remote in ("ssh-host", "eph-host"):
        ref = _profile_of(items[remote])["kernel_source_ref"]
        assert isinstance(ref, dict)
        assert "git" in ref
        assert set(ref["git"]) == {"remote", "ref"}


def test_collection_chains_into_runs_create_and_build() -> None:
    resp = build_host_profile_examples(_ALL_KINDS)
    assert resp.suggested_next_actions == ["runs.create", "runs.build"]


def test_empty_host_list_is_valid_empty_collection() -> None:
    resp = build_host_profile_examples([])
    assert resp.status == "ok"
    assert resp.items == []
    assert resp.data["count"] == "0"
