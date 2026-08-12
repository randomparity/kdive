"""mint-system.sh validates its preconditions before any stack call (#1293, ADR-0389).

The live mint (allocate -> provision -> ready) needs a running stack and is proven by the operator
nightly / the local native smoke (plan Task 7), not CI. This test pins the fail-loud preconditions:
an absent warm rootfs or stack URL dies before any HTTP call, so a misconfigured job fails at the
boundary, not deep in provisioning.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live-vm" / "mint-system.sh"


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env},
    )


def test_dies_without_rootfs() -> None:
    r = _run({"KDIVE_STACK_BASE_URL": "http://127.0.0.1:8000"})
    assert r.returncode != 0
    assert "KDIVE_LIVE_VM_ROOTFS" in r.stderr


def test_dies_without_stack_url(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"x")
    r = _run({"KDIVE_LIVE_VM_ROOTFS": str(rootfs)})
    assert r.returncode != 0
    assert "KDIVE_STACK_BASE_URL" in r.stderr


def test_dies_when_rootfs_path_missing(tmp_path: Path) -> None:
    r = _run(
        {
            "KDIVE_LIVE_VM_ROOTFS": str(tmp_path / "nope.qcow2"),
            "KDIVE_STACK_BASE_URL": "http://127.0.0.1:8000",
        }
    )
    assert r.returncode != 0
    assert "KDIVE_LIVE_VM_ROOTFS" in r.stderr


def test_polls_system_status_through_gateway(tmp_path: Path) -> None:
    package = tmp_path / "kdive" / "mcp"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "dev_harness.py").write_text(
        """
import json
import os
from types import SimpleNamespace


class LiveStackClient:
    @classmethod
    def over_http(cls, base, token):
        return cls()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def call_tool(self, tool_name, **arguments):
        with open(os.environ["CALLS_PATH"], "a") as calls:
            calls.write(json.dumps({"name": tool_name, "arguments": arguments}) + "\\n")
        if tool_name == "allocations.request":
            return SimpleNamespace(status="ok", object_id="allocation-1")
        if tool_name == "systems.provision":
            return SimpleNamespace(status="ok", data={"system_id": "system-1"})
        if tool_name == "tools.invoke":
            return SimpleNamespace(status="ready")
        if tool_name == "systems.get":
            raise AssertionError("systems.get is not directly exposed by the gateway profile")
        return SimpleNamespace(status="ok")
"""
    )
    script = _SCRIPT.read_text()
    embedded = script.partition("<<'PY'\n")[2].rsplit("\nPY", 1)[0]
    calls_path = tmp_path / "calls.jsonl"

    result = subprocess.run(
        [sys.executable, "-c", embedded, str(tmp_path / "rootfs.qcow2")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(tmp_path),
            "CALLS_PATH": str(calls_path),
            "KDIVE_STACK_BASE_URL": "http://127.0.0.1:8000",
            "KDIVE_TOKEN": "not-a-real-token",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "system-1\n"
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert calls[-1] == {
        "name": "tools.invoke",
        "arguments": {"name": "systems.get", "arguments": {"system_id": "system-1"}},
    }
