"""Generic dispatch for schema-generated ``kdivectl`` verbs (#1450, ADR-0423).

Covers the three halves the generic handler unites: payload assembly from the parsed namespace
(strip the ``genarg_`` dest prefix, fold in ``--<param>-json`` values, honor ``unwrap_request``),
tier resolution from the *live* server annotations (never the committed artifact), and the
mutating/destructive ceremony (token preflight + typed-``yes`` confirm) driven by that live tier.

The session/client is faked (the ``dispatch._session_factory`` seam is monkeypatched, mirroring
``tests/cli/test_tool_call.py``) so the tests are hermetic.
"""

from __future__ import annotations

import argparse
import asyncio

import pytest

from kdive.cli import dispatch
from kdive.cli.__main__ import build_parser
from kdive.cli.commands.registry import GENERATED_ARG_PREFIX
from kdive.cli.commands.verb_spec import GeneratedFlag, GeneratedVerb


class _Annotations:
    def __init__(self, **hints: object) -> None:
        for key, value in hints.items():
            setattr(self, key, value)


class _Tool:
    def __init__(self, name: str, **hints: object) -> None:
        self.name = name
        self.annotations = _Annotations(**hints)


def _read_tool(name: str) -> _Tool:
    return _Tool(name, readOnlyHint=True)


def _mutating_tool(name: str) -> _Tool:
    return _Tool(name, readOnlyHint=False)


def _destructive_tool(name: str) -> _Tool:
    return _Tool(name, readOnlyHint=False, destructiveHint=True)


class _FakeResult:
    def __init__(self, envelope: dict) -> None:
        self.structured_content = envelope
        self.data = envelope


class _FakeClient:
    def __init__(self, tools: list[_Tool], envelope: dict) -> None:
        self._tools = tools
        self._envelope = envelope
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def list_tools(self) -> list[_Tool]:
        return self._tools

    async def call_tool(self, name: str, arguments: dict) -> _FakeResult:
        self.calls.append((name, arguments))
        return _FakeResult(self._envelope)


class _FakeSession:
    def __init__(self, client: _FakeClient, token: str = "x.y.z") -> None:
        self._client = client
        self.token = token

    def client(self) -> _FakeClient:
        return self._client


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tool: _Tool,
    *,
    envelope: dict | None = None,
    token: str = "x.y.z",
) -> _FakeClient:
    client = _FakeClient([tool], envelope or {"object_id": "o", "status": "ok", "data": {}})
    monkeypatch.setattr(dispatch, "_session_factory", lambda: _FakeSession(client, token=token))
    monkeypatch.setattr(dispatch, "ensure_token_valid", lambda *a, **k: None)
    monkeypatch.setattr(dispatch.sys.stdin, "isatty", lambda: False)
    return client


def _raise_expiring(*_a: object, **_k: object) -> None:
    from kdive.cli.commands.mutations import TokenExpiringError

    raise TokenExpiringError("expired")


def _verb(
    tool: str,
    *,
    destructive: bool = False,
    read_only: bool = True,
    unwrap_request: bool = False,
    flags: tuple[GeneratedFlag, ...] = (),
    json_params: tuple[str, ...] = (),
) -> GeneratedVerb:
    group, _, op = tool.partition(".")
    return GeneratedVerb(
        group=group,
        sub=op.replace("_", "-"),
        tool=tool,
        read_only=read_only,
        destructive=destructive,
        unwrap_request=unwrap_request,
        flags=flags,
        json_params=json_params,
    )


def _scalar(dest: str, *, required: bool = False) -> GeneratedFlag:
    return GeneratedFlag(
        name=f"--{dest.replace('_', '-')}", dest=dest, required=required, help="", arg_type="str"
    )


def _ns(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _dest(param: str) -> str:
    return f"{GENERATED_ARG_PREFIX}{param}"


def _run(verb: GeneratedVerb, args: argparse.Namespace) -> int:
    return asyncio.run(dispatch.invoke_generated_verb(verb, args))


# --- payload assembly ------------------------------------------------------------------------


def test_payload_strips_the_genarg_prefix() -> None:
    verb = _verb("systems.define", flags=(_scalar("name"),))
    args = _ns(**{_dest("name"): "web-01"})
    assert dispatch._assemble_generated_payload(verb, args) == {"name": "web-01"}


def test_payload_omits_absent_scalar_flags() -> None:
    verb = _verb("systems.define", flags=(_scalar("name"), _scalar("arch")))
    args = _ns(**{_dest("name"): "web-01", _dest("arch"): None})
    assert dispatch._assemble_generated_payload(verb, args) == {"name": "web-01"}


def test_payload_store_true_included_only_when_set() -> None:
    on = GeneratedFlag(name="--wait", dest="wait", required=False, help="", action="store_true")
    verb = _verb("runs.boot", flags=(on,))
    assert dispatch._assemble_generated_payload(verb, _ns(**{_dest("wait"): True})) == {
        "wait": True
    }
    # An unset boolean is omitted so the server default holds (argparse cannot express "unset").
    assert dispatch._assemble_generated_payload(verb, _ns(**{_dest("wait"): False})) == {}


def test_payload_append_flag_included_when_present() -> None:
    pkgs = GeneratedFlag(name="--pkg", dest="pkg", required=False, help="", action="append")
    verb = _verb("images.build", flags=(pkgs,))
    assert dispatch._assemble_generated_payload(verb, _ns(**{_dest("pkg"): ["a", "b"]})) == {
        "pkg": ["a", "b"]
    }
    assert dispatch._assemble_generated_payload(verb, _ns(**{_dest("pkg"): None})) == {}


def test_payload_folds_in_json_param() -> None:
    verb = _verb("systems.provision", flags=(_scalar("allocation_id"),), json_params=("profile",))
    args = _ns(
        **{
            _dest("allocation_id"): "al-1",
            f"{GENERATED_ARG_PREFIX}profile_json": '{"arch": "x86_64"}',
        }
    )
    assert dispatch._assemble_generated_payload(verb, args) == {
        "allocation_id": "al-1",
        "profile": {"arch": "x86_64"},
    }


def test_payload_json_param_array_folds_in() -> None:
    verb = _verb(
        "artifacts.create_run_upload", flags=(_scalar("run_id"),), json_params=("artifacts",)
    )
    args = _ns(
        **{
            _dest("run_id"): "run-1",
            f"{GENERATED_ARG_PREFIX}artifacts_json": '[{"name": "vmlinuz"}]',
        }
    )
    assert dispatch._assemble_generated_payload(verb, args) == {
        "run_id": "run-1",
        "artifacts": [{"name": "vmlinuz"}],
    }


def test_payload_absent_json_param_omitted() -> None:
    verb = _verb("systems.provision", flags=(_scalar("allocation_id"),), json_params=("profile",))
    args = _ns(**{_dest("allocation_id"): "al-1", f"{GENERATED_ARG_PREFIX}profile_json": None})
    assert dispatch._assemble_generated_payload(verb, args) == {"allocation_id": "al-1"}


def test_payload_unwrap_request_wraps_body() -> None:
    verb = _verb("accounting.report_granted_set", unwrap_request=True, flags=(_scalar("group_by"),))
    args = _ns(**{_dest("group_by"): "principal"})
    assert dispatch._assemble_generated_payload(verb, args) == {
        "request": {"group_by": "principal"}
    }


def test_payload_unwrap_request_empty_body_sends_no_key() -> None:
    verb = _verb("jobs.list", unwrap_request=True, flags=(_scalar("kind"),))
    args = _ns(**{_dest("kind"): None})
    assert dispatch._assemble_generated_payload(verb, args) == {}


# --- tier resolution + ceremony --------------------------------------------------------------


def test_read_only_verb_dispatches_with_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, _read_tool("systems.list"))
    assert _run(_verb("systems.list"), _ns()) == 0
    assert client.calls == [("systems.list", {})]


def test_mutating_verb_dispatches_without_any_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # Naming the verb is the acknowledgement; no --allow-mutating exists on a generated verb.
    client = _install(monkeypatch, _mutating_tool("resources.cordon"))
    assert _run(_verb("resources.cordon", read_only=False), _ns()) == 0
    assert client.calls == [("resources.cordon", {})]


def test_mutating_verb_preflight_refuses_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, _mutating_tool("resources.cordon"))
    monkeypatch.setattr(dispatch, "ensure_token_valid", _raise_expiring)
    assert _run(_verb("resources.cordon", read_only=False), _ns()) == 3
    assert client.calls == []


def test_read_only_verb_not_subject_to_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, _read_tool("systems.list"))
    monkeypatch.setattr(dispatch, "ensure_token_valid", _raise_expiring)
    assert _run(_verb("systems.list"), _ns()) == 0
    assert client.calls == [("systems.list", {})]


def test_destructive_verb_non_tty_without_yes_refused(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install(monkeypatch, _destructive_tool("control.force_crash"))
    assert (
        _run(_verb("control.force_crash", destructive=True, read_only=False), _ns(yes=False)) == 3
    )
    assert client.calls == []
    assert "--yes" in capsys.readouterr().out


def test_destructive_verb_with_yes_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, _destructive_tool("control.force_crash"))
    assert _run(_verb("control.force_crash", destructive=True, read_only=False), _ns(yes=True)) == 0
    assert client.calls == [("control.force_crash", {})]


def test_destructive_verb_preflight_refuses_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, _destructive_tool("control.force_crash"))
    monkeypatch.setattr(dispatch, "ensure_token_valid", _raise_expiring)
    assert _run(_verb("control.force_crash", destructive=True, read_only=False), _ns(yes=True)) == 3
    assert client.calls == []


def test_unclassifiable_tool_is_refused(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # The verb's tool is absent from the live list, so it classifies UNKNOWN and is unreachable.
    client = _install(monkeypatch, _read_tool("present.tool"))
    assert _run(_verb("absent.tool", destructive=True), _ns(yes=True)) == 3
    assert client.calls == []
    assert "not positively classified" in capsys.readouterr().out


def test_denied_envelope_maps_to_exit_3(monkeypatch: pytest.MonkeyPatch) -> None:
    denied = {
        "object_id": "h",
        "status": "error",
        "error_category": "authorization_denied",
        "data": {},
    }
    client = _install(monkeypatch, _mutating_tool("resources.cordon"), envelope=denied)
    assert _run(_verb("resources.cordon", read_only=False), _ns()) == 3
    # The call WAS dispatched; the denial is a returned envelope, not a client-side refusal.
    assert client.calls == [("resources.cordon", {})]


# --- live annotation governs the tier, not the committed artifact ----------------------------


def test_live_read_only_downgrade_skips_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    # The committed artifact marks the verb destructive, but the live server annotates it
    # read-only: the live annotation wins, so no confirmation is required and the call dispatches.
    client = _install(monkeypatch, _read_tool("some.tool"))
    assert _run(_verb("some.tool", destructive=True, read_only=False), _ns(yes=False)) == 0
    assert client.calls == [("some.tool", {})]


def test_live_destructive_upgrade_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # The committed artifact marks the verb read-only, but the live server annotates it
    # destructive: the live annotation wins, so the call is refused without confirmation.
    client = _install(monkeypatch, _destructive_tool("some.tool"))
    assert _run(_verb("some.tool", read_only=True, destructive=False), _ns()) == 3
    assert client.calls == []
    assert "confirmation" in capsys.readouterr().out


# --- payload reaches the call; rendering honors --json ---------------------------------------


def test_assembled_payload_reaches_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch, _read_tool("systems.get"))
    verb = _verb("systems.get", flags=(_scalar("system_id"),))
    assert _run(verb, _ns(**{_dest("system_id"): "sys-1"})) == 0
    assert client.calls == [("systems.get", {"system_id": "sys-1"})]


def test_json_output_prints_whole_envelope(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    envelope = {"object_id": "o", "status": "ok", "data": {}, "suggested_next_actions": ["x"]}
    _install(monkeypatch, _read_tool("systems.list"), envelope=envelope)
    assert _run(_verb("systems.list"), _ns(json=True)) == 0
    assert "suggested_next_actions" in capsys.readouterr().out


def test_default_output_renders_a_table(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    envelope = {"object_id": "sys-1", "status": "ready", "data": {}}
    _install(monkeypatch, _read_tool("systems.get"), envelope=envelope)
    assert _run(_verb("systems.get"), _ns(json=False)) == 0
    out = capsys.readouterr().out
    assert "sys-1" in out and "suggested_next_actions" not in out


# --- parser surface: --yes exists only on destructive generated verbs ------------------------


def test_destructive_generated_verb_accepts_yes() -> None:
    args = build_parser().parse_args(["control", "force-crash", "--system-id", "sys-1", "--yes"])
    assert args.yes is True


def test_destructive_generated_verb_defaults_yes_false() -> None:
    args = build_parser().parse_args(["control", "force-crash", "--system-id", "sys-1"])
    assert args.yes is False


def test_read_only_generated_verb_rejects_yes() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["accounting", "estimate", "--project", "p", "--yes"])
