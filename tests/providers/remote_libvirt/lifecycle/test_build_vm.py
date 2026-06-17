"""Unit tests for the ephemeral remote-libvirt build VM lifecycle (ADR-0100).

Drives EphemeralBuildVm.session over the same fake provision-connection the provisioning
tests use (no libvirt host). Asserts the build-domain XML shape, the provision→yield→teardown
order, teardown-on-exception, and overlay creation over the base image.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import GitSourceRef
from kdive.providers.remote_libvirt.guest.build_transport import GuestExecBuildTransport
from kdive.providers.remote_libvirt.lifecycle.build_vm import (
    BuildVmTiming,
    EphemeralBuildVm,
    build_domain_name,
    build_overlay_volume_name,
    render_build_domain_xml,
)
from kdive.providers.remote_libvirt.lifecycle.xml import recorded_gdb_port
from kdive.providers.remote_libvirt.transport import remote_libvirt_connections
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import RecordingBackend, libvirt_error
from tests.providers.remote_libvirt.lifecycle.test_provisioning import (
    _BASE_VOLUME,
    FakePool,
    FakeProvisionConn,
    FakeVolume,
    _config,
    _ticker,
)

RUN_ID = UUID("00000000-0000-0000-0000-00000000ca11")
DOMAIN_NAME = build_domain_name(RUN_ID)
OVERLAY = build_overlay_volume_name(RUN_ID)


def _agent_ok(domain: Any, command: str, timeout: int, flags: int) -> str:
    """A guest-agent fake good enough for a no-op transport binding (no exec in these tests)."""
    msg = json.loads(command)
    if msg["execute"] == "guest-exec":
        return json.dumps({"return": {"pid": 1}})
    return json.dumps({"return": {"exited": True, "exitcode": 0}})


def _agent_route_after(polls: int) -> tuple[Any, dict[str, int]]:
    """A guest-agent fake whose route probe reports rc!=0 for the first `polls` checks then rc 0.

    The probe is the only guest-exec issued in these tests, so each guest-exec/guest-exec-status
    pair is one probe. Returns rc 1 (no route) until `polls` checks have happened, then rc 0. The
    returned `state` dict exposes `checks` so a test can assert the gate actually polled.
    """
    state = {"checks": 0}

    def _agent(domain: Any, command: str, timeout: int, flags: int) -> str:
        msg = json.loads(command)
        if msg["execute"] == "guest-exec":
            return json.dumps({"return": {"pid": 1}})
        state["checks"] += 1
        rc = 0 if state["checks"] > polls else 1
        return json.dumps({"return": {"exited": True, "exitcode": rc}})

    return _agent, state


_ROUTE_MARKER = "/proc/net/route"
_LS_REMOTE_MARKER = "ls-remote"


class _EgressAgent:
    """Guest-agent fake that distinguishes the route probe from the egress (`ls-remote`) probe.

    `guest-exec` carries the argv (so the command kind is known at spawn); `guest-exec-status`
    carries only the pid. This fake remembers the kind per spawned pid and returns the configured
    rc on status, so a test can fail just the egress probe while the route probe succeeds. It
    records each issued `ls-remote` command string so a test can assert the probe targets `HEAD`.
    """

    def __init__(
        self,
        *,
        route_rc: int = 0,
        egress_rc: int = 0,
        egress_stderr: str = "",
        egress_raises: BaseException | None = None,
    ) -> None:
        self.route_rc = route_rc
        self.egress_rc = egress_rc
        self.egress_stderr = egress_stderr
        self.egress_raises = egress_raises
        self.ls_remote_commands: list[str] = []
        self._kind_by_pid: dict[int, str] = {}
        self._next_pid = 1

    def __call__(self, domain: Any, command: str, timeout: int, flags: int) -> str:
        msg = json.loads(command)
        if msg["execute"] == "guest-exec":
            argv = " ".join([msg["arguments"]["path"], *msg["arguments"].get("arg", [])])
            kind = "egress" if _LS_REMOTE_MARKER in argv else "route"
            if kind == "egress":
                if self.egress_raises is not None:
                    raise self.egress_raises
                self.ls_remote_commands.append(argv)
            pid = self._next_pid
            self._next_pid += 1
            self._kind_by_pid[pid] = kind
            return json.dumps({"return": {"pid": pid}})
        pid = msg["arguments"]["pid"]
        kind = self._kind_by_pid.get(pid, "route")
        if kind == "egress":
            stderr = base64.b64encode(self.egress_stderr.encode()).decode()
            return json.dumps(
                {"return": {"exited": True, "exitcode": self.egress_rc, "err-data": stderr}}
            )
        return json.dumps({"return": {"exited": True, "exitcode": self.route_rc}})


def _conn_with_base() -> FakeProvisionConn:
    pool = FakePool({_BASE_VOLUME: FakeVolume(_BASE_VOLUME)})
    return FakeProvisionConn({"default": pool})


def _build_vm(conn: FakeProvisionConn, tmp_path: Any) -> EphemeralBuildVm:
    def _open(_uri: str) -> Any:
        return conn

    return EphemeralBuildVm(
        secret_registry=SecretRegistry(),
        connections=remote_libvirt_connections(
            secret_registry=SecretRegistry(),
            config_factory=_config,
            open_connection=_open,
            secret_backend_factory=RecordingBackend,
            pki_base_dir=tmp_path,
        ),
        agent_command=_agent_ok,
        timing=BuildVmTiming(sleep=lambda _s: None, monotonic=_ticker()),
    )


def _build_vm_with_agent(
    conn: FakeProvisionConn, tmp_path: Any, agent: Any, **timing: Any
) -> EphemeralBuildVm:
    def _open(_uri: str) -> Any:
        return conn

    return EphemeralBuildVm(
        secret_registry=SecretRegistry(),
        connections=remote_libvirt_connections(
            secret_registry=SecretRegistry(),
            config_factory=_config,
            open_connection=_open,
            secret_backend_factory=RecordingBackend,
            pki_base_dir=tmp_path,
        ),
        agent_command=agent,
        timing=BuildVmTiming(sleep=lambda _s: None, monotonic=_ticker(), **timing),
    )


# --- build-domain XML ---------------------------------------------------------------


def test_render_build_domain_xml_has_agent_channel_and_no_gdbstub() -> None:
    xml = render_build_domain_xml(
        RUN_ID, pool="default", volume=OVERLAY, network="default", machine="pc"
    )
    assert f"<name>{DOMAIN_NAME}</name>" in xml
    # The agent channel must be present (readiness depends on it).
    assert "org.qemu.guest_agent.0" in xml
    # The build domain must record NO gdbstub port (inert for used_gdb_ports enumeration).
    assert recorded_gdb_port(xml) is None


# --- session lifecycle --------------------------------------------------------------


def test_session_provisions_yields_transport_and_tears_down(tmp_path: Any) -> None:
    conn = _conn_with_base()
    vm = _build_vm(conn, tmp_path)

    with vm.session(_BASE_VOLUME, run_id=RUN_ID) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
        # The domain is defined + started while the session is open.
        assert DOMAIN_NAME in conn.domains
        assert conn.domains[DOMAIN_NAME].active

    # After the session exits, the domain is destroyed + undefined and the overlay deleted.
    assert DOMAIN_NAME not in conn.domains
    assert OVERLAY in conn.pools["default"].deleted


def test_session_creates_overlay_over_base_image(tmp_path: Any) -> None:
    conn = _conn_with_base()
    vm = _build_vm(conn, tmp_path)

    with vm.session(_BASE_VOLUME, run_id=RUN_ID):
        [volume_xml] = conn.pools["default"].created_xml
        assert OVERLAY in volume_xml
        assert f"/pool/{_BASE_VOLUME}" in volume_xml


def test_session_tears_down_even_when_body_raises(tmp_path: Any) -> None:
    conn = _conn_with_base()
    vm = _build_vm(conn, tmp_path)

    with pytest.raises(RuntimeError, match="boom"), vm.session(_BASE_VOLUME, run_id=RUN_ID):
        raise RuntimeError("boom")

    # Teardown still ran: domain gone, overlay reclaimed.
    assert DOMAIN_NAME not in conn.domains
    assert OVERLAY in conn.pools["default"].deleted


def test_session_teardown_failure_preserves_body_error_and_logs_context(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    conn = _conn_with_base()
    vm = _build_vm(conn, tmp_path)

    with (
        caplog.at_level("WARNING"),
        pytest.raises(RuntimeError, match="boom"),
        vm.session(_BASE_VOLUME, run_id=RUN_ID),
    ):
        conn.domains[DOMAIN_NAME].destroy_error = libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)
        raise RuntimeError("boom")

    assert any(
        record.exc_info is not None and "domain teardown failed" in record.message
        for record in caplog.records
    )


# --- network-readiness gate (ADR-0144) ----------------------------------------------


def test_session_yields_only_after_route_appears(tmp_path: Any) -> None:
    conn = _conn_with_base()
    agent, state = _agent_route_after(2)
    vm = _build_vm_with_agent(conn, tmp_path, agent)

    with vm.session(_BASE_VOLUME, run_id=RUN_ID) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
        assert conn.domains[DOMAIN_NAME].active
        # The gate polled until the route appeared (rc1, rc1, rc0) — not a vacuous immediate yield.
        assert state["checks"] == 3
    assert DOMAIN_NAME not in conn.domains


def test_session_network_never_ready_raises_and_tears_down(tmp_path: Any) -> None:
    conn = _conn_with_base()
    agent, _state = _agent_route_after(10_000)
    # Route never appears; small network timeout so the fake clock reaches the deadline quickly.
    vm = _build_vm_with_agent(conn, tmp_path, agent, network_timeout_s=5.0, network_poll_s=1.0)

    with pytest.raises(CategorizedError) as exc, vm.session(_BASE_VOLUME, run_id=RUN_ID):
        pass
    assert exc.value.category == ErrorCategory.PROVISIONING_FAILURE
    # Teardown still ran.
    assert DOMAIN_NAME not in conn.domains
    assert OVERLAY in conn.pools["default"].deleted


# --- egress preflight to the configured source (ADR-0155) ----------------------------

_REMOTE = "https://git.example/linux.git"
_SOURCE = GitSourceRef(remote=_REMOTE, ref="v6.9")
_SHA = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret - fake commit sha


def test_session_fails_when_source_unreachable_naming_source(tmp_path: Any) -> None:
    conn = _conn_with_base()
    agent = _EgressAgent(route_rc=0, egress_rc=128, egress_stderr="fatal: unable to access")
    vm = _build_vm_with_agent(conn, tmp_path, agent)

    with (
        pytest.raises(CategorizedError) as exc,
        vm.session(_BASE_VOLUME, run_id=RUN_ID, source=_SOURCE),
    ):
        pass

    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    # The unreachable source is named (host present), and the git stderr is surfaced.
    assert "git.example/linux.git" in str(exc.value.details["remote"])
    assert "unable to access" in str(exc.value.details["stderr"])
    # The egress probe ran and was the last guest-exec (no clone followed).
    assert len(agent.ls_remote_commands) == 1
    # Teardown still ran: VM torn down, overlay reclaimed.
    assert DOMAIN_NAME not in conn.domains
    assert OVERLAY in conn.pools["default"].deleted


def test_session_yields_when_egress_works(tmp_path: Any) -> None:
    conn = _conn_with_base()
    agent = _EgressAgent(route_rc=0, egress_rc=0)
    vm = _build_vm_with_agent(conn, tmp_path, agent)

    with vm.session(_BASE_VOLUME, run_id=RUN_ID, source=_SOURCE) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
        assert len(agent.ls_remote_commands) == 1
    assert DOMAIN_NAME not in conn.domains


def test_session_skips_preflight_when_no_source(tmp_path: Any) -> None:
    conn = _conn_with_base()
    agent = _EgressAgent(route_rc=0, egress_rc=128)
    vm = _build_vm_with_agent(conn, tmp_path, agent)

    # No source supplied: route-only behavior, no ls-remote probe issued.
    with vm.session(_BASE_VOLUME, run_id=RUN_ID) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
    assert agent.ls_remote_commands == []
    assert DOMAIN_NAME not in conn.domains


def test_session_redacts_credential_in_unreachable_source(tmp_path: Any) -> None:
    conn = _conn_with_base()
    agent = _EgressAgent(route_rc=0, egress_rc=128)
    vm = _build_vm_with_agent(conn, tmp_path, agent)
    credentialed = GitSourceRef(remote="https://alice:hunter2@git.example/linux.git", ref="v6.9")

    with (
        pytest.raises(CategorizedError) as exc,
        vm.session(_BASE_VOLUME, run_id=RUN_ID, source=credentialed),
    ):
        pass

    rendered = f"{exc.value}{exc.value.details}"
    assert "git.example/linux.git" in str(exc.value.details["remote"])
    assert "hunter2" not in rendered
    assert "alice:hunter2" not in rendered


def test_session_propagates_agent_drop_during_preflight(tmp_path: Any) -> None:
    conn = _conn_with_base()
    agent = _EgressAgent(route_rc=0, egress_raises=libvirt_error(libvirt.VIR_ERR_OPERATION_FAILED))
    vm = _build_vm_with_agent(conn, tmp_path, agent)

    with (
        pytest.raises(CategorizedError) as exc,
        vm.session(_BASE_VOLUME, run_id=RUN_ID, source=_SOURCE),
    ):
        pass

    # An agent drop is a transport_failure that propagates unchanged (not a not-ready signal).
    assert exc.value.category == ErrorCategory.TRANSPORT_FAILURE
    assert DOMAIN_NAME not in conn.domains


def test_session_egress_probe_targets_head_not_pinned_sha(tmp_path: Any) -> None:
    conn = _conn_with_base()
    agent = _EgressAgent(route_rc=0, egress_rc=0)
    vm = _build_vm_with_agent(conn, tmp_path, agent)
    sha_pinned = GitSourceRef(remote=_REMOTE, ref=_SHA)

    # A reachable host with a SHA-pinned ref must NOT be falsely failed: the probe targets HEAD,
    # not the bare SHA (which is not an advertised ref).
    with vm.session(_BASE_VOLUME, run_id=RUN_ID, source=sha_pinned) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
    [probe] = agent.ls_remote_commands
    assert "HEAD" in probe
    assert _SHA not in probe


def test_session_egress_probe_guards_leading_dash_remote(tmp_path: Any) -> None:
    conn = _conn_with_base()
    agent = _EgressAgent(route_rc=0, egress_rc=0)
    vm = _build_vm_with_agent(conn, tmp_path, agent)
    dashed = GitSourceRef(remote="--upload-pack=evil", ref="v6.9")

    # A remote starting with '-' must reach git as a positional operand (after '--'), not an
    # option — the preflight runs before the clone's own leading-dash guard.
    with vm.session(_BASE_VOLUME, run_id=RUN_ID, source=dashed) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
    [probe] = agent.ls_remote_commands
    assert "-- --upload-pack=evil HEAD" in probe
