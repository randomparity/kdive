"""LocalLibvirtTrafficCapture drives filter-dump attach/detach over QMP passthrough (ADR-0385)."""

from __future__ import annotations

import json
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle import traffic_capture as traffic_capture_module
from kdive.providers.local_libvirt.lifecycle.traffic_capture import LocalLibvirtTrafficCapture
from kdive.providers.shared.runtime_paths import PCAP_HYPERVISOR_WRITE_REMEDIATION


class _FakeConn:
    def __init__(self, domain: object) -> None:
        self._domain = domain

    def lookupByName(self, name: str) -> object:  # noqa: N802 - mirrors the libvirt binding name
        return self._domain

    def close(self) -> int:
        return 0


def _capturer(monitor):
    domain = object()
    cap = LocalLibvirtTrafficCapture(connect=lambda: _FakeConn(domain), monitor=monitor)
    return cap


def test_attach_deletes_stale_then_adds_filter_dump() -> None:
    seen: list[dict] = []

    def monitor(domain, cmd, flags):
        seen.append(json.loads(cmd))
        return "{}"

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


def test_attach_swallows_object_not_found_on_first_run() -> None:
    calls: list[str] = []

    def monitor(domain, cmd, flags):
        parsed = json.loads(cmd)
        calls.append(parsed["execute"])
        if parsed["execute"] == "object-del":
            raise libvirt.libvirtError("Device 'kdive-dump-J' not found")
        return "{}"

    # Must NOT raise: the first capture has no stale filter to delete.
    _capturer(monitor).attach("kdive-x", qom_id="kdive-dump-J", dest_path="/p.pcap", snaplen=128)
    assert calls == ["object-del", "object-add"]


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
        return "{}"

    _capturer(monitor).detach("kdive-x", qom_id="kdive-dump-J")
    assert seen[0]["execute"] == "object-del"
    assert seen[0]["arguments"]["id"] == "kdive-dump-J"


def _noop_monitor(domain, cmd, flags):  # pragma: no cover - unused by the file-side tests
    return "{}"


def test_write_remediation_is_the_local_hypervisor_remedy() -> None:
    assert _capturer(_noop_monitor).write_remediation == PCAP_HYPERVISOR_WRITE_REMEDIATION


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
