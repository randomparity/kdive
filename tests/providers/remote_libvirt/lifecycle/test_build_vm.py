"""Unit tests for the ephemeral remote-libvirt build VM lifecycle (ADR-0100).

Drives EphemeralBuildVm.session over the same fake provision-connection the provisioning
tests use (no libvirt host). Asserts the build-domain XML shape, the provision→yield→teardown
order, teardown-on-exception, and overlay creation over the base image.
"""

from __future__ import annotations

import base64
import json
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import GitSourceRef
from kdive.providers.remote_libvirt.connection.transport import remote_libvirt_connections
from kdive.providers.remote_libvirt.guest.build_transport import GuestExecBuildTransport
from kdive.providers.remote_libvirt.lifecycle.build_vm import (
    BuildVmTiming,
    EphemeralBuildVm,
    build_domain_name,
    build_overlay_volume_name,
    render_build_domain_xml,
)
from kdive.providers.remote_libvirt.lifecycle.xml import recorded_gdb_port
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
    if msg["execute"] == "guest-ping":
        return json.dumps({"return": {}})
    if msg["execute"] == "guest-exec":
        return json.dumps({"return": {"pid": 1}})
    return json.dumps({"return": {"exited": True, "exitcode": 0}})


def _agent_unresponsive(domain: Any, command: str, timeout: int, flags: int) -> str:
    """A guest-agent fake whose guest-ping always reports code 86 (agent never answers)."""
    raise libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE)


def _agent_route_after(polls: int) -> tuple[Any, dict[str, int]]:
    """A guest-agent fake whose route probe reports rc!=0 for the first `polls` checks then rc 0.

    The probe is the only guest-exec issued in these tests, so each guest-exec/guest-exec-status
    pair is one probe. Returns rc 1 (no route) until `polls` checks have happened, then rc 0. The
    returned `state` dict exposes `checks` so a test can assert the gate actually polled.
    """
    state = {"checks": 0}

    def _agent(domain: Any, command: str, timeout: int, flags: int) -> str:
        msg = json.loads(command)
        if msg["execute"] == "guest-ping":
            return json.dumps({"return": {}})
        if msg["execute"] == "guest-exec":
            return json.dumps({"return": {"pid": 1}})
        state["checks"] += 1
        rc = 0 if state["checks"] > polls else 1
        return json.dumps({"return": {"exited": True, "exitcode": rc}})

    return _agent, state


def _agent_route_drops_then_ready(drops: int) -> tuple[Any, dict[str, int]]:
    """Route probe whose agent raises code 86 for the first `drops` checks, then the route appears.

    Models NetworkManager briefly dropping the build VM's virtio-serial agent channel while it
    brings the interface up (#584): a ``VIR_ERR_AGENT_UNRESPONSIVE`` during the network gate is
    transient and must be tolerated (keep polling), unlike a drop during the build itself.
    """
    state = {"checks": 0}

    def _agent(domain: Any, command: str, timeout: int, flags: int) -> str:
        msg = json.loads(command)
        if msg["execute"] == "guest-ping":
            return json.dumps({"return": {}})
        if msg["execute"] == "guest-exec":
            state["checks"] += 1
            if state["checks"] <= drops:
                raise libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE)
            return json.dumps({"return": {"pid": 1}})
        return json.dumps({"return": {"exited": True, "exitcode": 0}})

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
        if msg["execute"] == "guest-ping":
            return json.dumps({"return": {}})
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


# --- default connections wiring -----------------------------------------------------


def test_init_builds_default_connections_with_injected_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When no connections object is injected, the default is built from the secret registry,
    # config factory, and the provision opener — each must be threaded through, not dropped.
    import kdive.providers.remote_libvirt.lifecycle.build_vm as build_vm_module

    captured: dict[str, Any] = {}
    sentinel = object()

    def _fake_connections(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(build_vm_module, "remote_libvirt_connections", _fake_connections)

    registry = SecretRegistry()

    def _factory() -> Any:
        return _config()

    vm = EphemeralBuildVm(secret_registry=registry, config_factory=_factory)

    assert vm._connections is sentinel
    assert captured["secret_registry"] is registry
    assert captured["config_factory"] is _factory
    assert captured["open_connection"] is build_vm_module.open_libvirt_provision


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


def test_render_build_domain_xml_pins_host_model_cpu_for_el9_baseline() -> None:
    """The build VM must carry a host-model CPU so EL9 build images meet x86-64-v2 (#975).

    An EL9-based build image hits the identical missing-``<cpu>`` init panic as the System
    domain: QEMU defaults to x86-64-v1 and glibc aborts PID 1 before the guest-agent answers,
    so ``wait_for_agent`` times out. host-model gives a portable >= v2 baseline (ADR-0297),
    placed after ``<vcpu>`` and before ``<os>``.
    """
    from defusedxml.ElementTree import fromstring as _fromstring

    xml = render_build_domain_xml(
        RUN_ID, pool="default", volume=OVERLAY, network="default", machine="pc"
    )
    root = _fromstring(xml)
    cpu = root.find("./cpu")
    assert cpu is not None, "build domain XML must carry a <cpu> element"
    assert cpu.get("mode") == "host-model"
    children = [child.tag for child in root]
    assert children.index("cpu") == children.index("vcpu") + 1
    assert children.index("cpu") < children.index("os")


def test_render_build_domain_xml_full_structure() -> None:
    # Distinct, non-default values for every parameter so a wrong attribute name or a swapped
    # value cannot pass by coinciding with a default.
    from defusedxml.ElementTree import fromstring as _fromstring

    xml = render_build_domain_xml(
        RUN_ID,
        pool="build-pool",
        volume="overlay.qcow2",
        network="isolated-net",
        machine="q35",
        vcpus=6,
        memory_mib=4096,
        arch="aarch64",
    )
    root = _fromstring(xml)

    assert root.tag == "domain"
    assert root.get("type") == "kvm"
    assert root.findtext("name") == DOMAIN_NAME
    assert root.findtext("uuid") == str(RUN_ID)

    memory = root.find("memory")
    assert memory is not None
    assert memory.get("unit") == "MiB"
    assert memory.text == "4096"
    assert root.findtext("vcpu") == "6"

    os_type = root.find("os/type")
    assert os_type is not None
    assert os_type.get("arch") == "aarch64"
    assert os_type.get("machine") == "q35"
    assert os_type.text == "hvm"
    boot = root.find("os/boot")
    assert boot is not None
    assert boot.get("dev") == "hd"

    assert root.find("features/acpi") is not None

    disk = root.find("devices/disk")
    assert disk is not None
    assert disk.get("type") == "volume"
    assert disk.get("device") == "disk"
    driver = disk.find("driver")
    assert driver is not None
    assert driver.get("name") == "qemu"
    assert driver.get("type") == "qcow2"
    source = disk.find("source")
    assert source is not None
    assert source.get("pool") == "build-pool"
    assert source.get("volume") == "overlay.qcow2"
    target = disk.find("target")
    assert target is not None
    assert target.get("dev") == "vda"
    assert target.get("bus") == "virtio"

    interface = root.find("devices/interface")
    assert interface is not None
    assert interface.get("type") == "network"
    iface_source = interface.find("source")
    assert iface_source is not None
    assert iface_source.get("network") == "isolated-net"
    iface_model = interface.find("model")
    assert iface_model is not None
    assert iface_model.get("type") == "virtio"

    channel = root.find("devices/channel")
    assert channel is not None
    assert channel.get("type") == "unix"
    chan_target = channel.find("target")
    assert chan_target is not None
    assert chan_target.get("type") == "virtio"
    assert chan_target.get("name") == "org.qemu.guest_agent.0"

    # The kdive metadata build marker carries the run id (the reaper key).
    from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS

    build_marker = root.find(f"metadata/{{{KDIVE_METADATA_NS}}}build")
    assert build_marker is not None
    assert build_marker.text == str(RUN_ID)


def test_render_build_domain_xml_uses_default_sizing() -> None:
    from defusedxml.ElementTree import fromstring as _fromstring

    xml = render_build_domain_xml(
        RUN_ID, pool="default", volume=OVERLAY, network="default", machine="pc"
    )
    root = _fromstring(xml)
    # Defaults: 4 vCPUs, 8192 MiB, x86_64 (a builder wants headroom; ADR-0100).
    assert root.findtext("vcpu") == "4"
    assert root.findtext("memory") == "8192"
    os_type = root.find("os/type")
    assert os_type is not None
    assert os_type.get("arch") == "x86_64"


# --- session lifecycle --------------------------------------------------------------


def test_session_provisions_yields_transport_and_tears_down(tmp_path: Any) -> None:
    conn = _conn_with_base()
    vm = _build_vm(conn, tmp_path)

    with vm.session(_BASE_VOLUME, run_id=RUN_ID) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
        # The domain is defined + started while the session is open.
        assert DOMAIN_NAME in conn.domains
        assert conn.domains[DOMAIN_NAME].active
        # The defined domain XML references this run's overlay volume (run-id-derived name).
        assert f'volume="{OVERLAY}"' in conn.defined_xml[0]

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


# --- agent-responsiveness gate (ADR-0168) -------------------------------------------


def test_session_fails_non_retryable_when_agent_never_responsive(tmp_path: Any) -> None:
    conn = _conn_with_base()
    # The XML channel reports connected (wait_for_agent passes), but guest-ping never answers.
    vm = _build_vm_with_agent(
        conn,
        tmp_path,
        _agent_unresponsive,
        agent_responsive_timeout_s=3.0,
        agent_responsive_poll_s=1.0,
    )

    with pytest.raises(CategorizedError) as exc, vm.session(_BASE_VOLUME, run_id=RUN_ID):
        pass

    # Non-retryable configuration_error carrying the agent_readiness marker, not transport_failure.
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["agent_readiness"] == "unresponsive"
    # The gate runs before the transport is used: no network probe, and teardown still ran.
    assert DOMAIN_NAME not in conn.domains
    assert OVERLAY in conn.pools["default"].deleted


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


def test_session_network_timeout_surfaces_last_probe_output(tmp_path: Any) -> None:
    # On the network deadline the *last* probe's stderr/stdout is surfaced under the
    # probe_stderr/probe_stdout keys (stdout tail-trimmed to 200 chars), the failing domain is
    # named, and a registered secret in the probe stderr is scrubbed (ADR-0144).
    from kdive.security.secrets.redaction import REDACTION

    secret = "route-probe-secret-1234"  # pragma: allowlist secret - test fixture value
    registry = SecretRegistry()
    registry.register(secret, scope=None)

    # Every route poll emits a *distinct* stdout marker so an off-by-one "last probe" index is
    # caught; the final poll's marker (tracked in state["last_marker"]) must be the surfaced one.
    state: dict[str, Any] = {"n": 0, "last_marker": ""}

    def _agent(domain: Any, command: str, timeout: int, flags: int) -> str:
        msg = json.loads(command)
        if msg["execute"] == "guest-ping":
            return json.dumps({"return": {}})
        if msg["execute"] == "guest-exec":
            state["n"] += 1
            return json.dumps({"return": {"pid": state["n"]}})
        marker = f"poll-{state['n']:03d}"
        state["last_marker"] = marker
        payload = ("X" * 250) + marker  # > 200 chars so the tail-trim is observable
        out = base64.b64encode(payload.encode()).decode()
        err = base64.b64encode(f"no route to host {secret}".encode()).decode()
        # rc 1 forever: the route never appears, driving the gate to its deadline.
        return json.dumps(
            {"return": {"exited": True, "exitcode": 1, "out-data": out, "err-data": err}}
        )

    conn = _conn_with_base()

    def _open(_uri: str) -> Any:
        return conn

    vm = EphemeralBuildVm(
        secret_registry=registry,
        connections=remote_libvirt_connections(
            secret_registry=registry,
            config_factory=_config,
            open_connection=_open,
            secret_backend_factory=RecordingBackend,
            pki_base_dir=tmp_path,
        ),
        agent_command=_agent,
        timing=BuildVmTiming(
            sleep=lambda _s: None,
            monotonic=_ticker(),
            network_timeout_s=5.0,
            network_poll_s=1.0,
        ),
    )

    with pytest.raises(CategorizedError) as exc, vm.session(_BASE_VOLUME, run_id=RUN_ID):
        pass

    details = exc.value.details
    assert details["domain"] == DOMAIN_NAME
    # The registered secret in the probe stderr is scrubbed.
    assert secret not in str(details["probe_stderr"])
    assert REDACTION in str(details["probe_stderr"])
    # The surfaced stdout is the LAST poll's payload, tail-trimmed to 200 chars.
    probe_stdout = str(details["probe_stdout"])
    assert len(probe_stdout) == 200
    assert state["n"] >= 3  # several polls happened, so an off-by-one index would differ
    assert probe_stdout.endswith(state["last_marker"])


def test_session_fails_on_deterministic_agent_config_error_during_network_gate(
    tmp_path: Any,
) -> None:
    """A non-unresponsive deterministic agent config error during the route gate is fatal (#584).

    Only VIR_ERR_AGENT_UNRESPONSIVE is the transient NetworkManager churn; a denied/unsupported
    agent command must propagate, not be retried as "not ready".
    """

    def _agent(domain: Any, command: str, timeout: int, flags: int) -> str:
        msg = json.loads(command)
        if msg["execute"] == "guest-ping":
            return json.dumps({"return": {}})
        # The route probe's guest-exec hits a deterministic, non-unresponsive config error.
        raise libvirt_error(libvirt.VIR_ERR_OPERATION_DENIED)

    conn = _conn_with_base()
    vm = _build_vm_with_agent(conn, tmp_path, _agent, network_timeout_s=30.0, network_poll_s=1.0)

    with pytest.raises(CategorizedError) as exc, vm.session(_BASE_VOLUME, run_id=RUN_ID):
        pass

    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    # Teardown still ran.
    assert DOMAIN_NAME not in conn.domains


def test_session_tolerates_transient_agent_drop_during_network_gate(tmp_path: Any) -> None:
    """A code-86 agent drop while the network comes up is transient: keep polling, yield (#584)."""
    conn = _conn_with_base()
    agent, state = _agent_route_drops_then_ready(2)
    vm = _build_vm_with_agent(conn, tmp_path, agent, network_timeout_s=30.0, network_poll_s=1.0)

    with vm.session(_BASE_VOLUME, run_id=RUN_ID) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
        assert conn.domains[DOMAIN_NAME].active
        # Two drops were tolerated, then the route appeared on the third check.
        assert state["checks"] == 3
    assert DOMAIN_NAME not in conn.domains


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
    assert "build VM cannot reach build source" in str(exc.value)
    assert "git.example/linux.git" in str(exc.value)
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
        # The preflight runs in / with the exact git ls-remote argv (HEAD, --exit-code, the
        # end-of-options `--`), so a swapped flag or working directory is caught.
        assert (
            "cd / && exec git ls-remote --quiet --exit-code "
            f"-- {_REMOTE} HEAD" in agent.ls_remote_commands[0]
        )
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


def test_session_skips_network_wait_when_disabled(tmp_path: Any) -> None:
    conn = _conn_with_base()
    # Route never appears, but wait_network=False must not poll it or raise — the agent-reachability
    # probe (ADR-0167) needs only the guest agent, which _agent_route_after already binds.
    agent, state = _agent_route_after(10_000)
    vm = _build_vm_with_agent(conn, tmp_path, agent, network_timeout_s=5.0, network_poll_s=1.0)

    with vm.session(_BASE_VOLUME, run_id=RUN_ID, wait_network=False) as transport:
        assert isinstance(transport, GuestExecBuildTransport)
    assert state["checks"] == 0  # the network gate never ran
    assert DOMAIN_NAME not in conn.domains  # teardown still happened


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


def test_session_redacts_registered_secret_in_egress_stderr(tmp_path: Any) -> None:
    # The egress stderr detail is scrubbed against the build VM's secret registry, so a
    # registered secret leaked into git's stderr never reaches the error detail.
    from kdive.security.secrets.redaction import REDACTION

    secret = "topsecretvalue9876"  # pragma: allowlist secret - test fixture value
    registry = SecretRegistry()
    registry.register(secret, scope=None)

    conn = _conn_with_base()
    agent = _EgressAgent(
        route_rc=0, egress_rc=128, egress_stderr=f"fatal: auth failed using {secret}"
    )

    def _open(_uri: str) -> Any:
        return conn

    vm = EphemeralBuildVm(
        secret_registry=registry,
        connections=remote_libvirt_connections(
            secret_registry=registry,
            config_factory=_config,
            open_connection=_open,
            secret_backend_factory=RecordingBackend,
            pki_base_dir=tmp_path,
        ),
        agent_command=agent,
        timing=BuildVmTiming(sleep=lambda _s: None, monotonic=_ticker()),
    )

    with (
        pytest.raises(CategorizedError) as exc,
        vm.session(_BASE_VOLUME, run_id=RUN_ID, source=_SOURCE),
    ):
        pass

    stderr_detail = str(exc.value.details["stderr"])
    assert secret not in stderr_detail
    assert REDACTION in stderr_detail


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


def test_ephemeral_session_resolves_config_by_build_host_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0187, #395: the ephemeral build session must provision on the *build host's* config,
    # resolved by the host's [[remote_libvirt]] instance name — not a lone singleton.
    from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
    from kdive.providers.remote_libvirt.lifecycle import build_vm

    captured: dict[str, object] = {}

    def fake_for_resource(name: str) -> RemoteLibvirtConfig:
        captured["name"] = name
        return RemoteLibvirtConfig(
            uri=f"qemu+tls://{name}.example/system",
            cert_refs=TlsCertRefs("c", "k", "ca"),  # pragma: allowlist secret
            concurrent_allocation_cap=1,
        )

    class _RecordingVm:
        def __init__(self, *, secret_registry, config_factory) -> None:  # noqa: ANN001
            captured["config"] = config_factory()

        @contextmanager
        def session(self, *args, **kwargs):  # noqa: ANN002, ANN003
            yield object()

    monkeypatch.setattr(build_vm, "remote_config_for_resource", fake_for_resource)
    monkeypatch.setattr(build_vm, "EphemeralBuildVm", _RecordingVm)

    with build_vm.ephemeral_build_session(
        _BASE_VOLUME, SecretRegistry(), run_id=RUN_ID, resource_name="host-b"
    ):
        pass

    assert captured["name"] == "host-b"
    config = captured["config"]
    assert isinstance(config, RemoteLibvirtConfig)
    assert config.uri == "qemu+tls://host-b.example/system"
