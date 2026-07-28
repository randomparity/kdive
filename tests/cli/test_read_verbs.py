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
    assert client.calls == [("resources.list", {"request": {"kind": "remote-libvirt"}})]


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
    assert client.calls == [("allocations.list", {"request": {"project": "proj-a"}})]


def test_resources_get_renders_single_record(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    record = {
        "object_id": "r1",
        "status": "ok",
        "data": {"pool": "p", "host_uri": "u"},
        "items": [],
    }
    client = _install_session(monkeypatch, record)
    code = asyncio.run(reads.resources_get(_args(resource_id="r1")))
    assert code == 0
    assert client.calls == [("resources.describe", {"resource_id": "r1"})]
    out = capsys.readouterr().out
    assert "id" in out and "r1" in out and "pool" in out and "p" in out


def test_record_verb_json_mode_emits_whole_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # --json is the server envelope verbatim, not the flat id/state/data projection (ADR-0421 §6).
    record = {
        "object_id": "s1",
        "status": "running",
        "data": {"project": "p"},
        "items": [],
        "suggested_next_actions": ["systems.release"],
    }
    _install_session(monkeypatch, record)
    asyncio.run(reads.systems_get(argparse.Namespace(json=True, system_id="s1")))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == record
    assert parsed["suggested_next_actions"] == ["systems.release"]


def test_ledger_get_is_a_single_record(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    record = {"object_id": "p", "status": "ok", "data": {"kcu": "12", "window": "30d"}, "items": []}
    client = _install_session(monkeypatch, record)
    asyncio.run(reads.ledger_get(_args(project="proj-a", investigation_id=None)))
    assert client.calls == [
        ("accounting.usage", {"target": {"kind": "project", "project": "proj-a"}})
    ]
    out = capsys.readouterr().out
    assert "kcu" in out and "12" in out


def test_ledger_get_sends_the_investigation_target(monkeypatch: pytest.MonkeyPatch) -> None:
    record = {"object_id": "p", "status": "ok", "data": {"kcu": "12"}, "items": []}
    client = _install_session(monkeypatch, record)
    asyncio.run(reads.ledger_get(_args(project=None, investigation_id="inv-1")))
    assert client.calls == [
        ("accounting.usage", {"target": {"kind": "investigation", "investigation_id": "inv-1"}})
    ]


@pytest.mark.parametrize(("project", "investigation_id"), [(None, None), ("proj-a", "inv-1")])
def test_ledger_get_requires_exactly_one_target(
    monkeypatch: pytest.MonkeyPatch, capsys, project: str | None, investigation_id: str | None
) -> None:
    # The merged tool discriminates on target.kind, so neither/both is a usage error the CLI
    # catches before any tool call rather than an opaque server-side discriminator failure.
    client = _install_session(monkeypatch, {"object_id": "p", "status": "ok", "items": []})
    code = asyncio.run(reads.ledger_get(_args(project=project, investigation_id=investigation_id)))
    assert code == 2
    assert client.calls == []
    assert "--project" in capsys.readouterr().err


def test_inventory_show_lists_rows(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(
        monkeypatch,
        _collection([_item("k1", "ok", {"key": "k1", "backend": "minio", "status": "ready"})]),
    )
    asyncio.run(reads.inventory_show(_args(project=None)))
    assert client.calls == [("inventory.list", {"request": {}})]
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


def test_secrets_list_json_mode_emits_whole_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # --json is the whole envelope; the ref rows are a default-table projection, not the contract.
    envelope = _data_envelope({"secrets": ["ref://a"]})
    _install_session(monkeypatch, envelope)
    asyncio.run(reads.secrets_list(argparse.Namespace(json=True)))
    assert json.loads(capsys.readouterr().out) == envelope


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


def test_report_verb_is_registered_read_only_and_scope_required() -> None:
    # One curated verb now covers both scopes: --scope is required so the CLI never picks a
    # scope for the caller, and it sits at the merged tool's generated path so it overrides
    # the schema-derived shape instead of adding a second path.
    by_path = {(v.group, v.sub): v for v in REGISTRY}
    report = by_path[("accounting", "report")]
    assert report.tool == "accounting.report" and report.read_only
    assert report.required_options == ("scope",)
    assert report.options == ("projects", "group_by", "since", "until")
    assert "platform_auditor" in report.help  # help notes the role the wide scope needs
    usage = by_path[("accounting", "usage")]
    assert usage.tool == "accounting.usage" and usage.read_only
    assert usage.options == ("project", "investigation_id")


_READ_VERBS = [v for v in REGISTRY if v.read_only]

#: Values a verb needs beyond the generic ``"<name>-val"`` placeholder: an enum-valued
#: option needs a real member, a numeric one needs a parseable number, and a verb whose
#: options are mutually exclusive needs the ones it must *not* receive dropped.
_VERB_ARG_OVERRIDES: dict[tuple[str, str], dict[str, object]] = {
    ("accounting", "report"): {"scope": "granted-set"},
    ("accounting", "usage"): {"investigation_id": None},
    ("jobs", "wait"): {"timeout_s": "0"},
    ("allocations", "wait"): {"timeout_s": "0"},
}


@pytest.mark.parametrize("verb", _READ_VERBS, ids=lambda v: f"{v.group}.{v.sub}")
def test_handler_calls_the_tool_the_registry_declares(verb, monkeypatch, capsys) -> None:
    # Bind verb.tool (what the read-only gate test checks) to the handler's real call, so a
    # registry that declares a read-only tool but dispatches to another would fail here.
    client = _FakeClient(_collection([]))
    monkeypatch.setattr(reads, "_session_factory", lambda: _FakeSession(client))
    args = argparse.Namespace(json=False)
    for name in (*verb.positionals, *verb.options, *verb.required_options):
        setattr(args, name, f"{name}-val")
    for name, value in _VERB_ARG_OVERRIDES.get((verb.group, verb.sub), {}).items():
        setattr(args, name, value)
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
    code = asyncio.run(reads.systems_get(_args(system_id="s1")))
    assert code == 3


def test_list_verb_json_emits_whole_envelope_with_next_actions(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # A curated list verb's --json is the server envelope verbatim: nested item envelopes plus
    # the navigation contract (suggested_next_actions) the old column projection dropped.
    envelope = _collection([_item("al-1", "active", {"project": "p", "system": "s"})])
    envelope["suggested_next_actions"] = ["allocations.release"]
    _install_session(monkeypatch, envelope)
    asyncio.run(reads.allocations_list(argparse.Namespace(json=True, project="p")))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == envelope
    assert parsed["suggested_next_actions"] == ["allocations.release"]
    assert parsed["items"][0]["object_id"] == "al-1"


def test_systems_list_json_emits_whole_envelope_and_passes_state_filter(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    envelope = _collection([_item("sy-1", "running", {"project": "p"})])
    client = _install_session(monkeypatch, envelope)
    asyncio.run(reads.systems_list(argparse.Namespace(json=True, state="running")))
    assert client.calls == [("systems.list", {"request": {"state": "running"}})]
    assert json.loads(capsys.readouterr().out) == envelope


def test_jobs_list_json_emits_whole_envelope(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    envelope = _collection([_item("jo-1", "queued", {"kind": "boot"})])
    client = _install_session(monkeypatch, envelope)
    asyncio.run(reads.jobs_list(argparse.Namespace(json=True, limit=None)))
    assert client.calls == [("jobs.list", {"request": {}})]
    assert json.loads(capsys.readouterr().out) == envelope


def test_inventory_show_json_emits_whole_envelope_and_passes_project_filter(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    envelope = _collection(
        [_item("k1", "ok", {"key": "k1", "backend": "minio", "status": "ready"})]
    )
    client = _install_session(monkeypatch, envelope)
    asyncio.run(reads.inventory_show(argparse.Namespace(json=True, project="proj-a")))
    # ``inventory.list`` takes its filters inside the ``request`` wrapper; this pinned the flat
    # payload the tool rejects until the schema guard caught it (#1611).
    assert client.calls == [("inventory.list", {"request": {"project": "proj-a"}})]
    assert json.loads(capsys.readouterr().out) == envelope


def test_record_verbs_send_the_declared_id_payload_key(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    cases = [
        (reads.systems_get, "system_id", "systems.get"),
        (reads.runs_get, "run_id", "runs.get"),
    ]
    for handler, key, tool in cases:
        client = _install_session(monkeypatch, _data_envelope({}))
        asyncio.run(handler(_args(**{key: "obj-1"})))
        assert client.calls == [(tool, {key: "obj-1"})]


_WAIT_CASES = [
    (reads.jobs_wait, "job_id", "jobs.wait"),
    (reads.allocations_wait, "allocation_id", "allocations.wait"),
]


@pytest.mark.parametrize(("handler", "key", "tool"), _WAIT_CASES)
def test_wait_verb_omitting_timeout_sends_only_the_id(
    handler, key: str, tool: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """No ``--timeout-s`` sends no ``timeout_s``, leaving the tool's 30s default authoritative.

    ADR-0470 decision 2: the CLI mirrors the tool default rather than restating it, so a bare
    ``kdivectl jobs wait <id>`` must not become a point read behind the operator's back.
    """
    client = _install_session(monkeypatch, _data_envelope({"state": "running"}))
    code = asyncio.run(handler(_args(**{key: "obj-1", "timeout_s": None})))
    assert code == 0
    assert client.calls == [(tool, {key: "obj-1"})]


@pytest.mark.parametrize(("handler", "key", "tool"), _WAIT_CASES)
def test_wait_verb_coerces_the_timeout_to_a_number(
    handler, key: str, tool: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``--timeout-s`` reaches the tool as a float, not the string argparse produced.

    Curated options are declared with no ``type=``, so the namespace carries ``"1.5"``. Both
    tools declare ``timeout_s`` as a JSON ``number``, so passing the string through emits a
    payload the schema rejects (ADR-0470 decision 3).
    """
    client = _install_session(monkeypatch, _data_envelope({"state": "running"}))
    asyncio.run(handler(_args(**{key: "obj-1", "timeout_s": "1.5"})))
    assert client.calls == [(tool, {key: "obj-1", "timeout_s": 1.5})]
    assert isinstance(client.calls[0][1]["timeout_s"], float)


@pytest.mark.parametrize(("handler", "key", "tool"), _WAIT_CASES)
def test_wait_verb_point_read_sends_a_zero_timeout(
    handler, key: str, tool: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The documented point read ``--timeout-s 0`` reaches the tool as ``0.0``, not dropped."""
    client = _install_session(monkeypatch, _data_envelope({"state": "succeeded"}))
    asyncio.run(handler(_args(**{key: "obj-1", "timeout_s": "0"})))
    assert client.calls == [(tool, {key: "obj-1", "timeout_s": 0.0})]


@pytest.mark.parametrize(("handler", "key", "tool"), _WAIT_CASES)
def test_wait_verb_rejects_a_non_numeric_timeout(
    handler, key: str, tool: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A non-numeric ``--timeout-s`` is a usage error, not an uncaught ``ValueError``."""
    client = _install_session(monkeypatch, _data_envelope({}))
    code = asyncio.run(handler(_args(**{key: "obj-1", "timeout_s": "soon"})))
    assert code == 2
    assert client.calls == []
    assert "--timeout-s" in capsys.readouterr().err


@pytest.mark.parametrize(("handler", "key", "tool"), _WAIT_CASES)
def test_wait_verb_json_emits_whole_envelope(
    handler, key: str, tool: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    envelope = {
        "object_id": "obj-1",
        "status": "running",
        "data": {"state": "running"},
        "items": [],
        "suggested_next_actions": [tool],
    }
    _install_session(monkeypatch, envelope)
    asyncio.run(handler(argparse.Namespace(json=True, timeout_s=None, **{key: "obj-1"})))
    assert json.loads(capsys.readouterr().out) == envelope


@pytest.mark.parametrize(("handler", "key", "tool"), _WAIT_CASES)
def test_wait_verb_denial_exits_authorization_denied(
    handler, key: str, tool: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(monkeypatch, _denied("obj-1"))
    code = asyncio.run(handler(_args(**{key: "obj-1", "timeout_s": None})))
    assert code == 3


def test_payload_omits_missing_optional_filter(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # A list verb whose optional filter attr is absent sends no filter, rather than raising.
    client = _install_session(monkeypatch, _collection([]))
    asyncio.run(reads.allocations_list(argparse.Namespace(json=False)))
    assert client.calls == [("allocations.list", {"request": {}})]


def _report_collection(items: list[dict], totals: dict) -> dict:
    return {"object_id": "report", "status": "ok", "data": totals, "items": items}


def _report_args(**kwargs: object) -> argparse.Namespace:
    defaults: dict[str, object] = {"group_by": None, "since": None, "until": None, "projects": None}
    return _args(**{**defaults, **kwargs})


def test_report_all_projects_sends_only_the_scope(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {"scope": "all-projects"}))
    code = asyncio.run(reads.ledger_report(_report_args(scope="all-projects")))
    assert code == 0
    assert client.calls == [("accounting.report", {"request": {"scope": "all-projects"}})]


def test_report_all_projects_assembles_window_and_group_by(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(
        reads.ledger_report(
            _report_args(
                scope="all-projects", group_by="principal", since="2026-01-01T00:00:00+00:00"
            )
        )
    )
    assert client.calls == [
        (
            "accounting.report",
            {
                "request": {
                    "scope": "all-projects",
                    "group_by": "principal",
                    "window": ["2026-01-01T00:00:00+00:00", None],
                }
            },
        )
    ]


def test_report_window_until_only_is_half_open(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # The symmetric half-open direction: only --until sets the second bound, first is null.
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(
        reads.ledger_report(_report_args(scope="all-projects", until="2026-12-31T00:00:00+00:00"))
    )
    assert client.calls == [
        (
            "accounting.report",
            {"request": {"scope": "all-projects", "window": [None, "2026-12-31T00:00:00+00:00"]}},
        )
    ]


def test_report_granted_set_splits_projects(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(reads.ledger_report(_report_args(scope="granted-set", projects="a, b ,c")))
    assert client.calls == [
        ("accounting.report", {"request": {"scope": "granted-set", "projects": ["a", "b", "c"]}})
    ]


def test_report_granted_set_omits_projects_when_absent(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    asyncio.run(reads.ledger_report(_report_args(scope="granted-set")))
    assert client.calls == [("accounting.report", {"request": {"scope": "granted-set"}})]


def test_report_all_empty_projects_is_usage_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    code = asyncio.run(reads.ledger_report(_report_args(scope="granted-set", projects=" , ")))
    assert code == 2
    assert client.calls == []  # rejected before any tool call
    assert "--projects" in capsys.readouterr().err


def test_report_rejects_an_unknown_scope(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install_session(monkeypatch, _report_collection([], {}))
    code = asyncio.run(reads.ledger_report(_report_args(scope="everything")))
    assert code == 2
    assert client.calls == []
    assert "--scope" in capsys.readouterr().err


def test_report_rejects_projects_outside_the_granted_set_scope(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # `projects` exists only on the granted-set branch; sending it with all-projects would be
    # an extra="forbid" server error, so the CLI refuses it up front.
    client = _install_session(monkeypatch, _report_collection([], {}))
    code = asyncio.run(reads.ledger_report(_report_args(scope="all-projects", projects="a")))
    assert code == 2
    assert client.calls == []
    assert "--projects" in capsys.readouterr().err


def test_report_json_emits_whole_envelope(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # A report verb's --json is the server envelope verbatim: the totals live in ``data`` and
    # the per-project rows in nested ``items`` envelopes, not a projected ``{items, totals}``.
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
        "project_count": 1,
        "total_project": "*",
        "total_principal": "",
        "total_reserved": "20",
        "total_reconciled": "-19",
        "total_variance": "1",
    }
    envelope = _report_collection(items, totals)
    _install_session(monkeypatch, envelope)
    asyncio.run(
        reads.ledger_report(
            argparse.Namespace(
                json=True,
                scope="all-projects",
                group_by=None,
                since=None,
                until=None,
                projects=None,
            )
        )
    )
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == envelope
    assert parsed["data"]["total_reserved"] == "20"
    assert parsed["items"][0]["data"]["project"] == "p"


def test_report_all_denial_exits_authorization_denied(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_session(monkeypatch, _denied("report"))
    code = asyncio.run(reads.ledger_report(_report_args(scope="all-projects")))
    assert code == 3
