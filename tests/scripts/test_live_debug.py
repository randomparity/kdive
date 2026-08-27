"""Unit tests for the live-debug dev driver."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import urllib.error
from email.message import Message
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from kdive.mcp.exposure import CORE_TOOLS
from kdive.mcp.schema.tool_index import NAMESPACE_TOC
from kdive.mcp.tools.gateway import _SEARCH_LIMIT_MAX
from scripts.operations import live_debug_build, live_debug_transport

ROOT = Path(__file__).resolve().parents[2]

_REQUEST_SCHEMA: dict[str, Any] = {"properties": {"request": {"type": "object"}}}
_FLAT_SCHEMA: dict[str, Any] = {"properties": {"allocation_id": {"type": "string"}}}
# The catalog tools.search can reach. `allocations.list` is deliberately NOT in CORE_TOOLS, so
# under the default surface it is reachable only through search — the case #1608 is about.
_SEARCHABLE: dict[str, dict[str, Any]] = {
    "allocations.list": _REQUEST_SCHEMA,
    "allocations.release": _FLAT_SCHEMA,
    "control.capture_traffic": _FLAT_SCHEMA,
    "runs.list": _REQUEST_SCHEMA,
}
# Ranking really is cross-namespace: on the live registry 35 of 123 tools do not rank first for
# their own exact name, and `jobs.wait` ranks behind `control.capture_traffic`. The fake ranks
# this decoy first for every query so neither `matches[0]` nor a namespace-prefix filter passes.
_TOP_RANKED_DECOY = "control.capture_traffic"


def _load_live_debug() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "live_debug_script", ROOT / "scripts/operations/live-debug.py"
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
        self.list_calls = 0

    async def list_tools(self) -> list[_SchemaTool]:
        self.list_calls += 1
        return self._tools


class _Client:
    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(
        self,
        tools: list[_SchemaTool] | None = None,
        searchable: dict[str, dict[str, Any]] | None = None,
        *,
        truncated: bool = False,
    ) -> None:
        self._client = _SchemaClient(tools or [])
        self._searchable = searchable or {}
        self._truncated = truncated

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, tool: str, **args: Any) -> _Envelope:
        self.calls.append((tool, args))
        if tool == "tools.search":
            return _Envelope({"status": "ok", "data": self._search(args)})
        return _Envelope({"object_id": tool, "status": "ok", "data": {"args": args}})

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        """The real ``tools.search`` envelope payload: ranked matches, each with its schema."""
        namespace = args.get("namespace")
        query = args.get("query")
        if namespace is not None:
            hits = sorted(n for n in self._searchable if n.startswith(f"{namespace}."))
        else:
            # The server scores lexically over name + description + keywords + schema text, which
            # is not namespace-aware: an unrelated tool routinely outranks the exact name. Rank
            # the decoy first and the exact match last, so neither `matches[0]` nor a
            # same-namespace filter can pass for the right reason.
            hits = sorted(
                (
                    n
                    for n in self._searchable
                    if n == _TOP_RANKED_DECOY or _same_namespace(n, str(query))
                ),
                key=lambda n: (n != _TOP_RANKED_DECOY, n == query, n),
            )
        if self._truncated:
            # A clipped result set drops the tail, which is where the exact match was ranked.
            hits = hits[:1]
        matches = [{"name": n, "input_schema": self._searchable[n]} for n in hits]
        return {"matches": matches, "truncated": self._truncated}


def _same_namespace(name: str, query: str) -> bool:
    return name.split(".", 1)[0] == query.split(".", 1)[0]


def _gateway_surface_client(*, truncated: bool = False) -> _Client:
    """A client shaped like the DEFAULT surface: ``list_tools`` advertises only ``CORE_TOOLS``.

    The core set is imported from the server rather than hard-coded so this fake cannot drift
    into advertising a catalog the script no longer sees (ADR-0456; the gateway is on by default).
    ``truncated`` makes every ``tools.search`` report a clipped result set.
    """
    listed = [_SchemaTool(name, _REQUEST_SCHEMA) for name in sorted(CORE_TOOLS)]
    return _Client(listed, dict(_SEARCHABLE), truncated=truncated)


def _bind_client(monkeypatch: pytest.MonkeyPatch, live_debug: Any, client: _Client) -> None:
    """Point the script's client factory and token minter at ``client``."""

    class Factory:
        @staticmethod
        def over_http(base: str, token: str) -> _Client:
            del base, token
            return client

    monkeypatch.setattr(live_debug, "_token", lambda project: project)
    monkeypatch.setattr(live_debug, "LiveStackClient", Factory)


def test_wrap_request_only_for_single_request_schema() -> None:
    live_debug = live_debug_transport
    request_schema = {"properties": {"request": {"type": "object"}}}
    flat_schema = {"properties": {"run_id": {"type": "string"}}}

    assert live_debug._wrap_request(request_schema, {"project": "demo"}) == {
        "request": {"project": "demo"}
    }
    assert live_debug._wrap_request(request_schema, {"request": {"project": "demo"}}) == {
        "request": {"project": "demo"}
    }
    assert live_debug._wrap_request(flat_schema, {"run_id": "r1"}) == {"run_id": "r1"}


def test_call_uses_listed_schema_without_searching_for_a_core_tool() -> None:
    live_debug = live_debug_transport
    _Client.calls = []
    client = _gateway_surface_client()
    assert "runs.list" in CORE_TOOLS  # premise: the default surface does advertise it

    result = asyncio.run(
        live_debug._call(
            cast(Any, client),
            "runs.list",
            {"project": "demo"},
            live_debug._SchemaResolver(),
        )
    )

    assert result["object_id"] == "runs.list"
    assert _Client.calls == [("runs.list", {"request": {"project": "demo"}})]


def test_call_resolves_a_non_core_tool_schema_through_tools_search() -> None:
    """#1608: the default surface lists only CORE_TOOLS, so everything else resolves by search."""
    live_debug = live_debug_transport
    _Client.calls = []
    client = _gateway_surface_client()
    # The premise of the bug: this tool is callable but absent from the advertised catalog, so
    # the old `list_tools`-only map had no schema for it and sent flat kwargs the server rejects.
    assert "allocations.list" not in CORE_TOOLS

    result = asyncio.run(
        live_debug._call(
            cast(Any, client),
            "allocations.list",
            {"project": "demo"},
            live_debug._SchemaResolver(),
        )
    )

    assert result["object_id"] == "allocations.list"
    assert _Client.calls[0] == (
        "tools.search",
        {"query": "allocations.list", "limit": _SEARCH_LIMIT_MAX},
    )
    # The searched schema is the single-`request` one, so the flat args got wrapped. Taking
    # `matches[0]` instead would have picked the higher-ranked flat sibling and left them flat.
    assert _Client.calls[1] == ("allocations.list", {"request": {"project": "demo"}})


def test_schema_resolution_is_cached_across_calls() -> None:
    """A poller re-enters _call every few seconds; each tool must cost at most one search."""
    live_debug = live_debug_transport
    _Client.calls = []
    client = _gateway_surface_client()
    schemas = live_debug._SchemaResolver()

    for _ in range(3):
        asyncio.run(
            live_debug._call(cast(Any, client), "allocations.list", {"project": "demo"}, schemas)
        )

    assert [tool for tool, _ in _Client.calls].count("tools.search") == 1
    assert client._client.list_calls == 1


def test_schema_resolution_caches_and_reports_a_miss(capsys: pytest.CaptureFixture[str]) -> None:
    live_debug = live_debug_transport
    _Client.calls = []
    client = _gateway_surface_client()
    schemas = live_debug._SchemaResolver()

    # Neither surface knows this name. The call still goes out (and fails server-side with a
    # real error), but the fruitless search must not repeat on every poll tick.
    for _ in range(3):
        asyncio.run(live_debug._call(cast(Any, client), "nowhere.missing", {"x": 1}, schemas))

    assert [tool for tool, _ in _Client.calls].count("tools.search") == 1
    assert _Client.calls[-1] == ("nowhere.missing", {"x": 1})
    # The args go out unwrapped, so name the reason once rather than leaving the user to decode
    # the server-side validation error that follows.
    err = capsys.readouterr().err
    assert "no input schema for 'nowhere.missing'" in err
    assert "clipped" not in err  # this result set was complete, only the name was unknown


def test_a_clipped_search_says_so_instead_of_blaming_the_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    live_debug = live_debug_transport
    _Client.calls = []
    client = _gateway_surface_client(truncated=True)

    asyncio.run(
        live_debug._call(
            cast(Any, client),
            "allocations.list",
            {"project": "demo"},
            live_debug._SchemaResolver(),
        )
    )

    # The exact match ranks last, so clipping drops it: "unknown tool" would be the wrong story.
    assert "no input schema for 'allocations.list' (results were clipped" in capsys.readouterr().err
    assert _Client.calls[-1] == ("allocations.list", {"project": "demo"})


def test_input_schemas_reads_harness_tool_catalog() -> None:
    live_debug = live_debug_transport
    client = _Client(
        [
            _SchemaTool("runs.list", {"properties": {"request": {"type": "object"}}}),
            _SchemaTool("debug.continue", {"properties": {"session_id": {"type": "string"}}}),
        ]
    )

    schemas = asyncio.run(live_debug._input_schemas(cast(Any, client)))

    assert schemas["runs.list"]["properties"] == {"request": {"type": "object"}}
    assert schemas["debug.continue"]["properties"] == {"session_id": {"type": "string"}}


def test_poll_waits_until_terminal_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    live_debug = live_debug_transport
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
        assert tool == "jobs.wait"
        assert args == {"job_id": "j1"}
        return next(states)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(live_debug, "_call", fake_call)
    monkeypatch.setattr(live_debug.asyncio, "sleep", fake_sleep)

    result = asyncio.run(
        live_debug._poll(
            cast(Any, object()),
            "jobs.wait",
            {"job_id": "j1"},
            live_debug._SchemaResolver(),
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
    live_debug = live_debug_transport
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

    asyncio.run(
        live_debug._wait_job(
            cast(Any, object()),
            live_debug._SchemaResolver(),
            kind="boot",
            timeout_sec=30,
        )
    )

    assert seen_poll_args == {
        "tool": "jobs.wait",
        "args": {"job_id": "boot-1", "timeout_s": 0},
        "done": live_debug._JOB_DONE,
        "timeout_sec": 30,
        "label": "boot",
    }


def _built_kernel_src(tmp_path: Path) -> Path:
    kernel_src = tmp_path / "linux"
    (kernel_src / "arch/x86/boot").mkdir(parents=True)
    (kernel_src / "arch/x86/boot/bzImage").write_bytes(b"bz")
    return kernel_src


def _record_build_runs(
    monkeypatch: pytest.MonkeyPatch, live_debug: Any
) -> tuple[list[list[str]], list[dict[str, Any]]]:
    calls: list[list[str]] = []
    kwargs_seen: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> object:
        calls.append(cmd)
        kwargs_seen.append(kwargs)
        if cmd[0].endswith("tar"):  # the tar invocation writes the archive
            Path(cmd[cmd.index("-cf") + 1 if "-cf" in cmd else cmd.index("-czf") + 1]).write_bytes(
                b"tar"
            )
        return object()

    monkeypatch.setattr(live_debug, "_required_executable", lambda name: f"/bin/{name}")
    monkeypatch.setattr(live_debug.subprocess, "run", fake_run)
    return calls, kwargs_seen


def test_combined_kernel_tar_runs_recipe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live_debug = live_debug_build
    kernel_src = _built_kernel_src(tmp_path)
    dest = tmp_path / "scratch"
    dest.mkdir()
    monkeypatch.setattr(live_debug.shutil, "which", lambda name: None)  # no pigz -> gzip fallback
    calls, kwargs_seen = _record_build_runs(monkeypatch, live_debug)

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
    # every build step is bounded so a stall fails loudly instead of hanging
    assert all(kw.get("timeout") == live_debug._ARCHIVE_STEP_TIMEOUT_S for kw in kwargs_seen)


def test_combined_kernel_tar_uses_pigz_when_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live_debug = live_debug_build
    kernel_src = _built_kernel_src(tmp_path)
    dest = tmp_path / "scratch"
    dest.mkdir()
    monkeypatch.setattr(
        live_debug.shutil, "which", lambda name: "/usr/bin/pigz" if name == "pigz" else None
    )
    calls, _ = _record_build_runs(monkeypatch, live_debug)

    result = live_debug._combined_kernel_tar(kernel_src, dest)

    assert result.read_bytes() == b"tar"
    tar_cmd = calls[1]
    assert tar_cmd[:5] == [
        "/bin/tar",
        "-I",
        "/usr/bin/pigz",
        "-cf",
        str(dest / "kernel.tar.gz"),
    ]
    assert "--transform=s|^arch/x86/boot/bzImage$|boot/vmlinuz|" in tar_cmd


def test_combined_kernel_tar_times_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live_debug = live_debug_build
    kernel_src = _built_kernel_src(tmp_path)
    dest = tmp_path / "scratch"
    dest.mkdir()

    def fake_run(cmd: list[str], **_kwargs: Any) -> object:
        raise live_debug.subprocess.TimeoutExpired(cmd, live_debug._ARCHIVE_STEP_TIMEOUT_S)

    monkeypatch.setattr(live_debug, "_required_executable", lambda name: f"/bin/{name}")
    monkeypatch.setattr(live_debug.shutil, "which", lambda name: None)
    monkeypatch.setattr(live_debug.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="modules_install exceeded 900s"):
        live_debug._combined_kernel_tar(kernel_src, dest)


def test_combined_kernel_tar_requires_built_bzimage(tmp_path: Path) -> None:
    live_debug = live_debug_build
    with pytest.raises(RuntimeError, match="packages x86_64 kernels only"):
        live_debug._combined_kernel_tar(tmp_path / "unbuilt", tmp_path / "scratch")


def test_upload_kernel_drives_declare_put_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live_debug = live_debug_build
    kernel_tar = tmp_path / "kernel.tar.gz"
    kernel_tar.write_bytes(b"kernel-bytes")
    vmlinux = tmp_path / "vmlinux"
    vmlinux.write_bytes(b"\x7fELF-bytes")
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    put_calls: list[tuple[dict[str, Any], Path]] = []
    contract = {
        "upload_contracts": {"run": {"owner_kind": "run", "accepted_names": ["kernel", "vmlinux"]}}
    }

    class FakeClient:
        async def read_text_resource(self, uri: str) -> str:
            assert uri == live_debug.EXTERNAL_BUILD_CONTRACT_URI
            return json.dumps(contract)

    async def fake_call(
        _client: object, tool: str, args: dict[str, Any], _schemas: dict[str, Any]
    ) -> dict[str, Any]:
        tool_calls.append((tool, args))
        if tool == "artifacts.create_run_upload":
            return {
                "items": [
                    {"refs": {"upload_url": f"http://s3/{name}"}, "data": {"name": name}}
                    for name in ("kernel", "vmlinux")
                ]
            }
        return {"object_id": tool}

    async def fake_put(item: dict[str, Any], path: Path) -> None:
        put_calls.append((item, path))

    monkeypatch.setattr(live_debug, "_call", fake_call)
    monkeypatch.setattr(live_debug, "_put_presigned", fake_put)
    monkeypatch.setattr(live_debug, "_elf_build_id", lambda _path: "deadbeef")

    asyncio.run(
        live_debug._upload_kernel(
            cast(Any, FakeClient()),
            live_debug._SchemaResolver(),
            run_id="r1",
            kernel_tar=kernel_tar,
            vmlinux=vmlinux,
        )
    )

    names = [tool for tool, _ in tool_calls]
    assert names == [
        "artifacts.create_run_upload",
        "runs.complete_build",
    ]
    decls = {d["name"]: d for d in tool_calls[0][1]["artifacts"]}
    assert set(decls) == {"kernel", "vmlinux"}
    assert decls["kernel"]["size_bytes"] == len(b"kernel-bytes")
    assert decls["kernel"]["sha256"] == live_debug._sha256_b64(kernel_tar)
    assert decls["vmlinux"]["sha256"] == live_debug._sha256_b64(vmlinux)
    # A declared vmlinux is rejected server-side unless complete_build carries its build-id.
    assert tool_calls[1][1] == {"run_id": "r1", "build_id": "deadbeef"}
    assert [path for _item, path in put_calls] == [kernel_tar, vmlinux]


def test_upload_kernel_rejects_missing_kernel_contract(tmp_path: Path) -> None:
    live_debug = live_debug_build
    kernel_tar = tmp_path / "kernel.tar.gz"
    kernel_tar.write_bytes(b"k")
    vmlinux = tmp_path / "vmlinux"
    vmlinux.write_bytes(b"\x7fELF")
    # Only `kernel` is accepted: a contract that drops `vmlinux` must fail loudly rather than
    # silently upload a Run with no debuginfo, which degrades every gdb-MI op to no_debuginfo.
    contract = {
        "upload_contracts": {"run": {"owner_kind": "run", "accepted_names": ["rootfs", "kernel"]}}
    }

    class FakeClient:
        async def read_text_resource(self, uri: str) -> str:
            assert uri == live_debug.EXTERNAL_BUILD_CONTRACT_URI
            return json.dumps(contract)

    with pytest.raises(RuntimeError, match="no longer accepts run artifact"):
        asyncio.run(
            live_debug._upload_kernel(
                cast(Any, FakeClient()),
                live_debug._SchemaResolver(),
                run_id="r1",
                kernel_tar=kernel_tar,
                vmlinux=vmlinux,
            )
        )


def test_elf_build_id_reads_the_gnu_note(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live_debug = live_debug_build
    vmlinux = tmp_path / "vmlinux"
    vmlinux.write_bytes(b"\x7fELF")
    readelf_out = (
        "Displaying notes found in: .notes\n"
        "  Owner  Data size  Description\n"
        "  GNU    0x00000014 NT_GNU_BUILD_ID (unique build ID bitstring)\n"
        "    Build ID: b7813d588bd6355e318d515f9577e5208f42fc8e\n"
    )

    monkeypatch.setattr(live_debug, "_required_executable", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        live_debug.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(stdout=readelf_out),
    )

    assert live_debug._elf_build_id(vmlinux) == "b7813d588bd6355e318d515f9577e5208f42fc8e"


def test_elf_build_id_fails_loud_without_a_note(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live_debug = live_debug_build
    vmlinux = tmp_path / "vmlinux"
    vmlinux.write_bytes(b"\x7fELF")

    monkeypatch.setattr(live_debug, "_required_executable", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        live_debug.subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="no notes here\n")
    )

    # Uploading a vmlinux without a build-id would be rejected by complete_build anyway; failing
    # here names the real cause (a tree built without CONFIG_DEBUG_INFO_DWARF5). Prompted symbol
    # only - bare DEBUG_INFO is a symbol olddefconfig discards (#1871).
    with pytest.raises(RuntimeError, match="no GNU build-id note") as exc:
        live_debug._elf_build_id(vmlinux)
    assert "CONFIG_DEBUG_INFO_DWARF5=y" in str(exc.value)
    assert "CONFIG_DEBUG_INFO=y" not in str(exc.value)


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


def test_cmd_tools_enumerates_namespaces_instead_of_list_tools(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The discovery command must not inherit the profile-clipped list_tools catalog."""
    live_debug = _load_live_debug()
    _Client.calls = []
    client = _gateway_surface_client()

    _bind_client(monkeypatch, live_debug, client)

    rc = live_debug.asyncio.run(
        live_debug._cmd_tools(argparse.Namespace(project="demo", substr=None))
    )

    assert rc == 0
    searched = [args["namespace"] for tool, args in _Client.calls if tool == "tools.search"]
    assert searched == sorted(NAMESPACE_TOC)
    assert all(
        args["limit"] == _SEARCH_LIMIT_MAX for tool, args in _Client.calls if tool == "tools.search"
    )
    assert client._client.list_calls == 0  # list_tools would have shown only the core nine
    # `allocations.list` is not a core tool, so only the namespace browse can surface it.
    assert capsys.readouterr().out.splitlines() == sorted(_SEARCHABLE)


def test_cmd_tools_warns_when_a_namespace_listing_is_clipped(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A namespace bigger than the search cap loses tools; say so rather than under-report."""
    live_debug = _load_live_debug()
    _Client.calls = []
    client = _gateway_surface_client(truncated=True)
    _bind_client(monkeypatch, live_debug, client)

    rc = live_debug.asyncio.run(
        live_debug._cmd_tools(argparse.Namespace(project="demo", substr=None))
    )

    assert rc == 0
    captured = capsys.readouterr()
    assert "holds more than" in captured.err
    assert "'allocations'" in captured.err
    # `allocations.release` fell off the clipped page, which is exactly what the warning is for.
    assert captured.out.splitlines() == [
        "allocations.list",
        "control.capture_traffic",
        "runs.list",
    ]


def test_cmd_tools_filters_by_substring(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live_debug = _load_live_debug()
    _Client.calls = []
    client = _gateway_surface_client()

    _bind_client(monkeypatch, live_debug, client)

    rc = live_debug.asyncio.run(
        live_debug._cmd_tools(argparse.Namespace(project="demo", substr="release"))
    )

    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["allocations.release"]


def test_cmd_schema_resolves_a_non_core_tool(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    live_debug = _load_live_debug()
    _Client.calls = []
    client = _gateway_surface_client()

    _bind_client(monkeypatch, live_debug, client)

    rc = live_debug.asyncio.run(
        live_debug._cmd_schema(argparse.Namespace(project="demo", tools=["allocations.list"]))
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "### allocations.list" in out
    assert json.loads(out.split("\n", 1)[1]) == _REQUEST_SCHEMA["properties"]


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
