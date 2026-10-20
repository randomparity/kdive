"""LocalLibvirtTrafficCapture drives filter-dump attach/detach over QMP passthrough (ADR-0385)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.providers.local_libvirt.lifecycle import traffic_capture as traffic_capture_module
from kdive.providers.local_libvirt.lifecycle.traffic_capture import LocalLibvirtTrafficCapture
from kdive.providers.shared.runtime_paths import PCAP_HYPERVISOR_WRITE_REMEDIATION
from kdive.providers.shared.traffic_capture.execution import CaptureExecutor


class _FakeConn:
    def __init__(self, domain: object) -> None:
        self._domain = domain

    def lookupByName(self, name: str) -> object:  # noqa: N802 - mirrors the libvirt binding name
        return self._domain

    def close(self) -> int:
        return 0


_OK = json.dumps({"return": {}, "id": "libvirt-1"})
_ABSENT = json.dumps(
    {
        "error": {"class": "GenericError", "desc": "object 'kdive-dump-J' not found"},
        "id": "libvirt-2",
    }
)


def _refused(desc: str, error_class: str = "GenericError") -> str:
    return json.dumps({"error": {"class": error_class, "desc": desc}, "id": "libvirt-3"})


def _capturer(monitor):
    domain = object()
    cap = LocalLibvirtTrafficCapture(connect=lambda: _FakeConn(domain), monitor=monitor)
    return cap


def test_attach_deletes_stale_then_adds_filter_dump() -> None:
    seen: list[dict] = []

    def monitor(domain, cmd, flags):
        seen.append(json.loads(cmd))
        return _OK

    _capturer(monitor).attach(
        "kdive-x",
        qom_id="kdive-dump-J",
        dest_path="/var/lib/kdive/pcap/S/J.pcap",
        snaplen=128,
    )

    assert seen[0]["execute"] == "object-del"
    assert seen[0]["arguments"]["id"] == "kdive-dump-J"
    add = seen[1]
    assert add["execute"] == "object-add"
    args = add["arguments"]
    assert args["qom-type"] == "filter-dump"
    assert args["id"] == "kdive-dump-J"
    # The captured netdev is the local-libvirt SSH-forward netdev, chosen internally.
    assert args["netdev"] == "kdivessh"
    assert args["file"] == "/var/lib/kdive/pcap/S/J.pcap"
    assert args["maxlen"] == 128


@pytest.mark.parametrize(
    "absent",
    [_ABSENT, _refused("no such object", "DeviceNotFound")],
    ids=["not-found-text", "device-not-found-class"],
)
def test_attach_tolerates_an_absent_filter_reply_on_first_run(absent: str) -> None:
    """QEMU reports a missing id as an error *reply*, not a libvirtError (ADR-0385 amendment)."""
    calls: list[str] = []

    def monitor(domain, cmd, flags):
        parsed = json.loads(cmd)
        calls.append(parsed["execute"])
        return absent if parsed["execute"] == "object-del" else _OK

    # Must NOT raise: the first capture has no stale filter to delete.
    _capturer(monitor).attach("kdive-x", qom_id="kdive-dump-J", dest_path="/p.pcap", snaplen=128)
    assert calls == ["object-del", "object-add"]


def _add_refused_with(reply: str):
    def monitor(domain, cmd, flags):
        return reply if json.loads(cmd)["execute"] == "object-add" else _ABSENT

    return monitor


def test_attach_eacces_open_refusal_is_a_configuration_error_without_the_path() -> None:
    path = "/var/lib/kdive/pcap/S/J.pcap"
    monitor = _add_refused_with(_refused(f"net dump: can't open {path}: Permission denied"))

    with pytest.raises(CategorizedError) as excinfo:
        _capturer(monitor).attach("kdive-x", qom_id="kdive-dump-J", dest_path=path, snaplen=128)

    error = excinfo.value
    assert error.category is ErrorCategory.CONFIGURATION_ERROR
    assert error.details == {
        "reason": "pcap_open_denied",
        "operation": "object_add",
        "error": "GenericError",
        "remediation": PCAP_HYPERVISOR_WRITE_REMEDIATION,
    }
    assert path not in str(error)


@pytest.mark.parametrize(
    "reply",
    [
        _refused("net dump: can't open /p/J.pcap: No such file or directory"),
        _refused("net dump: can't open /p/J.pcap: No space left on device"),
        _refused("net dump: can't open /p/J.pcap: Permission denied (stale)"),
        _refused("Invalid parameter 'netdev'", "CommandNotFound"),
    ],
    ids=["enoent", "enospc", "suffix-mismatch", "other-class"],
)
def test_attach_other_refusal_is_a_retryable_control_failure(reply: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        _capturer(_add_refused_with(reply)).attach(
            "kdive-x", qom_id="kdive-dump-J", dest_path="/p/J.pcap", snaplen=128
        )

    error = excinfo.value
    assert error.category is ErrorCategory.CONTROL_FAILURE
    assert error.details["reason"] == "capture_filter_rejected"
    assert error.details["operation"] == "object_add"
    assert error.details["error"] == json.loads(reply)["error"]["class"]
    assert "/p/J.pcap" not in json.dumps(error.details)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "{}",
        "[]",
        '{"return": {}, "error": {"class": "GenericError"}}',
        '{"error": "denied"}',
        '{"error": {"desc": "no class"}}',
    ],
    ids=["empty", "neither", "not-object", "both", "error-not-object", "error-no-class"],
)
def test_attach_malformed_reply_is_a_control_failure(reply: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        _capturer(lambda domain, cmd, flags: reply).attach(
            "kdive-x", qom_id="kdive-dump-J", dest_path="/p.pcap", snaplen=128
        )

    assert excinfo.value.category is ErrorCategory.CONTROL_FAILURE
    assert excinfo.value.details["reason"] == "qmp_reply_malformed"


def test_detach_tolerates_an_absent_filter_reply() -> None:
    _capturer(lambda domain, cmd, flags: _ABSENT).detach("kdive-x", qom_id="kdive-dump-J")


def test_detach_refusal_is_a_control_failure() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        _capturer(lambda domain, cmd, flags: _refused("object is busy")).detach(
            "kdive-x", qom_id="kdive-dump-J"
        )

    assert excinfo.value.category is ErrorCategory.CONTROL_FAILURE
    assert excinfo.value.details["reason"] == "capture_filter_rejected"
    assert excinfo.value.details["operation"] == "object_del"


def test_executor_surfaces_a_refused_attach_without_polling(tmp_path) -> None:
    """A refused add plus an absent detach reaches the child as the attach error, zero polls."""
    path = tmp_path / "J.pcap"
    capturer = _capturer(
        _add_refused_with(_refused(f"net dump: can't open {path}: Permission denied"))
    )
    capturer.prepare = lambda system_id, job_id: str(path)  # type: ignore[method-assign]
    sleeps: list[float] = []
    executor = CaptureExecutor(capturer=capturer, provider_label="local", sleep=sleeps.append)
    request = CaptureRequest(
        job_id=uuid4(),
        provider_kind="local-libvirt",
        resource_id=uuid4(),
        system_id=uuid4(),
        domain_name="kdive-x",
        snaplen=128,
        max_bytes=1_048_576,
        max_polls=30,
    )

    with pytest.raises(CategorizedError) as excinfo:
        executor.execute(request, tmp_path)

    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "pcap_open_denied"
    assert sleeps == []


def test_attach_reraises_other_monitor_error_as_control_failure() -> None:
    def monitor(domain, cmd, flags):
        raise libvirt.libvirtError("some other monitor failure")

    with pytest.raises(CategorizedError) as excinfo:
        _capturer(monitor).attach("kdive-x", qom_id="q", dest_path="/p", snaplen=128)
    assert excinfo.value.category is ErrorCategory.CONTROL_FAILURE


def test_detach_issues_object_del() -> None:
    seen: list[dict] = []

    def monitor(domain, cmd, flags):
        seen.append(json.loads(cmd))
        return _OK

    _capturer(monitor).detach("kdive-x", qom_id="kdive-dump-J")
    assert seen[0]["execute"] == "object-del"
    assert seen[0]["arguments"]["id"] == "kdive-dump-J"


def _noop_monitor(domain, cmd, flags):  # pragma: no cover - unused by the file-side tests
    return _OK


def test_write_remediation_is_the_local_hypervisor_remedy() -> None:
    assert _capturer(_noop_monitor).write_remediation == PCAP_HYPERVISOR_WRITE_REMEDIATION


def test_write_remediation_fits_the_forwarded_detail_shape() -> None:
    """The capture child forwards only printable ASCII remediation up to 512 characters (#3269)."""
    remediation = PCAP_HYPERVISOR_WRITE_REMEDIATION
    assert remediation.isascii() and remediation.isprintable() and len(remediation) <= 512
    assert "svirt_image_t" in remediation and "kdive-live-libvirt" in remediation


def test_prepare_prepares_dir_and_returns_worker_pcap_path(monkeypatch, tmp_path) -> None:
    system_id, job_id = uuid4(), uuid4()
    prepared: list[object] = []
    monkeypatch.setattr(traffic_capture_module, "prepare_pcap_dir", prepared.append)
    monkeypatch.setattr(
        traffic_capture_module, "pcap_path", lambda sid, jid: tmp_path / f"{sid}-{jid}.pcap"
    )

    dest = _capturer(_noop_monitor).prepare(system_id, job_id)

    assert prepared == [system_id]  # the QEMU-writable dir prep ran for the System
    assert dest == str(tmp_path / f"{system_id}-{job_id}.pcap")


def test_captured_size_reads_growing_file_and_zero_when_absent(tmp_path) -> None:
    cap = _capturer(_noop_monitor)
    dest = tmp_path / "cap.pcap"
    assert cap.captured_size(str(dest)) == 0  # not yet written
    dest.write_bytes(b"abcd")
    assert cap.captured_size(str(dest)) == 4


def test_fetch_reads_whole_file_and_empty_when_absent(tmp_path) -> None:
    cap = _capturer(_noop_monitor)
    dest = tmp_path / "cap.pcap"
    assert cap.fetch(str(dest), max_bytes=10) == b""  # absent capture is empty
    dest.write_bytes(b"pcapbytes")
    assert cap.fetch(str(dest), max_bytes=10) == b"pcapbytes"


def test_reclaim_deletes_file_and_tolerates_absent(tmp_path) -> None:
    cap = _capturer(_noop_monitor)
    dest = tmp_path / "cap.pcap"
    dest.write_bytes(b"x")
    cap.reclaim(str(dest))
    assert not dest.exists()
    cap.reclaim(str(dest))  # second reclaim (already gone) must not raise


def test_prepare_pre_deletes_this_jobs_stale_pcap(monkeypatch, tmp_path) -> None:
    """An at-least-once retry starts from a clean file (ADR-0567); job-keyed, not a sweep."""
    system_id, job_id, other_job = uuid4(), uuid4(), uuid4()
    stale = tmp_path / f"{system_id}-{job_id}.pcap"
    concurrent = tmp_path / f"{system_id}-{other_job}.pcap"
    stale.write_bytes(b"stale")
    concurrent.write_bytes(b"live")
    monkeypatch.setattr(traffic_capture_module, "prepare_pcap_dir", lambda _sid: None)
    monkeypatch.setattr(
        traffic_capture_module,
        "pcap_path",
        lambda sid, jid: tmp_path / f"{sid}-{jid}.pcap",
    )

    _capturer(_noop_monitor).prepare(system_id, job_id)

    assert not stale.exists()  # this job's stale file is gone
    assert concurrent.read_bytes() == b"live"  # a different job's file survives


def test_prepare_tolerates_an_absent_stale_pcap(monkeypatch, tmp_path) -> None:
    system_id, job_id = uuid4(), uuid4()
    monkeypatch.setattr(traffic_capture_module, "prepare_pcap_dir", lambda _sid: None)
    monkeypatch.setattr(
        traffic_capture_module,
        "pcap_path",
        lambda sid, jid: tmp_path / f"{sid}-{jid}.pcap",
    )

    dest = _capturer(_noop_monitor).prepare(system_id, job_id)  # must not raise

    assert dest == str(tmp_path / f"{system_id}-{job_id}.pcap")


def test_prepare_logs_a_permission_failure_but_still_succeeds(
    monkeypatch, tmp_path, caplog
) -> None:
    """A suppressed unlink failure is operator-visible, never silent (ADR-0567)."""
    system_id, job_id = uuid4(), uuid4()
    stale = tmp_path / f"{system_id}-{job_id}.pcap"
    stale.write_bytes(b"stale")
    monkeypatch.setattr(traffic_capture_module, "prepare_pcap_dir", lambda _sid: None)
    monkeypatch.setattr(
        traffic_capture_module,
        "pcap_path",
        lambda sid, jid: tmp_path / f"{sid}-{jid}.pcap",
    )

    def _refuse(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(1, "operation not permitted")

    monkeypatch.setattr(Path, "unlink", _refuse)
    with caplog.at_level("WARNING"):
        dest = _capturer(_noop_monitor).prepare(system_id, job_id)
    assert dest == str(stale)  # prepare still returns the path
    assert "could not be removed" in caplog.text
