"""One shared capture QOM-object naming convention across producer and reaper (ADR-0556).

Every reaper must reconstruct the exact string the capture producer attached under, so a
duplicated literal is drift that detaches nothing. These guards pin both halves: the real
producers name their sink through :func:`capture_qom_id`, and no other module in ``src/``
spells the convention out again.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.providers.local_libvirt.lifecycle.capture_operation import LocalCaptureExecutor
from kdive.providers.ports.traffic import capture_qom_id
from kdive.providers.remote_libvirt.lifecycle.capture_operation import RemoteCaptureExecutor

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "kdive"
_CONVENTION_PREFIX = "kdive-dump-"
_DEFINING_MODULE = _SRC_ROOT / "providers" / "ports" / "traffic.py"
_PCAP_HEADER = b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00" + b"\x00" * 16


class _RecordingCapturer:
    """Record the exact ``qom_id`` the executor attaches and detaches under."""

    def __init__(self) -> None:
        self.attached: list[str] = []
        self.detached: list[str] = []

    @property
    def write_remediation(self) -> str:
        return "remediation"

    def prepare(self, system_id: object, job_id: object) -> str:
        del system_id, job_id
        return "/provider/capture.pcap"

    def attach(self, domain_name: str, *, qom_id: str, dest_path: str, snaplen: int) -> None:
        del domain_name, dest_path, snaplen
        self.attached.append(qom_id)

    def captured_size(self, dest_path: str) -> int:
        del dest_path
        return len(_PCAP_HEADER)

    def detach(self, domain_name: str, *, qom_id: str) -> None:
        del domain_name
        self.detached.append(qom_id)

    def fetch(self, dest_path: str, *, max_bytes: int) -> bytes:
        del dest_path, max_bytes
        return _PCAP_HEADER

    def reclaim(self, dest_path: str) -> None:
        del dest_path


def _request() -> CaptureRequest:
    return CaptureRequest(
        job_id=uuid4(),
        provider_kind="local-libvirt",
        resource_id=uuid4(),
        system_id=uuid4(),
        domain_name="kdive-guest",
        snaplen=128,
        max_bytes=1_048_576,
        max_polls=1,
    )


def test_capture_qom_id_names_only_the_owning_job() -> None:
    """The convention is job-scoped: two jobs never collide on one sink name."""
    first, second = uuid4(), uuid4()

    assert capture_qom_id(first) == f"{_CONVENTION_PREFIX}{first}"
    assert capture_qom_id(first) != capture_qom_id(second)


def test_local_producer_attaches_and_detaches_the_shared_qom_id(tmp_path: Path) -> None:
    capturer = _RecordingCapturer()
    request = _request()

    LocalCaptureExecutor(capturer=capturer, sleep=lambda _seconds: None).execute(request, tmp_path)

    assert capturer.attached == [capture_qom_id(request.job_id)]
    assert capturer.detached == [capture_qom_id(request.job_id)]


def test_remote_producer_attaches_and_detaches_the_shared_qom_id(tmp_path: Path) -> None:
    capturer = _RecordingCapturer()
    request = _request()

    RemoteCaptureExecutor(capturer=capturer, sleep=lambda _seconds: None).execute(request, tmp_path)

    assert capturer.attached == [capture_qom_id(request.job_id)]
    assert capturer.detached == [capture_qom_id(request.job_id)]


def test_the_convention_is_spelled_out_in_exactly_one_source_module() -> None:
    """A second literal is the drift this helper removes; the scan must also see the first."""
    modules = sorted(_SRC_ROOT.rglob("*.py"))
    spelling_it_out = [
        module for module in modules if _CONVENTION_PREFIX in module.read_text(encoding="utf-8")
    ]

    assert len(modules) > 100, "the source scan walked an unexpectedly small tree"
    assert spelling_it_out == [_DEFINING_MODULE]
