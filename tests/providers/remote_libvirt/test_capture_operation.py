"""Remote supervised-capture execution and independent TLS quiescence (ADR-0558)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from uuid import UUID, uuid4

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.providers.ports.traffic import RemoteCaptureConfiguration
from kdive.providers.remote_libvirt import composition
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.lifecycle.capture_operation import (
    RemoteCaptureExecutor,
    RemoteLibvirtCaptureQuiescence,
)

_PCAP_HEADER = b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00" + b"\x00" * 16


def _request() -> CaptureRequest:
    return CaptureRequest(
        job_id=uuid4(),
        provider_kind="remote-libvirt",
        resource_id=uuid4(),
        system_id=uuid4(),
        domain_name="kdive-remote",
        snaplen=128,
        max_bytes=1_048_576,
        max_polls=1,
    )


class _FakeCapturer:
    def __init__(self, *, fail: str | None = None, data: bytes = _PCAP_HEADER) -> None:
        self.fail = fail
        self.data = data
        self.calls: list[str] = []

    @property
    def write_remediation(self) -> str:
        return "remote remediation"

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise CategorizedError(f"{name} failed", category=ErrorCategory.TRANSPORT_FAILURE)

    def prepare(self, system_id: UUID, job_id: UUID) -> str:
        del system_id, job_id
        self._call("prepare")
        return "/remote/capture.pcap"

    def attach(self, domain_name: str, *, qom_id: str, dest_path: str, snaplen: int) -> None:
        del domain_name, qom_id, dest_path, snaplen
        self._call("attach")

    def captured_size(self, dest_path: str) -> int:
        del dest_path
        self._call("captured_size")
        return len(self.data)

    def detach(self, domain_name: str, *, qom_id: str) -> None:
        del domain_name, qom_id
        self._call("detach")

    def fetch(self, dest_path: str, *, max_bytes: int) -> bytes:
        del dest_path, max_bytes
        self._call("fetch")
        return self.data

    def reclaim(self, dest_path: str) -> None:
        del dest_path
        self._call("reclaim")


@pytest.mark.parametrize(
    "method", ["prepare", "attach", "captured_size", "detach", "fetch", "reclaim"]
)
def test_remote_executor_faults_every_method_and_reclaims_without_descendants(
    tmp_path: Path, method: str
) -> None:
    capturer = _FakeCapturer(fail=method)
    pid = Path("/proc/self").resolve().name
    children = Path(f"/proc/{pid}/task/{pid}/children")
    before = children.read_text()

    with pytest.raises(CategorizedError, match=f"{method} failed"):
        RemoteCaptureExecutor(capturer=capturer, sleep=lambda _seconds: None).execute(
            _request(), tmp_path
        )

    after = children.read_text()
    assert after == before
    if method != "prepare":
        assert "reclaim" in capturer.calls


def test_remote_executor_writes_bounded_result_and_reclaims(tmp_path: Path) -> None:
    capturer = _FakeCapturer()

    result = RemoteCaptureExecutor(capturer=capturer, sleep=lambda _seconds: None).execute(
        _request(), tmp_path
    )

    assert result.size_bytes == len(_PCAP_HEADER)
    assert (tmp_path / "capture.pcap").read_bytes() == _PCAP_HEADER
    assert capturer.calls[-2:] == ["fetch", "reclaim"]


def test_remote_executor_rejects_oversized_provider_result(tmp_path: Path) -> None:
    request = _request()
    capturer = _FakeCapturer(data=b"x" * (request.max_bytes + 1))

    with pytest.raises(CategorizedError) as excinfo:
        RemoteCaptureExecutor(capturer=capturer, sleep=lambda _seconds: None).execute(
            request, tmp_path
        )

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert not (tmp_path / "capture.pcap").exists()


class _Domain:
    def __init__(self, *, present: bool = False, unordered: bool = False) -> None:
        self.present = present
        self.unordered = unordered
        self.commands: list[dict[str, object]] = []

    def qemuMonitorCommand(self, raw: str, flags: int) -> str:  # noqa: N802
        del flags
        command = json.loads(raw)
        self.commands.append(command)
        if command["execute"] == "object-del":
            raise libvirt.libvirtError("object not found")
        response: dict[str, object] = {
            "return": [{"name": "kdive-dump-job"}] if self.present else []
        }
        if not self.unordered:
            response["id"] = command["id"]
        return json.dumps(response)


class _Connection:
    def __init__(self, domain: _Domain) -> None:
        self.domain = domain
        self.closed = False

    def lookupByName(self, name: str) -> _Domain:  # noqa: N802
        del name
        return self.domain

    def close(self) -> None:
        self.closed = True


class _ConnectionContext:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> _Connection:
        return self.connection

    def __exit__(self, *_args: object) -> None:
        self.connection.close()


def test_remote_quiescence_opens_independent_resource_bound_tls_connection() -> None:
    resource_id = uuid4()
    domain = _Domain()
    connections: list[_Connection] = []

    def connection() -> _ConnectionContext:
        opened = _Connection(domain)
        connections.append(opened)
        return _ConnectionContext(opened)

    evidence = RemoteLibvirtCaptureQuiescence(
        resource_id=resource_id,
        connection=connection,
    ).prove_absent(resource_id, "kdive-remote", "kdive-dump-job")

    assert len(connections) == 1
    assert connections[0].closed is True
    assert [command["execute"] for command in domain.commands] == ["object-del", "qom-list"]
    assert domain.commands[1]["arguments"] == {"path": "/objects"}
    assert evidence.resource_id == resource_id


def test_remote_fresh_probe_waits_for_prior_accepted_monitor_mutation() -> None:
    resource_id = uuid4()
    monitor_lock = threading.Lock()
    mutation_accepted = threading.Event()
    release_mutation = threading.Event()
    probe_finished = threading.Event()

    class OrderedDomain(_Domain):
        def qemuMonitorCommand(self, raw: str, flags: int) -> str:  # noqa: N802
            command = json.loads(raw)
            if command["execute"] == "object-del":
                raise libvirt.libvirtError("object not found")
            with monitor_lock:
                return json.dumps({"return": [], "id": command["id"]})

    def prior_mutation() -> None:
        with monitor_lock:
            mutation_accepted.set()
            assert release_mutation.wait(timeout=2)

    mutation = threading.Thread(target=prior_mutation)
    mutation.start()
    assert mutation_accepted.wait(timeout=2)
    domain = OrderedDomain()
    probe = RemoteLibvirtCaptureQuiescence(
        resource_id=resource_id,
        connection=lambda: _ConnectionContext(_Connection(domain)),
    )
    observer = threading.Thread(
        target=lambda: (
            probe.prove_absent(resource_id, "kdive-remote", "kdive-dump-job"),
            probe_finished.set(),
        )
    )
    observer.start()
    assert not probe_finished.wait(timeout=0.05)
    release_mutation.set()
    observer.join(timeout=2)
    mutation.join(timeout=2)
    assert probe_finished.is_set()


@pytest.mark.parametrize("failure", ["presence", "unreachable", "unordered", "resource"])
def test_remote_quiescence_fails_closed(failure: str) -> None:
    resource_id = uuid4()
    domain = _Domain(present=failure == "presence", unordered=failure == "unordered")

    def connection() -> _ConnectionContext:
        if failure == "unreachable":
            raise CategorizedError("unreachable", category=ErrorCategory.TRANSPORT_FAILURE)
        return _ConnectionContext(_Connection(domain))

    probe = RemoteLibvirtCaptureQuiescence(resource_id=resource_id, connection=connection)

    with pytest.raises(CategorizedError):
        probe.prove_absent(
            uuid4() if failure == "resource" else resource_id,
            "kdive-remote",
            "kdive-dump-job",
        )


def test_remote_composition_snapshots_exact_resource_tls_without_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource_id = uuid4()
    resolved: list[str] = []

    def config_for_resource(name: str) -> RemoteLibvirtConfig:
        resolved.append(name)
        return RemoteLibvirtConfig(
            uri="qemu+tls://host-b.example/system",
            cert_refs=TlsCertRefs("host-b.crt", "host-b.key", "host-b-ca.crt"),
            concurrent_allocation_cap=2,
            storage_pool="host-b-pool",
        )

    monkeypatch.setattr(composition, "remote_config_for_resource", config_for_resource)
    monkeypatch.setattr(composition, "secrets_root_from_env", lambda: tmp_path)
    monkeypatch.setattr(
        composition,
        "database_url",
        lambda: (_ for _ in ()).throw(AssertionError("capture config must not read the database")),
    )

    encoded = composition.capture_operation_configuration(resource_id, "host-b")
    configuration = RemoteCaptureConfiguration.from_canonical_json(encoded)

    assert resolved == ["host-b"]
    assert configuration.resource_id == resource_id
    assert configuration.uri == "qemu+tls://host-b.example/system"
    assert configuration.client_key_ref == "host-b.key"  # pragma: allowlist secret
    assert configuration.secrets_root == str(tmp_path)
    assert b"postgres" not in encoded
