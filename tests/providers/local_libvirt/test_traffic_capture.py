"""LocalLibvirtTrafficCapture drives filter-dump attach/detach over QMP passthrough (ADR-0384)."""

from __future__ import annotations

import json

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.traffic_capture import LocalLibvirtTrafficCapture


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
