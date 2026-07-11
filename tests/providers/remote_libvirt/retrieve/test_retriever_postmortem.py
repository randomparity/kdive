"""Remote-libvirt retriever and postmortem wiring tests."""

from __future__ import annotations

from typing import cast
from uuid import UUID

import pytest

from kdive.artifacts.storage import StoredArtifact
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.retrieve import (
    CaptureOutput,
    CrashOutput,
    CrashResult,
)
from kdive.providers.remote_libvirt.retrieve import postmortem
from kdive.providers.remote_libvirt.retrieve.host_dump_capture import HostDumpCapturer
from kdive.providers.remote_libvirt.retrieve.kdump_capture import KdumpCapturer
from kdive.providers.remote_libvirt.retrieve.retriever import RemoteLibvirtRetriever
from kdive.security.secrets.secret_registry import SecretRegistry


class _Capturer:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[tuple[UUID, UUID]] = []

    def capture(self, system_id: UUID, run_id: UUID) -> CaptureOutput:
        self.calls.append((system_id, run_id))
        artifact = StoredArtifact(f"{self.label}/{run_id}", "etag", Sensitivity.SENSITIVE, "vmcore")
        return CaptureOutput(
            raw=artifact, redacted=artifact, vmcore_build_id=self.label, raw_size_bytes=0
        )


def test_retriever_dispatches_supported_capture_methods() -> None:
    system_id = UUID("00000000-0000-0000-0000-00000000faca")
    kdump = _Capturer("kdump")
    host_dump = _Capturer("host")
    retrieve = RemoteLibvirtRetriever(
        secret_registry=SecretRegistry(),
        kdump_capturer=cast(KdumpCapturer, kdump),
        host_dump_capturer=cast(HostDumpCapturer, host_dump),
    )

    run_id = UUID("00000000-0000-0000-0000-00000000facd")
    assert retrieve.capture(system_id, run_id, CaptureMethod.KDUMP).vmcore_build_id == "kdump"
    assert retrieve.capture(system_id, run_id, CaptureMethod.HOST_DUMP).vmcore_build_id == "host"
    assert kdump.calls == [(system_id, run_id)]
    assert host_dump.calls == [(system_id, run_id)]


def test_retriever_rejects_unsupported_capture_method() -> None:
    retrieve = RemoteLibvirtRetriever(
        secret_registry=SecretRegistry(),
        kdump_capturer=cast(KdumpCapturer, _Capturer("kdump")),
        host_dump_capturer=cast(HostDumpCapturer, _Capturer("host")),
    )

    with pytest.raises(CategorizedError) as exc:
        retrieve.capture(
            UUID("00000000-0000-0000-0000-00000000facb"),
            UUID("00000000-0000-0000-0000-00000000face"),
            CaptureMethod.CONSOLE,
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "remote-libvirt capture supports only the kdump and host_dump methods"
    # The details name the offending method under a stable key so the caller can act.
    assert exc.value.details == {"method": "console"}


def test_crash_postmortem_adapter_passes_injected_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SecretRegistry()
    calls: list[dict[str, object]] = []

    def fake_run_crash_postmortem(**kwargs: object) -> CrashOutput:
        calls.append(kwargs)
        return CrashOutput(results={"ok": True}, transcript="done", truncated=False)

    monkeypatch.setattr(postmortem, "_run_crash_postmortem", fake_run_crash_postmortem)

    def fetch_object(ref: str) -> bytes:
        return b"object"

    def read_build_id(data: bytes) -> str:
        return "build-id"

    def run_crash(_vmlinux: object, _vmcore: object, _script: object) -> CrashResult:
        return CrashResult(0, b"stdout", b"stderr")

    adapter = postmortem.CrashPostmortemAdapter(
        secret_registry=registry,
        fetch_object=fetch_object,
        read_build_id=read_build_id,
        run_crash=run_crash,
    )

    output = adapter.run_crash_postmortem(
        vmcore_ref="vmcore",
        debuginfo_ref="vmlinux",
        expected_build_id="build-id",
        commands=["bt"],
    )

    assert output.results == {"ok": True}
    assert calls[0]["vmcore_ref"] == "vmcore"
    assert calls[0]["debuginfo_ref"] == "vmlinux"
    assert calls[0]["expected_build_id"] == "build-id"
    assert calls[0]["commands"] == ["bt"]
    assert calls[0]["secret_registry"] is registry
    # Each role-specific seam is threaded through by identity — a swap to None or a
    # dropped kwarg (so the shared helper defaults it) would fail these.
    assert calls[0]["fetch_object"] is fetch_object
    assert calls[0]["read_build_id"] is read_build_id
    assert calls[0]["run_crash"] is run_crash


def test_crash_postmortem_adapter_defaults_to_real_crash_runner() -> None:
    import inspect

    from kdive.providers.shared.debug_common.crash_postmortem import _real_run_crash

    default = (
        inspect.signature(postmortem.CrashPostmortemAdapter.__init__)
        .parameters["run_crash"]
        .default
    )
    assert default is _real_run_crash
