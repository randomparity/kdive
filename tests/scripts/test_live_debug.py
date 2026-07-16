"""Unit tests for the live-debug dev driver."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import urllib.error
from email.message import Message
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_live_debug() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "live_debug_script", ROOT / "scripts/live-debug.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Envelope:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return self._payload


class _SchemaTool:
    def __init__(self, name: str, schema: dict[str, Any]) -> None:
        self.name = name
        self.inputSchema = schema


class _SchemaClient:
    def __init__(self, tools: list[_SchemaTool]) -> None:
        self._tools = tools

    async def list_tools(self) -> list[_SchemaTool]:
        return self._tools


class _Client:
    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, tools: list[_SchemaTool] | None = None) -> None:
        self._client = _SchemaClient(tools or [])

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, tool: str, **args: Any) -> _Envelope:
        self.calls.append((tool, args))
        return _Envelope({"object_id": tool, "status": "ok", "data": {"args": args}})


def test_wrap_request_only_for_single_request_schema() -> None:
    live_debug = _load_live_debug()
    request_schema = {"properties": {"request": {"type": "object"}}}
    flat_schema = {"properties": {"run_id": {"type": "string"}}}

    assert live_debug._wrap_request(request_schema, {"project": "demo"}) == {
        "request": {"project": "demo"}
    }
    assert live_debug._wrap_request(request_schema, {"request": {"project": "demo"}}) == {
        "request": {"project": "demo"}
    }
    assert live_debug._wrap_request(flat_schema, {"run_id": "r1"}) == {"run_id": "r1"}


def test_call_uses_input_schema_to_wrap_request() -> None:
    live_debug = _load_live_debug()
    _Client.calls = []
    schemas = {"runs.list": {"properties": {"request": {"type": "object"}}}}

    result = asyncio.run(live_debug._call(_Client(), "runs.list", {"project": "demo"}, schemas))

    assert result["object_id"] == "runs.list"
    assert _Client.calls == [("runs.list", {"request": {"project": "demo"}})]


def test_input_schemas_reads_harness_tool_catalog() -> None:
    live_debug = _load_live_debug()
    client = _Client(
        [
            _SchemaTool("runs.list", {"properties": {"request": {"type": "object"}}}),
            _SchemaTool("debug.continue", {"properties": {"session_id": {"type": "string"}}}),
        ]
    )

    schemas = asyncio.run(live_debug._input_schemas(client))

    assert schemas["runs.list"]["properties"] == {"request": {"type": "object"}}
    assert schemas["debug.continue"]["properties"] == {"session_id": {"type": "string"}}


def test_poll_waits_until_terminal_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live_debug = _load_live_debug()
    states = iter(
        [
            {"object_id": "job", "status": "running", "data": {"state": "running"}},
            {"object_id": "job", "status": "ok", "data": {"state": "succeeded"}},
        ]
    )
    sleeps: list[float] = []

    async def fake_call(
        _client: object, tool: str, args: dict[str, Any], _schemas: dict[str, Any]
    ) -> dict[str, Any]:
        assert tool == "jobs.get"
        assert args == {"job_id": "j1"}
        return next(states)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(live_debug, "_call", fake_call)
    monkeypatch.setattr(live_debug.asyncio, "sleep", fake_sleep)

    result = asyncio.run(
        live_debug._poll(
            object(),
            "jobs.get",
            {"job_id": "j1"},
            {},
            done={"succeeded"},
            timeout_sec=60,
            label="boot",
        )
    )

    assert result["data"]["state"] == "succeeded"
    assert sleeps == [live_debug._POLL_INTERVAL_SEC]
    assert "[boot] running" in capsys.readouterr().err


def test_wait_job_selects_matching_job_and_requires_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_debug = _load_live_debug()
    seen_poll_args: dict[str, Any] = {}

    async def fake_call(
        _client: object, tool: str, args: dict[str, Any], _schemas: dict[str, Any]
    ) -> dict[str, Any]:
        assert tool == "jobs.list"
        assert args == {"limit": 20}
        return {
            "items": [
                {"object_id": "old-build", "data": {"kind": "build"}},
                {"object_id": "boot-1", "data": {"kind": "boot"}},
            ]
        }

    async def fake_poll(
        _client: object,
        tool: str,
        args: dict[str, Any],
        _schemas: dict[str, Any],
        *,
        done: set[str],
        timeout_sec: float,
        label: str,
    ) -> dict[str, Any]:
        seen_poll_args.update(
            {
                "tool": tool,
                "args": args,
                "done": done,
                "timeout_sec": timeout_sec,
                "label": label,
            }
        )
        return {"status": "ok", "data": {"status": "completed"}}

    monkeypatch.setattr(live_debug, "_call", fake_call)
    monkeypatch.setattr(live_debug, "_poll", fake_poll)

    asyncio.run(live_debug._wait_job(object(), {}, kind="boot", timeout_sec=30))

    assert seen_poll_args == {
        "tool": "jobs.get",
        "args": {"job_id": "boot-1"},
        "done": live_debug._JOB_DONE,
        "timeout_sec": 30,
        "label": "boot",
    }


def test_combined_kernel_tar_runs_recipe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live_debug = _load_live_debug()
    kernel_src = tmp_path / "linux"
    (kernel_src / "arch/x86/boot").mkdir(parents=True)
    (kernel_src / "arch/x86/boot/bzImage").write_bytes(b"bz")
    dest = tmp_path / "scratch"
    dest.mkdir()
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> object:
        calls.append(cmd)
        if cmd[1] == "-czf":  # the tar invocation writes the archive
            Path(cmd[2]).write_bytes(b"tar")
        return object()

    monkeypatch.setattr(live_debug, "_required_executable", lambda name: f"/bin/{name}")
    monkeypatch.setattr(live_debug.subprocess, "run", fake_run)

    result = live_debug._combined_kernel_tar(kernel_src, dest)

    assert result == dest / "kernel.tar.gz"
    assert result.read_bytes() == b"tar"
    assert calls[0] == [
        "/bin/make",
        "-C",
        str(kernel_src),
        "modules_install",
        f"INSTALL_MOD_PATH={dest / 'modstage'}",
    ]
    tar_cmd = calls[1]
    assert tar_cmd[:3] == ["/bin/tar", "-czf", str(dest / "kernel.tar.gz")]
    assert "--transform=s|^arch/x86/boot/bzImage$|boot/vmlinuz|" in tar_cmd
    assert "--exclude=*/build" in tar_cmd and "--exclude=*/source" in tar_cmd


def test_combined_kernel_tar_requires_built_bzimage(tmp_path: Path) -> None:
    live_debug = _load_live_debug()
    with pytest.raises(RuntimeError, match="no built bzImage"):
        live_debug._combined_kernel_tar(tmp_path / "unbuilt", tmp_path / "scratch")


def test_upload_kernel_drives_declare_put_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live_debug = _load_live_debug()
    kernel_tar = tmp_path / "kernel.tar.gz"
    kernel_tar.write_bytes(b"kernel-bytes")
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    put_calls: list[tuple[dict[str, Any], Path]] = []

    async def fake_call(
        _client: object, tool: str, args: dict[str, Any], _schemas: dict[str, Any]
    ) -> dict[str, Any]:
        tool_calls.append((tool, args))
        if tool == "artifacts.expected_uploads":
            return {"items": [{"data": {"owner_kind": "run", "accepted_names": ["kernel"]}}]}
        if tool == "artifacts.create_run_upload":
            return {
                "items": [{"refs": {"upload_url": "http://s3/kernel"}, "data": {"name": "kernel"}}]
            }
        return {"object_id": tool}

    async def fake_put(item: dict[str, Any], path: Path) -> None:
        put_calls.append((item, path))

    monkeypatch.setattr(live_debug, "_call", fake_call)
    monkeypatch.setattr(live_debug, "_put_presigned", fake_put)

    asyncio.run(live_debug._upload_kernel(object(), {}, run_id="r1", kernel_tar=kernel_tar))

    names = [tool for tool, _ in tool_calls]
    assert names == [
        "artifacts.expected_uploads",
        "artifacts.create_run_upload",
        "runs.complete_build",
    ]
    decl = tool_calls[1][1]["artifacts"][0]
    assert decl["name"] == "kernel"
    assert decl["size_bytes"] == len(b"kernel-bytes")
    assert decl["sha256"] == live_debug._sha256_b64(kernel_tar)
    assert tool_calls[2][1] == {"run_id": "r1"}
    assert len(put_calls) == 1
    assert put_calls[0][0]["refs"]["upload_url"] == "http://s3/kernel"
    assert put_calls[0][1] == kernel_tar


def test_upload_kernel_rejects_missing_kernel_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live_debug = _load_live_debug()
    kernel_tar = tmp_path / "kernel.tar.gz"
    kernel_tar.write_bytes(b"k")

    async def fake_call(
        _client: object, _tool: str, _args: dict[str, Any], _schemas: dict[str, Any]
    ) -> dict[str, Any]:
        return {"items": [{"data": {"owner_kind": "run", "accepted_names": ["rootfs"]}}]}

    monkeypatch.setattr(live_debug, "_call", fake_call)

    with pytest.raises(RuntimeError, match="no longer accepts a 'kernel'"):
        asyncio.run(live_debug._upload_kernel(object(), {}, run_id="r1", kernel_tar=kernel_tar))


def test_transcript_renders_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live_debug = _load_live_debug()
    transcript = tmp_path / "session-1.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "command": "-exec-continue",
                "records": [
                    {
                        "type": "result",
                        "message": "done",
                        "payload": {"bkptno": "1"},
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(live_debug, "DEBUG_DIR", tmp_path)

    rc = live_debug._cmd_transcript(argparse.Namespace(session_id="session-1"))

    out = capsys.readouterr().out
    assert rc == 0
    assert "$ -exec-continue" in out
    assert 'result/done: {"bkptno": "1"}' in out


def test_reload_restarts_server_and_accepts_http_error_as_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    live_debug = _load_live_debug()
    run_calls: list[list[str]] = []
    pids = iter([[1234], []])

    def fake_server_pids() -> list[int]:
        return next(pids)

    def fake_run(cmd: list[str], **_kwargs: Any) -> object:
        run_calls.append(cmd)
        return object()

    def fake_urlopen(_url: str, *, timeout: int) -> object:
        assert timeout == 2
        raise urllib.error.HTTPError(_url, 404, "not found", hdrs=Message(), fp=None)

    monkeypatch.setattr(live_debug, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(live_debug, "BASE_URL", "http://127.0.0.1:8000/mcp")
    monkeypatch.setattr(live_debug, "_server_pids", fake_server_pids)
    monkeypatch.setattr(live_debug, "_required_executable", lambda name: f"/bin/{name}")
    monkeypatch.setattr(live_debug.subprocess, "run", fake_run)
    monkeypatch.setattr(live_debug.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(live_debug.urllib.request, "urlopen", fake_urlopen)

    rc = live_debug._cmd_reload(argparse.Namespace())

    assert rc == 0
    assert run_calls[0] == ["/bin/kill", "1234"]
    assert run_calls[1][:2] == ["/bin/bash", "-c"]
    assert run_calls[1][2].startswith(f"cd {tmp_path} &&")
    assert "-m kdive server" in run_calls[1][2]


def test_main_routes_sync_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    live_debug = _load_live_debug()
    seen: list[str] = []

    def fake_transcript(args: argparse.Namespace) -> int:
        seen.append(f"transcript:{args.session_id}")
        return 7

    def fake_reload(args: argparse.Namespace) -> int:
        seen.append(args.command)
        return 8

    monkeypatch.setattr(live_debug, "_cmd_transcript", fake_transcript)
    monkeypatch.setattr(live_debug, "_cmd_reload", fake_reload)

    assert live_debug.main(["transcript", "s1"]) == 7
    assert live_debug.main(["reload"]) == 8
    assert seen == ["transcript:s1", "reload"]


def test_cmd_call_uses_live_stack_client_factory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live_debug = _load_live_debug()
    _Client.calls = []
    client = _Client([_SchemaTool("runs.list", {"properties": {"request": {"type": "object"}}})])

    class Factory:
        @staticmethod
        def over_http(base: str, token: str) -> _Client:
            assert base == "http://example/mcp"
            assert token == "token"
            return client

    monkeypatch.setattr(live_debug, "BASE_URL", "http://example/mcp")
    monkeypatch.setattr(live_debug, "_token", lambda project: f"{project}")
    monkeypatch.setattr(live_debug, "LiveStackClient", Factory)
    args = argparse.Namespace(project="token", tool="runs.list", args='{"project": "demo"}')

    rc = asyncio.run(live_debug._cmd_call(args))

    assert rc == 0
    assert _Client.calls == [("runs.list", {"request": {"project": "demo"}})]
    assert json.loads(capsys.readouterr().out)["data"]["args"] == {"request": {"project": "demo"}}
