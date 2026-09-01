"""Local supervised-capture execution and fresh-connection quiescence (ADR-0558)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from uuid import UUID, uuid4

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.providers.local_libvirt import composition
from kdive.providers.local_libvirt.lifecycle.capture_operation import (
    LocalLibvirtCaptureQuiescence,
)
from kdive.providers.ports.traffic import LocalCaptureConfiguration
from kdive.providers.shared.traffic_capture.execution import CaptureExecutor

_PCAP_HEADER = b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00" + b"\x00" * 16


def _request() -> CaptureRequest:
    return CaptureRequest(
        job_id=uuid4(),
        provider_kind="local-libvirt",
        resource_id=uuid4(),
        system_id=uuid4(),
        domain_name="kdive-local",
        snaplen=128,
        max_bytes=1_048_576,
        max_polls=2,
    )


class _FakeCapturer:
    def __init__(self, *, fail: str | None = None, data: bytes = _PCAP_HEADER) -> None:
        self.fail = fail
        self.data = data
        self.calls: list[str] = []

    @property
    def write_remediation(self) -> str:
        return "local remediation"

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.fail == name:
            raise CategorizedError(
                f"{name} failed",
                category=ErrorCategory.CONTROL_FAILURE,
                details={"secret": "must-not-cross"},  # pragma: allowlist secret
            )

    def prepare(self, system_id: UUID, job_id: UUID) -> str:
        del system_id, job_id
        self._call("prepare")
        return "/provider/capture.pcap"

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


def test_local_executor_runs_synchronously_and_reclaims(tmp_path: Path) -> None:
    capturer = _FakeCapturer()
    executor = CaptureExecutor(
        capturer=capturer, provider_label="local", sleep=lambda _seconds: None
    )
    result = executor.execute(_request(), tmp_path)

    assert result.size_bytes == len(_PCAP_HEADER)
    assert result.truncated is False
    assert (tmp_path / "capture.pcap").read_bytes() == _PCAP_HEADER
    assert capturer.calls == [
        "prepare",
        "attach",
        "captured_size",
        "captured_size",
        "detach",
        "fetch",
        "reclaim",
    ]


@pytest.mark.parametrize(
    "method", ["prepare", "attach", "captured_size", "detach", "fetch", "reclaim"]
)
def test_local_executor_surfaces_each_provider_failure_and_reclaims_when_possible(
    tmp_path: Path, method: str
) -> None:
    capturer = _FakeCapturer(fail=method)

    with pytest.raises(CategorizedError, match=f"{method} failed"):
        executor = CaptureExecutor(
            capturer=capturer, provider_label="local", sleep=lambda _seconds: None
        )
        executor.execute(_request(), tmp_path)

    if method != "prepare":
        assert "reclaim" in capturer.calls
    assert not (tmp_path / "capture.pcap").exists()


def test_local_executor_rejects_oversized_provider_result(tmp_path: Path) -> None:
    request = _request()
    capturer = _FakeCapturer(data=b"x" * (request.max_bytes + 1))

    with pytest.raises(CategorizedError) as excinfo:
        executor = CaptureExecutor(
            capturer=capturer, provider_label="local", sleep=lambda _seconds: None
        )
        executor.execute(request, tmp_path)

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert not (tmp_path / "capture.pcap").exists()


def test_local_executor_observes_exact_bound_after_final_poll_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request().model_copy(update={"max_polls": 1})
    capturer = _FakeCapturer(data=b"x" * request.max_bytes)
    interval_elapsed = False

    def captured_size(_destination: str) -> int:
        capturer.calls.append("captured_size")
        return request.max_bytes if interval_elapsed else 0

    def sleep(_seconds: float) -> None:
        nonlocal interval_elapsed
        capturer.calls.append("sleep")
        interval_elapsed = True

    monkeypatch.setattr(capturer, "captured_size", captured_size)

    executor = CaptureExecutor(capturer=capturer, provider_label="local", sleep=sleep)
    result = executor.execute(request, tmp_path)

    assert result.truncated is True
    assert result.size_bytes == request.max_bytes
    assert capturer.calls == [
        "prepare",
        "attach",
        "sleep",
        "captured_size",
        "detach",
        "fetch",
        "reclaim",
    ]


class _ProbeDomain:
    pass


class _ProbeConnection:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.closed = False

    def lookupByName(self, name: str) -> _ProbeDomain:  # noqa: N802
        self.events.append(f"lookup:{name}")
        return _ProbeDomain()

    def close(self) -> int:
        self.closed = True
        return 0


def _ordered_monitor(present: bool = False, *, members: list[object] | None = None):
    def monitor(domain: object, raw: str, flags: int) -> str:
        del domain, flags
        command = json.loads(raw)
        assert "id" not in command
        if command["execute"] == "object-del":
            raise libvirt.libvirtError("DeviceNotFound")
        returned_members = (
            [{"name": "kdive-dump-job", "type": "child<filter-dump>"}] if present else members or []
        )
        return json.dumps({"return": returned_members, "id": "libvirt-15"})

    return monitor


def test_local_quiescence_reconnects_detaches_and_queries_exact_qom_id() -> None:
    resource_id = uuid4()
    opened: list[_ProbeConnection] = []
    events: list[str] = []

    def connect() -> _ProbeConnection:
        connection = _ProbeConnection(events)
        opened.append(connection)
        return connection

    evidence = LocalLibvirtCaptureQuiescence(
        resource_id=resource_id,
        connect=connect,
        monitor=_ordered_monitor(
            members=[{"name": "unrelated-object", "type": "child<filter-dump>"}]
        ),
    ).prove_absent(resource_id, "kdive-local", "kdive-dump-job")

    assert len(opened) == 1
    assert opened[0].closed is True
    assert evidence.result == "absent"
    assert evidence.qom_id == "kdive-dump-job"
    assert events == ["lookup:kdive-local"]


@pytest.mark.parametrize(
    "members",
    [
        [{}],
        [{"name": 7, "type": "child<filter-dump>"}],
        [{"name": None, "type": "child<filter-dump>"}],
        [{"name": "", "type": "child<filter-dump>"}],
        [{"name": "other", "type": 7}],
        [{"name": "other", "type": None}],
        [{"name": "other", "type": ""}],
        [{"name": "other"}],
        [{"type": "child<filter-dump>"}],
        [
            {"name": "valid-first", "type": "child<filter-dump>"},
            {},
        ],
    ],
)
def test_local_quiescence_rejects_malformed_qom_members(members: list[object]) -> None:
    resource_id = uuid4()
    probe = LocalLibvirtCaptureQuiescence(
        resource_id=resource_id,
        connect=lambda: _ProbeConnection([]),
        monitor=_ordered_monitor(members=members),
    )

    with pytest.raises(CategorizedError, match="inconclusive shape"):
        probe.prove_absent(resource_id, "kdive-local", "kdive-dump-job")


def test_local_fresh_probe_waits_for_prior_accepted_monitor_mutation() -> None:
    resource_id = uuid4()
    monitor_lock = threading.Lock()
    mutation_accepted = threading.Event()
    release_mutation = threading.Event()
    probe_finished = threading.Event()

    def prior_mutation() -> None:
        with monitor_lock:
            mutation_accepted.set()
            assert release_mutation.wait(timeout=2)

    def monitor(domain: object, raw: str, flags: int) -> str:
        del domain, flags
        command = json.loads(raw)
        assert "id" not in command
        if command["execute"] == "object-del":
            raise libvirt.libvirtError("DeviceNotFound")
        with monitor_lock:
            return json.dumps({"return": [], "id": "libvirt-16"})

    mutation = threading.Thread(target=prior_mutation)
    mutation.start()
    assert mutation_accepted.wait(timeout=2)
    probe = LocalLibvirtCaptureQuiescence(
        resource_id=resource_id,
        connect=lambda: _ProbeConnection([]),
        monitor=monitor,
    )
    observer = threading.Thread(
        target=lambda: (
            probe.prove_absent(resource_id, "kdive-local", "kdive-dump-job"),
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
def test_local_quiescence_fails_closed(failure: str) -> None:
    resource_id = uuid4()

    def connect() -> _ProbeConnection:
        if failure == "unreachable":
            raise libvirt.libvirtError("connection refused")
        return _ProbeConnection([])

    def unordered_monitor(domain: object, raw: str, flags: int) -> str:
        del domain, raw, flags
        return json.dumps({"return": []})

    probe = LocalLibvirtCaptureQuiescence(
        resource_id=resource_id,
        connect=connect,
        monitor=(
            unordered_monitor if failure == "unordered" else _ordered_monitor(failure == "presence")
        ),
    )
    observed_resource = uuid4() if failure == "resource" else resource_id

    with pytest.raises(CategorizedError):
        probe.prove_absent(observed_resource, "kdive-local", "kdive-dump-job")


def test_local_composition_snapshots_allowlisted_uri_for_post_release_spool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_id = uuid4()
    monkeypatch.setattr(composition.config, "require", lambda _setting: "qemu:///session")

    encoded = composition.capture_operation_configuration(resource_id)
    configuration = LocalCaptureConfiguration.from_canonical_json(encoded)

    assert configuration.resource_id == resource_id
    assert configuration.uri == "qemu:///session"
    assert b"KDIVE_DATABASE_URL" not in encoded
