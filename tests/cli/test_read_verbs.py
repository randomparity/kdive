"""Curated read verbs call the right tool, flatten the envelope, and render rows/records.

The verbs are driven through fakes for the MCP client so the tests are hermetic: a fake
client returns a deserialized ``ToolResponse``-shaped payload (``object_id`` + ``status``
+ ``data`` + ``items``), the verb flattens it to rows, and ``render`` prints them.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import pytest

import kdive.cli.commands.reads as reads
from kdive.cli.commands.registry import REGISTRY


class _FakeResult:
    def __init__(self, data: dict) -> None:
        self.data = data


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict) -> _FakeResult:
        self.calls.append((name, arguments))
        return _FakeResult(self._payload)


class _FakeSession:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def client(self) -> _FakeClient:
        return self._client


def _install_session(monkeypatch: pytest.MonkeyPatch, payload: dict) -> _FakeClient:
    client = _FakeClient(payload)
    monkeypatch.setattr(reads, "_session_factory", lambda: _FakeSession(client))
    return client


def _collection(items: list[dict]) -> dict:
    return {"object_id": "x", "status": "ok", "data": {"count": len(items)}, "items": items}


def _item(object_id: str, status: str, data: dict) -> dict:
    return {"object_id": object_id, "status": status, "data": data, "items": []}


def _args(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(json=False, **kwargs)


def test_resources_list_flattens_items_and_renders(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(
        monkeypatch,
        _collection([_item("r1", "ok", {"kind": "local-libvirt", "host": "qemu:///system"})]),
    )
    code = asyncio.run(reads.resources_list(_args(kind=None)))
    assert code == 0
    assert client.calls == [("resources.list", {})]
    out = capsys.readouterr().out
    assert "r1" in out and "local-libvirt" in out and "qemu:///system" in out


def test_resources_list_passes_kind_filter(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _collection([]))
    asyncio.run(reads.resources_list(_args(kind="remote-libvirt")))
    assert client.calls == [("resources.list", {"kind": "remote-libvirt"})]


def test_list_verb_id_comes_from_object_id_and_state_from_status(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(
        monkeypatch,
        _collection([_item("al-1", "active", {"project": "p", "system": "s"})]),
    )
    asyncio.run(reads.allocations_list(_args(project="p")))
    out = capsys.readouterr().out
    # id <- object_id, state <- status, project/system <- data.
    assert "al-1" in out and "active" in out and "p" in out and "s" in out


def test_allocations_list_requires_project_in_payload(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install_session(monkeypatch, _collection([]))
    asyncio.run(reads.allocations_list(_args(project="proj-a")))
    assert client.calls == [("allocations.list", {"project": "proj-a"})]


def test_resources_describe_renders_single_record(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    record = {
        "object_id": "r1",
        "status": "ok",
        "data": {"pool": "p", "host_uri": "u"},
        "items": [],
    }
    client = _install_session(monkeypatch, record)
    code = asyncio.run(reads.resources_describe(_args(resource_id="r1")))
    assert code == 0
    assert client.calls == [("resources.describe", {"resource_id": "r1"})]
    out = capsys.readouterr().out
    assert "id" in out and "r1" in out and "pool" in out and "p" in out


def test_record_verb_json_mode_emits_flat_record(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    record = {"object_id": "s1", "status": "running", "data": {"project": "p"}, "items": []}
    _install_session(monkeypatch, record)
    asyncio.run(reads.systems_show(argparse.Namespace(json=True, system_id="s1")))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["id"] == "s1" and parsed["state"] == "running" and parsed["project"] == "p"


def test_ledger_show_is_a_single_record(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    record = {"object_id": "p", "status": "ok", "data": {"kcu": "12", "window": "30d"}, "items": []}
    client = _install_session(monkeypatch, record)
    asyncio.run(reads.ledger_show(_args(project="proj-a")))
    assert client.calls == [("accounting.usage_project", {"project": "proj-a"})]
    out = capsys.readouterr().out
    assert "kcu" in out and "12" in out


def test_inventory_show_lists_rows(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(
        monkeypatch,
        _collection([_item("k1", "ok", {"key": "k1", "backend": "minio", "status": "ready"})]),
    )
    asyncio.run(reads.inventory_show(_args(project=None)))
    assert client.calls == [("inventory.list", {})]
    out = capsys.readouterr().out
    assert "minio" in out and "ready" in out


def _data_envelope(data: dict) -> dict:
    return {"object_id": "x", "status": "ok", "data": data, "items": []}


def test_secrets_list_renders_refs_from_data(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # secrets.list returns refs under data.secrets (a flat string list), not nested items.
    client = _install_session(monkeypatch, _data_envelope({"secrets": ["ref://a", "ref://b"]}))
    code = asyncio.run(reads.secrets_list(_args()))
    assert code == 0
    assert client.calls == [("secrets.list", {})]
    out = capsys.readouterr().out
    assert "ref" in out and "ref://a" in out and "ref://b" in out


def test_secrets_list_json_mode_emits_ref_rows(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(monkeypatch, _data_envelope({"secrets": ["ref://a"]}))
    asyncio.run(reads.secrets_list(argparse.Namespace(json=True)))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == [{"ref": "ref://a"}]


def test_fixtures_list_renders_rows_from_data(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(
        monkeypatch,
        _data_envelope({"fixtures": [{"provider": "local-libvirt", "name": "base", "arch": "x"}]}),
    )
    code = asyncio.run(reads.fixtures_list(_args()))
    assert code == 0
    assert client.calls == [("fixtures.list", {})]
    out = capsys.readouterr().out
    assert "local-libvirt" in out and "base" in out


def test_data_shaped_lists_ignore_malformed_rows(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(
        monkeypatch,
        _data_envelope(
            {
                "fixtures": [
                    {"provider": "local-libvirt", "name": "base", "arch": "x86_64"},
                    "not-a-row",
                ]
            }
        ),
    )
    asyncio.run(reads.fixtures_list(_args()))
    out = capsys.readouterr().out
    assert "local-libvirt" in out
    assert "not-a-row" not in out


def test_data_shaped_lists_ignore_missing_list_data(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(
        monkeypatch,
        _data_envelope({"secrets": "not-a-list"}),  # pragma: allowlist secret - key name only
    )
    asyncio.run(reads.secrets_list(_args()))
    out = capsys.readouterr().out.strip()
    assert out and len(out.splitlines()) == 1


def test_every_registry_verb_has_a_handler() -> None:
    # The registry is the single source of truth; every entry must resolve to a callable.
    for verb in REGISTRY:
        assert callable(verb.handler)


def test_report_verbs_are_registered_and_read_only() -> None:
    by_path = {(v.group, v.sub): v for v in REGISTRY}
    all_v = by_path[("ledger", "report-all")]
    granted = by_path[("ledger", "report-granted")]
    assert all_v.tool == "accounting.report_all_projects" and all_v.read_only
    assert granted.tool == "accounting.report_granted_set" and granted.read_only
    assert all_v.options == ("group_by", "since", "until")
    assert granted.options == ("projects", "group_by", "since", "until")
    assert "platform_auditor" in all_v.help  # help notes the required role


_READ_VERBS = [v for v in REGISTRY if v.read_only]


@pytest.mark.parametrize("verb", _READ_VERBS, ids=lambda v: f"{v.group}.{v.sub}")
def test_handler_calls_the_tool_the_registry_declares(verb, monkeypatch, capsys) -> None:
    # Bind verb.tool (what the read-only gate test checks) to the handler's real call, so a
    # registry that declares a read-only tool but dispatches to another would fail here.
    client = _FakeClient(_collection([]))
    monkeypatch.setattr(reads, "_session_factory", lambda: _FakeSession(client))
    args = argparse.Namespace(json=False)
    for name in (*verb.positionals, *verb.options, *verb.required_options):
        setattr(args, name, f"{name}-val")
    asyncio.run(verb.handler(args))
    assert client.calls and client.calls[0][0] == verb.tool


def test_list_verb_with_empty_items_prints_only_header(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(monkeypatch, _collection([]))
    asyncio.run(reads.jobs_list(_args(limit=None)))
    out = capsys.readouterr().out.strip()
    assert out and len(out.splitlines()) == 1


def _denied(object_id: str) -> dict:
    return {
        "object_id": object_id,
        "status": "error",
        "error_category": "authorization_denied",
        "data": {},
        "items": [],
    }


def test_secrets_list_denial_exits_authorization_denied(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # A server-side denial returns a failure envelope; the verb must surface exit 3, not the
    # empty-success exit 0 that an unmapped error_category leaves (ADR-0089 exit-code table).
    _install_session(monkeypatch, _denied("secrets"))
    code = asyncio.run(reads.secrets_list(_args()))
    assert code == 3


def test_list_verb_denial_exits_authorization_denied(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(monkeypatch, _denied("resources"))
    code = asyncio.run(reads.resources_list(_args(kind=None)))
    assert code == 3


def test_record_verb_denial_exits_authorization_denied(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # A single-record verb must surface the same nonzero exit a denial returns, not the
    # success exit 0 that ignoring the envelope would leave.
    _install_session(monkeypatch, _denied("s1"))
    code = asyncio.run(reads.systems_show(_args(system_id="s1")))
    assert code == 3


def test_fixtures_list_denial_exits_authorization_denied(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(monkeypatch, _denied("fixtures"))
    code = asyncio.run(reads.fixtures_list(_args()))
    assert code == 3


def test_allocations_list_json_projects_declared_columns(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(
        monkeypatch,
        _collection([_item("al-1", "active", {"project": "p", "system": "s"})]),
    )
    asyncio.run(reads.allocations_list(argparse.Namespace(json=True, project="p")))
    assert json.loads(capsys.readouterr().out) == [
        {"id": "al-1", "project": "p", "system": "s", "state": "active"}
    ]


def test_systems_list_json_projects_columns_and_passes_state_filter(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install_session(
        monkeypatch,
        _collection([_item("sy-1", "running", {"project": "p"})]),
    )
    asyncio.run(reads.systems_list(argparse.Namespace(json=True, state="running")))
    assert client.calls == [("systems.list", {"state": "running"})]
    assert json.loads(capsys.readouterr().out) == [
        {"id": "sy-1", "project": "p", "state": "running"}
    ]


def test_jobs_list_json_projects_declared_columns(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install_session(
        monkeypatch,
        _collection([_item("jo-1", "queued", {"kind": "boot"})]),
    )
    asyncio.run(reads.jobs_list(argparse.Namespace(json=True, limit=None)))
    assert json.loads(capsys.readouterr().out) == [
        {"id": "jo-1", "kind": "boot", "state": "queued"}
    ]


def test_inventory_show_json_projects_columns_and_passes_project_filter(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install_session(
        monkeypatch,
        _collection([_item("k1", "ok", {"key": "k1", "backend": "minio", "status": "ready"})]),
    )
    asyncio.run(reads.inventory_show(argparse.Namespace(json=True, project="proj-a")))
    assert client.calls == [("inventory.list", {"project": "proj-a"})]
    assert json.loads(capsys.readouterr().out) == [
        {"key": "k1", "backend": "minio", "status": "ready"}
    ]


def test_fixtures_list_json_projects_declared_columns(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(
        monkeypatch,
        _data_envelope({"fixtures": [{"provider": "local-libvirt", "name": "base", "arch": "x"}]}),
    )
    asyncio.run(reads.fixtures_list(argparse.Namespace(json=True)))
    assert json.loads(capsys.readouterr().out) == [
        {"provider": "local-libvirt", "name": "base", "arch": "x"}
    ]


def test_record_verbs_send_the_declared_id_payload_key(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    cases = [
        (reads.allocations_get, "allocation_id", "allocations.get"),
        (reads.systems_show, "system_id", "systems.get"),
        (reads.runs_show, "run_id", "runs.get"),
        (reads.jobs_get, "job_id", "jobs.get"),
    ]
    for handler, key, tool in cases:
        client = _install_session(monkeypatch, _data_envelope({}))
        asyncio.run(handler(_args(**{key: "obj-1"})))
        assert client.calls == [(tool, {key: "obj-1"})]


def test_payload_omits_missing_optional_filter(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # A list verb whose optional filter attr is absent sends no filter, rather than raising.
    client = _install_session(monkeypatch, _collection([]))
    asyncio.run(reads.allocations_list(argparse.Namespace(json=False)))
    assert client.calls == [("allocations.list", {})]


def _report_collection(items: list[dict], totals: dict) -> dict:
    return {"object_id": "report", "status": "ok", "data": totals, "items": items}


def test_report_all_calls_tool_with_no_optional_args(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install_session(monkeypatch, _report_collection([], {"scope": "all-projects"}))
    code = asyncio.run(reads.ledger_report_all(_args(group_by=None, since=None, until=None)))
    assert code == 0
    assert client.calls == [("accounting.report_all_projects", {})]


def test_report_all_assembles_window_and_group_by(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(
        reads.ledger_report_all(
            _args(group_by="principal", since="2026-01-01T00:00:00+00:00", until=None)
        )
    )
    assert client.calls == [
        (
            "accounting.report_all_projects",
            {"group_by": "principal", "window": ["2026-01-01T00:00:00+00:00", None]},
        )
    ]


def test_report_all_window_until_only_is_half_open(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # The symmetric half-open direction: only --until sets the second bound, first is null.
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(
        reads.ledger_report_all(_args(group_by=None, since=None, until="2026-12-31T00:00:00+00:00"))
    )
    assert client.calls == [
        ("accounting.report_all_projects", {"window": [None, "2026-12-31T00:00:00+00:00"]})
    ]


def test_report_granted_splits_projects(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(
        reads.ledger_report_granted(
            _args(group_by=None, since=None, until=None, projects="a, b ,c")
        )
    )
    assert client.calls == [("accounting.report_granted_set", {"projects": ["a", "b", "c"]})]


def test_report_granted_omits_projects_when_absent(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(
        reads.ledger_report_granted(_args(group_by=None, since=None, until=None, projects=None))
    )
    assert client.calls == [("accounting.report_granted_set", {})]


def test_report_granted_all_empty_projects_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    code = asyncio.run(
        reads.ledger_report_granted(_args(group_by=None, since=None, until=None, projects=" , "))
    )
    assert code == 2
    assert client.calls == []  # rejected before any tool call
    assert "--projects" in capsys.readouterr().err


def test_report_renders_rows_and_totals_json(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    items = [
        _item(
            "p",
            "ok",
            {
                "project": "p",
                "principal": "",
                "reserved": "20",
                "reconciled": "-19",
                "variance": "1",
            },
        )
    ]
    totals = {
        "scope": "all-projects",
        "group_by": "",
        "project_count": "1",
        "total_project": "*",
        "total_principal": "",
        "total_reserved": "20",
        "total_reconciled": "-19",
        "total_variance": "1",
    }
    _install_session(monkeypatch, _report_collection(items, totals))
    asyncio.run(
        reads.ledger_report_all(
            argparse.Namespace(json=True, group_by=None, since=None, until=None)
        )
    )
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["items"] == [
        {"project": "p", "principal": "", "reserved": "20", "reconciled": "-19", "variance": "1"}
    ]
    assert parsed["totals"]["total_reserved"] == "20"
    assert parsed["totals"]["scope"] == "all-projects"


def test_report_all_denial_exits_authorization_denied(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(monkeypatch, _denied("report"))
    code = asyncio.run(reads.ledger_report_all(_args(group_by=None, since=None, until=None)))
    assert code == 3
