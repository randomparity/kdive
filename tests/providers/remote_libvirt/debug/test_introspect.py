"""Unit tests for the remote-libvirt introspection ports (issue #205, ADR-0083).

Drive the worker-side vmcore postmortem (``from_vmcore``) and the in-guest drgn-live port
(``introspect_live``) with injected fakes — a fake-fetched core, a fake drgn ``_Program``, and
a scripted guest-agent double — so the full orchestration + redaction run with no drgn, no
object store, and no libvirt host.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.debug.introspect import (
    RemoteLibvirtLiveIntrospect,
    RemoteLibvirtVmcoreIntrospect,
)
from kdive.providers.remote_libvirt.guest.agent import AgentExecResult
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend


class _FakeProgram:
    def iter_tasks(self):
        return []

    def iter_modules(self):
        return []

    def uts(self):
        return {"release": "6.1.0"}

    def boot_cmdline(self):
        return "ro"

    def cpus_online(self):
        return 1

    def mem_total_pages(self):
        return 1


def _vmcore_introspect(
    *,
    open_program=None,
    run_helper=None,
    fetch=None,
    build_id=lambda b: "BID",
    secret_registry=None,
):
    return RemoteLibvirtVmcoreIntrospect(
        fetch_object=fetch or (lambda ref: b"core" if "core" in ref else b"vmlinux"),
        read_vmcore_build_id=build_id,
        secret_registry=secret_registry or SecretRegistry(),
        open_program=open_program,
        run_helper=run_helper,
    )


def test_from_vmcore_off_gate_is_missing_dependency():
    introspect = _vmcore_introspect()  # no drgn seams
    with pytest.raises(CategorizedError) as exc:
        introspect.from_vmcore(vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert str(exc.value) == "offline drgn introspection runs only under the live_vm gate"


def test_from_vmcore_off_gate_does_not_fetch_when_either_seam_missing():
    # The gate must fire if EITHER seam is missing (open_program OR run_helper). With only one
    # set, no object fetch may occur — the gate short-circuits before any IO.
    fetched: list[str] = []

    def tracking_fetch(ref: str) -> bytes:
        fetched.append(ref)
        return b"core"

    introspect = _vmcore_introspect(run_helper=lambda prog, name: {}, fetch=tracking_fetch)
    with pytest.raises(CategorizedError) as exc:
        introspect.from_vmcore(vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID")
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert fetched == []  # gate short-circuited before fetching


def test_open_without_drgn_opener_is_missing_dependency():
    introspect = _vmcore_introspect(run_helper=lambda prog, name: {})

    with pytest.raises(CategorizedError) as exc:
        introspect._open(Path("core"), Path("vmlinux"))

    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert str(exc.value) == "offline drgn introspection runs only under the live_vm gate"


def test_open_forwards_core_and_vmlinux_paths_to_opener():
    # _open must pass the core and vmlinux paths through unchanged, in order.
    seen: list[tuple[Path, Path]] = []

    def opener(core: Path, vmlinux: Path) -> _FakeProgram:
        seen.append((core, vmlinux))
        return _FakeProgram()

    introspect = _vmcore_introspect(open_program=opener, run_helper=lambda prog, name: {})
    introspect._open(Path("/c"), Path("/v"))
    assert seen == [(Path("/c"), Path("/v"))]


def test_from_vmcore_build_id_mismatch_is_configuration_error():
    introspect = _vmcore_introspect(
        open_program=lambda core, vmlinux: _FakeProgram(),
        run_helper=lambda prog, name: {},
        build_id=lambda b: "OTHER",
    )
    with pytest.raises(CategorizedError) as exc:
        introspect.from_vmcore(vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == (
        "captured vmcore build-id does not match the Run's debuginfo build-id"
    )
    assert exc.value.details == {"vmcore_ref": "core"}


def test_from_vmcore_opens_temp_files_holding_the_fetched_core_and_vmlinux():
    # from_vmcore must hand _open real temp-file paths whose contents are the fetched core and
    # vmlinux bytes (so a dropped/None path argument is caught).
    contents: list[tuple[bytes, bytes]] = []

    def opener(core: Path, vmlinux: Path) -> _FakeProgram:
        contents.append((core.read_bytes(), vmlinux.read_bytes()))
        return _FakeProgram()

    introspect = _vmcore_introspect(
        open_program=opener,
        run_helper=lambda prog, name: {},
        fetch=lambda ref: b"CORE-DATA" if "core" in ref else b"VMLINUX-DATA",
    )
    introspect.from_vmcore(vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID")
    assert contents == [(b"CORE-DATA", b"VMLINUX-DATA")]


def test_from_vmcore_reads_build_id_from_fetched_core_bytes():
    # The observed build-id must be derived from the fetched vmcore bytes (not None / other input).
    seen: list[bytes] = []

    def build_id(data: bytes) -> str:
        seen.append(data)
        return "BID"

    introspect = _vmcore_introspect(
        open_program=lambda core, vmlinux: _FakeProgram(),
        run_helper=lambda prog, name: {},
        fetch=lambda ref: b"core-bytes" if "core" in ref else b"vmlinux",
        build_id=build_id,
    )
    introspect.from_vmcore(vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID")
    assert seen == [b"core-bytes"]


def test_from_vmcore_returns_redacted_report():
    from kdive.providers.shared.debug_common.introspect import (
        helper_modules,
        helper_sysinfo,
        helper_tasks,
    )

    helpers = {"tasks": helper_tasks, "modules": helper_modules, "sysinfo": helper_sysinfo}
    introspect = _vmcore_introspect(
        open_program=lambda core, vmlinux: _FakeProgram(),
        run_helper=lambda prog, name: helpers[name](prog),
    )
    out = introspect.from_vmcore(
        vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID"
    )
    assert out.sysinfo["release"] == "6.1.0"
    assert out.truncated is False


def test_from_vmcore_routes_each_helper_section_into_its_report_field():
    # tasks/modules/sysinfo must each carry the matching helper's output, distinct per field.
    sections = {
        "tasks": {"tasks": [{"pid": 1}]},
        "modules": {"modules": ["mod_a"]},
        "sysinfo": {"release": "6.1.0"},
    }
    introspect = _vmcore_introspect(
        open_program=lambda core, vmlinux: _FakeProgram(),
        run_helper=lambda prog, name: sections[name],
    )
    out = introspect.from_vmcore(
        vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID"
    )
    assert out.tasks == {"tasks": [{"pid": 1}]}
    assert out.modules == {"modules": ["mod_a"]}
    assert out.sysinfo == {"release": "6.1.0"}


def test_from_vmcore_redacts_using_the_provider_secret_registry():
    # The report must be redacted with the introspector's secret_registry (not a fresh/empty one).
    registry = SecretRegistry()
    registry.register("hunter2-secret", scope=None)  # pragma: allowlist secret
    introspect = _vmcore_introspect(
        open_program=lambda core, vmlinux: _FakeProgram(),
        run_helper=lambda prog, name: (
            {"sysinfo": "leak hunter2-secret here"} if name == "sysinfo" else {}
        ),
        secret_registry=registry,
    )
    out = introspect.from_vmcore(
        vmcore_ref="core", debuginfo_ref="vmlinux", expected_build_id="BID"
    )
    assert "hunter2-secret" not in str(out.sysinfo)
    assert "[REDACTED]" in str(out.sysinfo)


_REFS = TlsCertRefs(client_cert_ref="c", client_key_ref="k", ca_cert_ref="a")


class _ScriptedAgent:
    """A qemu_agent_command double implementing the two-phase guest-exec protocol.

    Mirrors test_install.py's scripted agent so the tests exercise the real GuestAgentExec
    (and its worker-side allowlist), not a mock of it. ``handler(argv)`` returns the command's
    AgentExecResult or raises libvirt.libvirtError. Records every argv it ran.
    """

    def __init__(self, handler):
        self._handler = handler
        self._pending = {}
        self._next_pid = 1
        self.argvs: list[list[str]] = []

    def __call__(self, domain, command, timeout, flags):
        payload = json.loads(command)
        if payload["execute"] == "guest-exec":
            args = payload["arguments"]
            argv = [args["path"], *args["arg"]]
            result = self._handler(argv)
            self.argvs.append(argv)
            pid = self._next_pid
            self._next_pid += 1
            self._pending[pid] = result
            return json.dumps({"return": {"pid": pid}})
        if payload["execute"] == "guest-exec-status":
            result = self._pending.pop(payload["arguments"]["pid"])
            return json.dumps(
                {
                    "return": {
                        "exited": True,
                        "exitcode": result.exit_status,
                        "out-data": base64.b64encode(result.stdout).decode(),
                        "err-data": base64.b64encode(result.stderr).decode(),
                    }
                }
            )
        raise AssertionError(payload)


class _FakeDomain:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class _FakeConn:
    def lookupByName(self, name):  # noqa: N802 - libvirt binding name
        return _FakeDomain(name)

    def close(self):
        pass


def _config_remote():
    return RemoteLibvirtConfig(
        uri="qemu+tls://h/system",
        cert_refs=_REFS,
        concurrent_allocation_cap=1,
        gdb_addr="10.0.0.5",
    )


def _live(agent, *, secret_registry=None, conn=None):
    # RecordingBackend + a real GuestAgentExec run; only the libvirt opener is faked.
    return RemoteLibvirtLiveIntrospect(
        secret_registry=secret_registry or SecretRegistry(),
        config_factory=_config_remote,
        open_connection=lambda _uri: conn or _FakeConn(),
        agent_command=agent,
        secret_backend_factory=RecordingBackend,
    )


def test_introspect_live_unknown_helper_is_configuration_error():
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, b"{}", b""))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="kdive-sys", helper="evil")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "unknown live introspection helper: evil"
    assert agent.argvs == []  # rejected before any agent round-trip


def test_introspect_live_blank_handle_is_configuration_error():
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, b"{}", b""))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="   ", helper="sysinfo")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert agent.argvs == []


def test_introspect_live_runs_allowlisted_helper_through_real_guest_agent():
    section = {
        "release": "6.1.0",
        "version": "v",
        "machine": "x86_64",
        "nodename": "n",
        "boot_cmdline": "ro",
        "cpus_online": 1,
        "mem_total_pages": 1,
    }
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, json.dumps(section).encode(), b""))
    live = _live(agent)
    out = live.introspect_live(transport_handle="kdive-sys", helper="sysinfo")
    # the single allowlisted program
    assert agent.argvs == [["/usr/local/sbin/kdive-drgn", "sysinfo"]]
    assert out.sysinfo["release"] == "6.1.0"


@pytest.mark.parametrize(
    ("helper", "field"),
    [("tasks", "tasks"), ("modules", "modules"), ("sysinfo", "sysinfo")],
)
def test_introspect_live_routes_section_into_the_matching_report_field(helper, field):
    # The decoded section's payload must land in the field that matches the requested helper, and
    # must NOT appear in the other two fields (which carry only their empty defaults).
    marker = f"{helper}-payload"
    section = {"value": marker}
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, json.dumps(section).encode(), b""))
    live = _live(agent)
    out = live.introspect_live(transport_handle="kdive-sys", helper=helper)

    assert getattr(out, field).get("value") == marker
    for other in ("tasks", "modules", "sysinfo"):
        if other != field:
            assert marker not in str(getattr(out, other))


def test_introspect_live_threads_handle_into_domain_lookup():
    # The stripped domain name must be threaded into conn.lookupByName, and the looked-up domain
    # (not None) must be handed to the agent round-trip.
    looked_up: list[str] = []
    agent_domains: list[object] = []

    class _RecordingConn:
        def lookupByName(self, name):  # noqa: N802 - libvirt binding name
            looked_up.append(name)
            return _FakeDomain(name)

        def close(self):
            pass

    class _DomainCapturingAgent(_ScriptedAgent):
        def __call__(self, domain, command, timeout, flags):
            if json.loads(command)["execute"] == "guest-exec":
                agent_domains.append(domain)
            return super().__call__(domain, command, timeout, flags)

    agent = _DomainCapturingAgent(lambda argv: AgentExecResult(0, b"{}", b""))
    live = _live(agent, conn=_RecordingConn())
    live.introspect_live(transport_handle="  kdive-sys  ", helper="sysinfo")

    assert looked_up == ["kdive-sys"]
    assert agent_domains and agent_domains[0] is not None
    assert agent_domains[0].name() == "kdive-sys"


def test_introspect_live_tasks_section_is_byte_capped():
    # A tasks reply with rows must pass through the byte-cap (a real integer cap), returning the
    # rows under the 1 MiB default. A missing/None cap would fault on the size comparison.
    section = {"tasks": [{"pid": i} for i in range(3)]}
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, json.dumps(section).encode(), b""))
    live = _live(agent)
    out = live.introspect_live(transport_handle="kdive-sys", helper="tasks")

    assert out.tasks["tasks"] == [{"pid": 0}, {"pid": 1}, {"pid": 2}]
    assert out.truncated is False


def test_introspect_live_redacts_using_the_provider_secret_registry():
    registry = SecretRegistry()
    registry.register("topsecret-value", scope=None)  # pragma: allowlist secret
    section = {"leak": "see topsecret-value here"}
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, json.dumps(section).encode(), b""))
    live = _live(agent, secret_registry=registry)
    out = live.introspect_live(transport_handle="kdive-sys", helper="modules")

    assert "topsecret-value" not in str(out.modules)
    assert "[REDACTED]" in str(out.modules)


def test_introspect_live_nonzero_exit_is_debug_attach_failure():
    agent = _ScriptedAgent(lambda argv: AgentExecResult(1, b"", b"boom"))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="kdive-sys", helper="tasks")
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert str(exc.value) == (
        "in-guest drgn helper exited non-zero (could not attach to the live kernel)"
    )
    assert exc.value.details == {"domain": "kdive-sys", "exit_status": 1}


def test_introspect_live_undecodable_output_is_infrastructure_failure():
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, b"not json", b""))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="kdive-sys", helper="modules")
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(exc.value) == "in-guest drgn helper returned undecodable JSON"


def test_introspect_live_non_object_json_is_infrastructure_failure():
    # A valid-JSON-but-not-an-object reply (e.g. a list) is a malformed helper output.
    agent = _ScriptedAgent(lambda argv: AgentExecResult(0, b"[1, 2, 3]", b""))
    live = _live(agent)
    with pytest.raises(CategorizedError) as exc:
        live.introspect_live(transport_handle="kdive-sys", helper="tasks")
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(exc.value) == "in-guest drgn helper output was not a JSON object"
