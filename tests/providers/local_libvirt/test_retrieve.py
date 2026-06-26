"""Tests for the local-libvirt Retrieve plane (ADR-0031)."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest

from kdive.artifacts.storage import (
    ArtifactStreamRequest,
    ArtifactWriteRequest,
    HeadResult,
    StoredArtifact,
)
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve import LocalLibvirtRetrieve
from kdive.providers.ports import CaptureOutput, CrashOutput, CrashResult
from kdive.providers.shared.runtime_paths import WORKER_READABILITY_REMEDIATION
from kdive.security.artifacts.crash_commands import crash_command_rejection_reason
from kdive.security.secrets.secret_registry import SecretRegistry

_ALLOW = frozenset({"bt", "log", "ps", "p", "rd"})

_SYS = UUID("33333333-3333-3333-3333-333333333333")
_RUN = UUID("44444444-4444-4444-4444-444444444444")
_TENANT = "local"


@pytest.mark.parametrize("command", ["bt", "  log ", "ps -A", "p jiffies"])
def test_allowed_commands_pass(command: str) -> None:
    assert crash_command_rejection_reason(command, _ALLOW) is None


@pytest.mark.parametrize(
    "command",
    [
        "",
        "   ",
        "bt | sh",
        "log > /etc/passwd",
        "rd `whoami`",
        "ps; reboot",
        "log $(id)",
        "!touch x",
        "log\nbt",
        "nuke now",
    ],
)
def test_rejected_commands_have_a_reason(command: str) -> None:
    assert crash_command_rejection_reason(command, _ALLOW) is not None


def _sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


@dataclass
class _FakeStore:
    """Records both byte puts and streamed puts; serves a matching ``head`` for verification."""

    puts: list[tuple[str, str, Sensitivity, bytes]] = field(default_factory=list)
    streams: list[tuple[str, str, Path, str]] = field(default_factory=list)
    heads: dict[str, HeadResult] = field(default_factory=dict)
    fail_on: str | None = None

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        if self.fail_on == request.name:
            raise CategorizedError(
                "synthetic put failure", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        key = request.key()
        self.puts.append((key, request.name, request.sensitivity, request.data))
        return StoredArtifact(
            key, "etag-" + request.name, request.sensitivity, request.retention_class
        )

    def put_stream(self, request: ArtifactStreamRequest) -> StoredArtifact:
        if self.fail_on == request.name:
            raise CategorizedError(
                "synthetic stream failure", category=ErrorCategory.INFRASTRUCTURE_FAILURE
            )
        key = request.key()
        self.streams.append((key, request.name, request.path, request.sha256_b64))
        self.heads[key] = HeadResult(
            size_bytes=request.path.stat().st_size,
            checksum_sha256=request.sha256_b64,
            etag="etag-" + request.name,
            sensitivity=request.sensitivity,
        )
        return StoredArtifact(
            key, "etag-" + request.name, request.sensitivity, request.retention_class
        )

    def head(self, key: str) -> HeadResult | None:
        return self.heads.get(key)


def _kdump_retriever(
    store: _FakeStore, *, core_path: Path | None, build_id: str = "deadbeef"
) -> LocalLibvirtRetrieve:
    return LocalLibvirtRetrieve(
        tenant=_TENANT,
        store_factory=lambda: store,
        wait_for_vmcore=lambda system_id: core_path,
        read_vmcore_build_id=lambda data: pytest.fail("bytes build-id seam used on kdump path"),
        read_vmcore_build_id_from_file=lambda path: build_id,
        extract_redacted_from_file=lambda path: b"dmesg: password=[REDACTED]",
        host_dump_capture=lambda _sid: pytest.fail("host_dump seam used on kdump path"),
        secret_registry=SecretRegistry(),
    )


def _spooled_core(tmp_path: Path, data: bytes) -> Path:
    """A core in its own spool dir, mirroring ``_real_wait_for_vmcore``'s ``mkdtemp`` layout."""
    spool = tmp_path / "spool"
    spool.mkdir()
    core = spool / "vmcore"
    core.write_bytes(data)
    return core


def test_capture_streams_raw_core_and_returns_build_id(tmp_path: Path) -> None:
    core = _spooled_core(tmp_path, b"RAWCORE")
    store = _FakeStore()
    out = _kdump_retriever(store, core_path=core).capture(_SYS, _RUN, CaptureMethod.KDUMP)
    assert isinstance(out, CaptureOutput)
    assert out.raw.key == f"{_TENANT}/runs/{_RUN}/vmcore-kdump"
    assert out.redacted.key == f"{_TENANT}/runs/{_RUN}/vmcore-kdump-redacted"
    assert out.vmcore_build_id == "deadbeef"
    assert out.raw_size_bytes == len(b"RAWCORE")
    # raw core went through the streaming put (a path), not a bytes put.
    assert len(store.streams) == 1
    stream_key, stream_name, stream_path, stream_sha = store.streams[0]
    assert stream_name == "vmcore-kdump"
    assert stream_sha == _sha256_b64(b"RAWCORE")
    # only the redacted derivative is a bytes put.
    assert [name for _, name, _, _ in store.puts] == ["vmcore-kdump-redacted"]
    redacted_data = next(d for _, name, _, d in store.puts if name == "vmcore-kdump-redacted")
    assert b"hunter2" not in redacted_data and b"[REDACTED]" in redacted_data


def test_capture_removes_the_spool_dir_on_success(tmp_path: Path) -> None:
    core = _spooled_core(tmp_path, b"RAWCORE")
    _kdump_retriever(_FakeStore(), core_path=core).capture(_SYS, _RUN, CaptureMethod.KDUMP)
    assert not core.exists()
    assert not core.parent.exists()


def test_capture_removes_the_spool_dir_on_store_failure(tmp_path: Path) -> None:
    core = _spooled_core(tmp_path, b"RAWCORE")
    retr = _kdump_retriever(_FakeStore(fail_on="vmcore-kdump"), core_path=core)
    with pytest.raises(CategorizedError) as exc:
        retr.capture(_SYS, _RUN, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert not core.exists()
    assert not core.parent.exists()


def test_capture_no_core_is_readiness_failure() -> None:
    with pytest.raises(CategorizedError) as exc:
        _kdump_retriever(_FakeStore(), core_path=None).capture(_SYS, _RUN, CaptureMethod.KDUMP)
    assert exc.value.category is ErrorCategory.READINESS_FAILURE


def test_capture_store_failure_is_infrastructure_failure(tmp_path: Path) -> None:
    core = _spooled_core(tmp_path, b"X")
    with pytest.raises(CategorizedError) as exc:
        _kdump_retriever(_FakeStore(fail_on="vmcore-kdump"), core_path=core).capture(
            _SYS, _RUN, CaptureMethod.KDUMP
        )
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_capture_verifies_stored_checksum(tmp_path: Path) -> None:
    core = _spooled_core(tmp_path, b"RAWCORE")

    @dataclass
    class _CorruptingStore(_FakeStore):
        def head(self, key: str) -> HeadResult | None:
            base = super().head(key)
            if base is None:
                return None
            return HeadResult(
                size_bytes=base.size_bytes,
                checksum_sha256="mismatch",
                etag=base.etag,
                sensitivity=base.sensitivity,
            )

    with pytest.raises(CategorizedError) as exc:
        _kdump_retriever(_CorruptingStore(), core_path=core).capture(
            _SYS, _RUN, CaptureMethod.KDUMP
        )
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert not core.exists()


def _crash_retriever(*, observed_build_id: str, crash: CrashResult) -> LocalLibvirtRetrieve:
    return LocalLibvirtRetrieve(
        tenant=_TENANT,
        store_factory=_FakeStore,
        wait_for_vmcore=lambda s: None,
        read_vmcore_build_id=lambda data: observed_build_id,
        read_vmcore_build_id_from_file=lambda path: observed_build_id,
        extract_redacted_from_file=lambda path: b"",
        host_dump_capture=lambda s: None,
        secret_registry=SecretRegistry(),
        fetch_object=lambda ref: b"BYTES",
        run_crash=lambda vmlinux, vmcore, script: crash,
    )


def test_run_returns_redacted_crash_output() -> None:
    crash = CrashResult(exit_status=0, stdout=b"$ log\npassword=hunter2\nok", stderr=b"")
    out = _crash_retriever(observed_build_id="deadbeef", crash=crash).run_crash_postmortem(
        vmcore_ref="k/systems/s/vmcore",
        debuginfo_ref="k/runs/r/vmlinux",
        expected_build_id="deadbeef",
        commands=["log"],
    )
    assert isinstance(out, CrashOutput)
    assert "hunter2" not in out.transcript and "[REDACTED]" in out.transcript


def test_run_build_id_mismatch_is_configuration_error() -> None:
    crash = CrashResult(exit_status=0, stdout=b"", stderr=b"")
    with pytest.raises(CategorizedError) as exc:
        _crash_retriever(observed_build_id="aaaa", crash=crash).run_crash_postmortem(
            vmcore_ref="v",
            debuginfo_ref="d",
            expected_build_id="bbbb",
            commands=["log"],
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_run_rejects_bad_command_before_fetching_or_running_crash() -> None:
    fetched: list[str] = []

    retriever = LocalLibvirtRetrieve(
        tenant=_TENANT,
        store_factory=_FakeStore,
        wait_for_vmcore=lambda s: None,
        read_vmcore_build_id=lambda data: "deadbeef",
        read_vmcore_build_id_from_file=lambda path: "deadbeef",
        extract_redacted_from_file=lambda path: b"",
        host_dump_capture=lambda s: None,
        secret_registry=SecretRegistry(),
        fetch_object=lambda ref: fetched.append(ref) or b"BYTES",
        run_crash=lambda vmlinux, vmcore, script: pytest.fail("crash seam should not run"),
    )

    with pytest.raises(CategorizedError) as exc:
        retriever.run_crash_postmortem(
            vmcore_ref="v",
            debuginfo_ref="d",
            expected_build_id="deadbeef",
            commands=["bt | sh"],
        )

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["reason"] == "disallowed metacharacter '|'"
    assert fetched == []


def _host_dump_retriever(
    store: _FakeStore, *, core_path: Path | None, build_id: str = "hostbid"
) -> LocalLibvirtRetrieve:
    """A retriever whose host_dump seam yields a spooled ``Path`` (file-streamed, #657)."""
    return LocalLibvirtRetrieve(
        tenant=_TENANT,
        store_factory=lambda: store,
        wait_for_vmcore=lambda _sid: pytest.fail("kdump seam used for host_dump"),
        read_vmcore_build_id=lambda _b: pytest.fail("bytes build-id seam used on host_dump path"),
        read_vmcore_build_id_from_file=lambda _p: build_id,
        extract_redacted_from_file=lambda _p: b"dmesg: password=[REDACTED]",
        host_dump_capture=lambda _sid: core_path,
        secret_registry=SecretRegistry(),
    )


def test_capture_host_dump_streams_raw_core_and_returns_build_id(tmp_path: Path) -> None:
    core = _spooled_core(tmp_path, b"\x7fELFcore")
    store = _FakeStore()
    out = _host_dump_retriever(store, core_path=core).capture(_SYS, _RUN, CaptureMethod.HOST_DUMP)
    assert isinstance(out, CaptureOutput)
    assert out.raw.key == f"{_TENANT}/runs/{_RUN}/vmcore-host_dump"
    assert out.redacted.key == f"{_TENANT}/runs/{_RUN}/vmcore-host_dump-redacted"
    assert out.vmcore_build_id == "hostbid"
    assert out.raw_size_bytes == len(b"\x7fELFcore")
    # the raw core is streamed (a path), not held as bytes.
    assert len(store.streams) == 1
    stream_key, stream_name, stream_path, stream_sha = store.streams[0]
    assert stream_name == "vmcore-host_dump"
    assert stream_sha == _sha256_b64(b"\x7fELFcore")
    assert [name for _, name, _, _ in store.puts] == ["vmcore-host_dump-redacted"]
    redacted_data = next(d for _, name, _, d in store.puts if name == "vmcore-host_dump-redacted")
    assert b"[REDACTED]" in redacted_data


def test_capture_host_dump_removes_the_spool_dir_on_success(tmp_path: Path) -> None:
    core = _spooled_core(tmp_path, b"\x7fELFcore")
    _host_dump_retriever(_FakeStore(), core_path=core).capture(_SYS, _RUN, CaptureMethod.HOST_DUMP)
    assert not core.exists()
    assert not core.parent.exists()


def test_capture_host_dump_removes_the_spool_dir_on_store_failure(tmp_path: Path) -> None:
    core = _spooled_core(tmp_path, b"\x7fELFcore")
    retr = _host_dump_retriever(_FakeStore(fail_on="vmcore-host_dump"), core_path=core)
    with pytest.raises(CategorizedError) as exc:
        retr.capture(_SYS, _RUN, CaptureMethod.HOST_DUMP)
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert not core.exists()
    assert not core.parent.exists()


def test_capture_host_dump_no_core_is_readiness_failure() -> None:
    with pytest.raises(CategorizedError) as exc:
        _host_dump_retriever(_FakeStore(), core_path=None).capture(
            _SYS, _RUN, CaptureMethod.HOST_DUMP
        )
    assert exc.value.category is ErrorCategory.READINESS_FAILURE


def _retriever_with_build_id_seam(
    store: _FakeStore, *, core_path: Path, build_id_seam: Callable[[Path], str]
) -> LocalLibvirtRetrieve:
    """A host_dump retriever whose build-id seam (the first spooled-core read) the caller sets."""
    return LocalLibvirtRetrieve(
        tenant=_TENANT,
        store_factory=lambda: store,
        wait_for_vmcore=lambda _sid: pytest.fail("kdump seam used for host_dump"),
        read_vmcore_build_id=lambda _b: pytest.fail("bytes build-id seam used on host_dump path"),
        read_vmcore_build_id_from_file=build_id_seam,
        extract_redacted_from_file=lambda _p: pytest.fail("dmesg read after a build-id failure"),
        host_dump_capture=lambda _sid: core_path,
        secret_registry=SecretRegistry(),
    )


def test_capture_host_dump_unreadable_core_is_configuration_error(tmp_path: Path) -> None:
    """A root-owned host_dump core (qemu:///system writes it as root) is a host config problem,
    not an uncategorized infrastructure failure (ADR-0223). The build-id read fails first."""

    def deny(_p: Path) -> str:
        raise PermissionError(13, "Permission denied")

    core = _spooled_core(tmp_path, b"\x7fELFcore")
    retr = _retriever_with_build_id_seam(_FakeStore(), core_path=core, build_id_seam=deny)
    with pytest.raises(CategorizedError) as exc:
        retr.capture(_SYS, _RUN, CaptureMethod.HOST_DUMP)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["operation"] == "read_spooled_core"
    assert exc.value.details["remediation"] == WORKER_READABILITY_REMEDIATION
    # the spool is still cleaned up despite the failure.
    assert not core.exists()
    assert not core.parent.exists()


def test_capture_host_dump_missing_dependency_is_not_remapped(tmp_path: Path) -> None:
    """A drgn-absent MISSING_DEPENDENCY (a CategorizedError, not a PermissionError) must surface
    unchanged — the PermissionError remap must not swallow it."""

    def no_drgn(_p: Path) -> str:
        raise CategorizedError(
            "drgn is not installed on this worker host",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )

    core = _spooled_core(tmp_path, b"\x7fELFcore")
    retr = _retriever_with_build_id_seam(_FakeStore(), core_path=core, build_id_seam=no_drgn)
    with pytest.raises(CategorizedError) as exc:
        retr.capture(_SYS, _RUN, CaptureMethod.HOST_DUMP)
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert not core.parent.exists()


def test_capture_host_dump_verifies_stored_checksum(tmp_path: Path) -> None:
    core = _spooled_core(tmp_path, b"\x7fELFcore")

    @dataclass
    class _CorruptingStore(_FakeStore):
        def head(self, key: str) -> HeadResult | None:
            base = super().head(key)
            if base is None:
                return None
            return HeadResult(
                size_bytes=base.size_bytes,
                checksum_sha256="mismatch",
                etag=base.etag,
                sensitivity=base.sensitivity,
            )

    with pytest.raises(CategorizedError) as exc:
        _host_dump_retriever(_CorruptingStore(), core_path=core).capture(
            _SYS, _RUN, CaptureMethod.HOST_DUMP
        )
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert not core.exists()


def test_local_retrieve_from_env_wires_real_crash_runner() -> None:
    # Production assembly must wire the real crash(8) runner, not the removed stub, so
    # postmortem.crash/triage actually run over a captured core on the deployed worker.
    from kdive.providers.shared.debug_common.crash_postmortem import _real_run_crash

    retriever = LocalLibvirtRetrieve.from_env(secret_registry=SecretRegistry())
    assert retriever._run_crash is _real_run_crash
