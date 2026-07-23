"""``kdivectl images`` verbs call the right server tool with the expected payload.

The verbs are driven through fakes for the MCP client so the tests are hermetic. ``list``
is a read passthrough; ``upload``/``delete``/``build``/``publish``/``prune``/``extend`` are
mutating verbs that run the fail-closed token preflight first, then call their server tool
(ADR-0089). A denial envelope from the server maps to exit ``3``.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import pytest

import kdive.cli.commands.images as images
import kdive.cli.commands.mutations as mutations
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
    def __init__(self, client: _FakeClient, token: str = "x.y.z") -> None:
        self._client = client
        self.token = token

    def client(self) -> _FakeClient:
        return self._client


def _install(monkeypatch: pytest.MonkeyPatch, payload: dict | None = None) -> _FakeClient:
    client = _FakeClient(payload or {"object_id": "o", "status": "ok", "data": {}})
    monkeypatch.setattr(reads, "_session_factory", lambda: _FakeSession(client))
    monkeypatch.setattr(mutations, "_session_factory", lambda: _FakeSession(client))
    monkeypatch.setattr(mutations, "ensure_token_valid", lambda *a, **k: None)
    return client


def _args(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(json=False, **kwargs)


def _json_args(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(json=True, **kwargs)


def _collection(items: list[dict]) -> dict:
    return {
        "object_id": "images",
        "status": "ok",
        "data": {"count": len(items)},
        "items": items,
    }


def test_list_calls_images_list_read_tool(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _install(
        monkeypatch,
        _collection(
            [{"object_id": "i1", "status": "registered", "data": {"name": "fedora"}, "items": []}]
        ),
    )
    code = asyncio.run(images.images_list(_args()))
    assert code == 0
    assert client.calls == [("images.list", {})]
    assert "fedora" in capsys.readouterr().out


def test_get_calls_images_describe_read_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(
        monkeypatch, {"object_id": "img-1", "status": "registered", "data": {"name": "fedora"}}
    )
    code = asyncio.run(reads.images_get(_args(image_id="img-1")))
    assert code == 0
    assert client.calls == [("images.describe", {"image_id": "img-1"})]


def test_describe_threads_target_kernel_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(
        monkeypatch, {"object_id": "img-1", "status": "registered", "data": {"name": "fedora"}}
    )
    code = asyncio.run(reads.images_get(_args(image_id="img-1", target_kernel="7.1")))
    assert code == 0
    assert client.calls == [("images.describe", {"image_id": "img-1", "target_kernel": "7.1"})]


def test_describe_omits_target_kernel_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(
        monkeypatch, {"object_id": "img-1", "status": "registered", "data": {"name": "fedora"}}
    )
    code = asyncio.run(reads.images_get(_args(image_id="img-1", target_kernel=None)))
    assert code == 0
    assert client.calls == [("images.describe", {"image_id": "img-1"})]


def test_describe_verb_registered_read_only() -> None:
    by_tool = {verb.tool: verb for verb in REGISTRY if verb.group == "images"}
    assert by_tool["images.describe"].read_only is True


def test_describe_verb_declares_target_kernel_option() -> None:
    by_tool = {verb.tool: verb for verb in REGISTRY if verb.group == "images"}
    assert "target_kernel" in by_tool["images.describe"].options


def test_upload_calls_images_upload_with_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(
        images.images_upload(
            _args(
                project="proj-a",
                name="custom",
                arch="x86_64",
                quarantine_key="quarantine/abc",
                lifetime_seconds=3600,
            )
        )
    )
    assert client.calls == [
        (
            "images.upload",
            {
                "project": "proj-a",
                "name": "custom",
                "arch": "x86_64",
                "quarantine_key": "quarantine/abc",
                "lifetime_seconds": 3600,
            },
        )
    ]


def test_upload_omits_lifetime_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(
        images.images_upload(
            _args(
                project="proj-a",
                name="custom",
                arch="x86_64",
                quarantine_key="quarantine/abc",
                lifetime_seconds=None,
            )
        )
    )
    assert client.calls == [
        (
            "images.upload",
            {
                "project": "proj-a",
                "name": "custom",
                "arch": "x86_64",
                "quarantine_key": "quarantine/abc",
            },
        )
    ]


def test_delete_calls_images_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(images.images_delete(_args(image_id="img-1")))
    assert client.calls == [("images.delete", {"image_id": "img-1"})]


def test_build_calls_images_build(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(
        images.images_build(
            _args(
                provider="local-libvirt",
                name="fedora-40",
                packages=["crash", "drgn"],
            )
        )
    )
    assert client.calls == [
        (
            "images.build",
            {
                "request": {
                    "provider": "local-libvirt",
                    "name": "fedora-40",
                    "packages": ["crash", "drgn"],
                },
            },
        )
    ]


def test_build_trims_blank_package_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(
        images.images_build(
            _args(
                provider="local-libvirt",
                name="fedora-40",
                packages=[" crash ", " ", "drgn"],
            )
        )
    )
    assert client.calls[0] == (
        "images.build",
        {
            "request": {
                "provider": "local-libvirt",
                "name": "fedora-40",
                "packages": ["crash", "drgn"],
            },
        },
    )


def test_publish_calls_images_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(
        images.images_publish(
            _args(
                provider="local-libvirt",
                name="fedora-40",
                packages=["crash"],
            )
        )
    )
    assert client.calls[0][0] == "images.publish"


def test_prune_expired_calls_break_glass_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(images.images_prune(_args(expired=True, reason="cleanup")))
    assert client.calls == [("images.prune_expired", {"reason": "cleanup"})]


def test_prune_requires_expired_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    with pytest.raises(SystemExit):
        asyncio.run(images.images_prune(_args(expired=False, reason="x")))
    assert client.calls == []


def test_extend_calls_images_extend(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    asyncio.run(images.images_extend(_args(image_id="img-1", seconds=86400, reason="keep")))
    assert client.calls == [
        ("images.extend", {"image_id": "img-1", "seconds": 86400, "reason": "keep"})
    ]


def test_denied_envelope_maps_to_exit_3(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        payload={
            "object_id": "img-1",
            "status": "error",
            "error_category": "authorization_denied",
            "data": {},
        },
    )
    code = asyncio.run(images.images_delete(_args(image_id="img-1")))
    assert code == 3


def test_list_denial_envelope_maps_to_exit_3(monkeypatch: pytest.MonkeyPatch) -> None:
    # A server-side denial returns a failure envelope; images list must surface exit 3, not the
    # empty-success exit 0 that ignoring the envelope's error_category would leave (ADR-0089).
    _install(
        monkeypatch,
        payload={
            "object_id": "images",
            "status": "error",
            "error_category": "authorization_denied",
            "data": {},
            "items": [],
        },
    )
    code = asyncio.run(images.images_list(_args()))
    assert code == 3


def test_mutating_image_verbs_run_preflight_first(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient({"object_id": "o", "status": "ok", "data": {}})
    monkeypatch.setattr(mutations, "_session_factory", lambda: _FakeSession(client))

    class _Boom(RuntimeError):
        pass

    def _refuse(*_a: object, **_k: object) -> None:
        raise _Boom

    monkeypatch.setattr(mutations, "ensure_token_valid", _refuse)
    with pytest.raises(_Boom):
        asyncio.run(images.images_delete(_args(image_id="img-1")))
    assert client.calls == []


def test_list_renders_exact_column_set(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install(
        monkeypatch,
        _collection(
            [
                {
                    "object_id": "i1",
                    "status": "registered",
                    "data": {
                        "name": "fedora",
                        "arch": "x86_64",
                        "visibility": "public",
                        "owner": "platform",
                    },
                    "items": [],
                }
            ]
        ),
    )
    asyncio.run(images.images_list(_args()))
    header = capsys.readouterr().out.splitlines()[0]
    assert header.split() == ["id", "name", "arch", "visibility", "owner", "state"]


def test_list_json_emits_whole_envelope(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # --json is the server envelope verbatim, not the projected column set (ADR-0421 §6).
    envelope = _collection(
        [
            {
                "object_id": "i1",
                "status": "registered",
                "data": {"name": "fedora", "arch": "x86_64"},
                "items": [],
            }
        ]
    )
    envelope["suggested_next_actions"] = ["images.describe"]
    _install(monkeypatch, envelope)
    asyncio.run(images.images_list(_json_args()))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == envelope
    assert parsed["suggested_next_actions"] == ["images.describe"]
    assert parsed["items"][0]["object_id"] == "i1"


def test_list_missing_optional_attrs_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install(monkeypatch, _collection([]))
    bare = argparse.Namespace(json=False)
    code = asyncio.run(images.images_list(bare))
    assert code == 0
    assert client.calls == [("images.list", {})]


def test_upload_json_flag_threads_through_to_render(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install(monkeypatch, {"object_id": "o", "status": "ok", "data": {"name": "custom"}})
    asyncio.run(
        images.images_upload(
            _json_args(
                project="proj-a",
                name="custom",
                arch="x86_64",
                quarantine_key="quarantine/abc",
                lifetime_seconds=None,
            )
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["object_id"] == "o" and payload["data"]["name"] == "custom"


def test_upload_tolerates_missing_lifetime_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    bare = argparse.Namespace(
        json=False,
        project="proj-a",
        name="custom",
        arch="x86_64",
        quarantine_key="quarantine/abc",
    )
    asyncio.run(images.images_upload(bare))
    assert client.calls == [
        (
            "images.upload",
            {
                "project": "proj-a",
                "name": "custom",
                "arch": "x86_64",
                "quarantine_key": "quarantine/abc",
            },
        )
    ]


def test_delete_json_flag_threads_through_to_render(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install(monkeypatch, {"object_id": "img-1", "status": "deleted", "data": {}})
    asyncio.run(images.images_delete(_json_args(image_id="img-1")))
    payload = json.loads(capsys.readouterr().out)
    assert payload["object_id"] == "img-1" and payload["status"] == "deleted"


def test_build_json_flag_threads_through_to_render(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install(monkeypatch, {"object_id": "b1", "status": "queued", "data": {}})
    asyncio.run(
        images.images_build(
            _json_args(
                provider="local-libvirt",
                name="fedora-40",
                packages=["crash"],
            )
        )
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["object_id"] == "b1"


def test_build_tolerates_missing_packages_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    bare = argparse.Namespace(
        json=False,
        provider="local-libvirt",
        name="fedora-40",
    )
    asyncio.run(images.images_build(bare))
    assert client.calls[0][1]["request"]["packages"] == []


def test_publish_sends_request_envelope_and_threads_json(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    client = _install(monkeypatch, {"object_id": "p1", "status": "queued", "data": {}})
    asyncio.run(
        images.images_publish(
            _json_args(
                provider="local-libvirt",
                name="fedora-40",
                packages=["crash"],
            )
        )
    )
    name, arguments = client.calls[0]
    assert name == "images.publish"
    assert list(arguments.keys()) == ["request"]
    assert arguments["request"] == {
        "provider": "local-libvirt",
        "name": "fedora-40",
        "packages": ["crash"],
    }
    payload = json.loads(capsys.readouterr().out)
    assert payload["object_id"] == "p1"


def test_prune_exit_message_names_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(images.images_prune(_args(expired=False, reason="x")))
    assert str(excinfo.value) == "images prune is destructive: pass --expired to confirm the sweep"


def test_prune_refuses_when_expired_attr_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _install(monkeypatch)
    bare = argparse.Namespace(json=False, reason="x")
    with pytest.raises(SystemExit):
        asyncio.run(images.images_prune(bare))
    assert client.calls == []


def test_prune_json_flag_threads_through_to_render(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _install(monkeypatch, {"object_id": "sweep", "status": "ok", "data": {}})
    asyncio.run(images.images_prune(_json_args(expired=True, reason="cleanup")))
    payload = json.loads(capsys.readouterr().out)
    assert payload["object_id"] == "sweep"


def test_extend_json_flag_threads_through_to_render(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install(monkeypatch, {"object_id": "img-1", "status": "extended", "data": {}})
    asyncio.run(images.images_extend(_json_args(image_id="img-1", seconds=86400, reason="keep")))
    payload = json.loads(capsys.readouterr().out)
    assert payload["object_id"] == "img-1" and payload["status"] == "extended"


def test_image_verbs_registered_with_expected_read_only_flags() -> None:
    by_tool = {verb.tool: verb for verb in REGISTRY if verb.group == "images"}
    assert by_tool["images.list"].read_only is True
    for mutating in (
        "images.upload",
        "images.delete",
        "images.build",
        "images.publish",
        "images.prune_expired",
        "images.extend",
    ):
        assert by_tool[mutating].read_only is False
