"""Boundary fault tests for the real remote ordering experiment (#2816)."""

from __future__ import annotations

import socket
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from kdive.providers.ports.traffic import RemoteCaptureConfiguration
from scripts.coverage_campaign.evidence import Outcome
from scripts.coverage_campaign.results import qualify
from tests.integration.live_stack import remote_quiescence as proof
from tests.integration.live_stack.evidence import RunIdentity, build_record
from tests.integration.live_stack.remote_lifecycle import RemoteHost


@pytest.fixture
def configuration() -> RemoteCaptureConfiguration:
    return RemoteCaptureConfiguration(
        resource_id=uuid4(),
        uri="qemu+tls://provider.invalid/system",
        client_cert_ref="file:client",
        client_key_ref="file:key",  # pragma: allowlist secret - synthetic reference, not a key
        ca_cert_ref="file:ca",
        secrets_root="/unused",
        storage_pool="default",
    )


@pytest.mark.parametrize(
    "fault", [None, "accept", "early", "death", "kill", "timeout", "identity", "node", "listener"]
)
def test_ordering_requires_all_native_boundary_observations(
    monkeypatch: pytest.MonkeyPatch,
    configuration: RemoteCaptureConfiguration,
    fault: str | None,
) -> None:
    events: list[str] = []
    processes = []

    class Process:
        def __init__(self, *, target, args):  # noqa: ANN001, ANN204 - external process double
            self.target, self.args = target, args
            self.alive = False
            self.exitcode = None
            processes.append(self)

        def start(self) -> None:
            self.alive = True

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            events.append("terminate")
            self.alive = fault == "kill" and "release" not in events

        def join(self, _timeout: float) -> None:
            if "release" in events:
                self.alive = False
                self.exitcode = 0

    class Pipe:
        def __init__(self) -> None:
            self.polls = 0
            self.receives = 0

        def poll(self, _timeout: float) -> bool:
            self.polls += 1
            return (
                self.polls in {1, 4}
                and not (fault == "timeout" and self.polls == 4)
                or (self.polls == 2 and fault == "early" or self.polls == 3 and fault == "death")
            )

        def recv(self) -> object:
            self.receives += 1
            if self.receives == 1:
                return "entered"
            config, domain, node, _pipe = processes[1].args
            return (
                str(uuid4() if fault == "identity" else config.resource_id),
                domain,
                node,
                "absent",
                "fresh-qmp-connection",
            )

        def close(self) -> None:
            events.append("pipe-closed")

    class Barrier:
        def __init__(self, _destination: str) -> None:
            self.messages = iter(["12345", "wrong" if fault == "accept" else "accepted", "closed"])
            self.process = SimpleNamespace(wait=lambda timeout: 1 if fault == "listener" else 0)

        def read(self) -> str:
            return next(self.messages)

        def release(self) -> None:
            events.append("release")

        def close(self) -> None:
            self.release()
            events.append("barrier-closed")

    @contextmanager
    def tls(_configuration):  # noqa: ANN001, ANN202 - native connection boundary
        yield SimpleNamespace(lookupByName=lambda name: object())

    def monitor(_domain, _raw, _flags):  # noqa: ANN001, ANN202 - native monitor boundary
        node = processes[0].args[2] if fault == "node" else "other"
        return '{"return":[{"node-name":"' + node + '"}]}'

    pipe = Pipe()
    monkeypatch.setattr(proof, "Barrier", Barrier)
    monkeypatch.setattr(proof, "tls", tls)
    monkeypatch.setattr(proof.libvirt_qemu, "qemuMonitorCommand", monitor)
    monkeypatch.setattr(
        proof.multiprocessing,
        "get_context",
        lambda method: SimpleNamespace(Pipe=lambda duplex: (pipe, pipe), Process=Process),
    )
    if fault is None:
        result = proof.ordered(configuration, "owned-domain", "provider.invalid")
        assert result["node_absent"] is True
        assert result["resource_and_domain_matched"] is True
    else:
        with pytest.raises(AssertionError):
            proof.ordered(configuration, "owned-domain", "provider.invalid")
    assert "barrier-closed" in events
    assert all(not process.is_alive() for process in processes)
    assert events[-2:] == ["pipe-closed", "pipe-closed"]


def test_actual_context_and_bound_node_qualify_without_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    candidate = "a" * 40
    monkeypatch.setattr(
        proof,
        "remote_host",
        lambda: RemoteHost("provider.invalid", "default", "rocky:10.2", "x86_64", "kvm"),
    )
    monkeypatch.setattr(proof, "staged_base_volume", lambda image: "image.qcow2")
    monkeypatch.setattr(proof, "volume_sha256", lambda *args: "b" * 64)
    inputs = proof.bindings(candidate)
    cell = proof.cells()[0]
    expected = inputs.cells[cell.id]
    assert expected.guest_os == "rocky:10" and expected.guest_arch == "x86_64"
    identity = RunIdentity(
        candidate,
        inputs.matrix_sha256,
        "rocky:10.2",
        "x86_64",
        True,
        dict.fromkeys(("server", "worker", "reconciler"), candidate),
    )
    record = build_record(
        cell,
        identity,
        outcome=Outcome.SUCCESS,
        context=expected,
        duration_s=2,
        assertions=dict.fromkeys(cell.assertions, "c" * 64),
    )
    contract = proof.build_contract()
    verdict = next(v for v in qualify(contract, inputs, [record]).cells if v.cell.id == cell.id)
    assert verdict.qualified, verdict.reasons
    missing_guest = record.model_copy(
        update={"context": expected.model_copy(update={"guest_os": None})}
    )
    verdict = next(
        v for v in qualify(contract, inputs, [missing_guest]).cells if v.cell.id == cell.id
    )
    assert not verdict.qualified


@pytest.mark.parametrize("fault", ["uri", "domain"])
def test_binding_mismatch_fails_before_listener(
    monkeypatch: pytest.MonkeyPatch,
    configuration: RemoteCaptureConfiguration,
    fault: str,
) -> None:
    closed = []
    monkeypatch.setattr(proof, "remote_instance_names", lambda: ["selected"])
    monkeypatch.setattr(
        proof,
        "remote_config_for_resource",
        lambda name: SimpleNamespace(
            uri="qemu+tls://different.invalid/system" if fault == "uri" else configuration.uri
        ),
    )
    connection = SimpleNamespace(
        lookupByName=lambda name: SimpleNamespace(UUIDString=lambda: "observer-domain"),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(proof, "observer", lambda destination: connection)

    @contextmanager
    def tls(_configuration):  # noqa: ANN001, ANN202 - native boundary
        yield SimpleNamespace(
            lookupByName=lambda name: SimpleNamespace(UUIDString=lambda: "different-domain")
        )

    monkeypatch.setattr(proof, "tls", tls)
    with pytest.raises(
        AssertionError, match="URI mismatch" if fault == "uri" else "domain mismatch"
    ):
        proof.verify_domain(configuration, "owned-domain", "provider.invalid")
    assert closed == ([] if fault == "uri" else [True])


@pytest.mark.parametrize("disconnect", [False, True])
def test_listener_closes_real_accepted_socket_on_release_or_controller_loss(
    monkeypatch: pytest.MonkeyPatch,
    disconnect: bool,
) -> None:
    native = proof.subprocess.Popen
    monkeypatch.setattr(
        proof.subprocess,
        "Popen",
        lambda argv, **kwargs: native([sys.executable, "-u", "-c", proof._LISTENER], **kwargs),
    )
    barrier = proof.Barrier("provider.invalid")
    try:
        port = int(barrier.read())
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            assert barrier.read() == "accepted"
            if disconnect:
                assert barrier.process.stdin is not None
                barrier.process.stdin.close()
                assert barrier.process.wait(5) == 3
            else:
                barrier.release()
                assert barrier.read() == "closed"
                assert barrier.process.wait(5) == 0
            assert client.recv(1) == b""
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=2)
    finally:
        barrier.close()
