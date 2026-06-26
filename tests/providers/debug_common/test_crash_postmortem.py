"""Provider-neutral worker-side crash postmortem (ADR-0031/0083/0084)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import CrashResult
from kdive.providers.shared.debug_common import crash_postmortem
from kdive.providers.shared.debug_common.crash_postmortem import (
    _real_run_crash,
    run_crash_postmortem,
)
from kdive.security.secrets.secret_registry import SecretRegistry


def _run(stdout: bytes) -> CrashResult:
    return CrashResult(exit_status=0, stdout=stdout, stderr=b"")


def test_runs_commands_and_redacts() -> None:
    fetched = {"core-ref": b"CORE", "debug-ref": b"VMLINUX"}
    build_id_inputs: list[bytes] = []
    crash_calls: list[tuple[str, str, str]] = []

    def _read_build_id(data: bytes) -> str:
        build_id_inputs.append(data)
        return "deadbeef"

    def _run_crash(vmlinux: object, core: object, script: str) -> CrashResult:
        crash_calls.append((str(vmlinux), str(core), script))
        return _run(b"OK")

    out = run_crash_postmortem(
        vmcore_ref="core-ref",
        debuginfo_ref="debug-ref",
        expected_build_id="deadbeef",
        commands=["bt", "ps"],
        fetch_object=lambda ref: fetched[ref],
        read_build_id=_read_build_id,
        run_crash=_run_crash,
        secret_registry=SecretRegistry(),
    )
    assert out.results == {"bt": {"ran": True}, "ps": {"ran": True}}
    assert out.transcript == "OK"
    assert out.truncated is False
    # The build-id is read from the *fetched vmcore bytes*, not some other buffer.
    assert build_id_inputs == [b"CORE"]
    # run_crash receives the vmlinux temp path, the vmcore temp path (suffixes mark each),
    # and a script that joins the commands with newlines and appends the quit terminator.
    assert len(crash_calls) == 1
    vmlinux_path, core_path, script = crash_calls[0]
    assert vmlinux_path.endswith(".vmlinux")
    assert core_path.endswith(".vmcore")
    assert script == "bt\nps\nquit\n"


def test_transcript_redacts_against_supplied_registry() -> None:
    # The supplied registry (not the process-global default) seeds the redactor: a value
    # registered here must be masked out of the crash transcript.
    registry = SecretRegistry()
    registry.register("hunter2-unique-token", scope=None)
    out = run_crash_postmortem(
        vmcore_ref="core-ref",
        debuginfo_ref="debug-ref",
        expected_build_id="deadbeef",
        commands=["bt"],
        fetch_object=lambda ref: b"CORE",
        read_build_id=lambda data: "deadbeef",
        run_crash=lambda vmlinux, core, script: _run(b"value=hunter2-unique-token done"),
        secret_registry=registry,
    )
    assert "hunter2-unique-token" not in out.transcript
    assert "[REDACTED]" in out.transcript


def test_transcript_decodes_invalid_utf8_with_replacement() -> None:
    # crash stdout is arbitrary bytes; an invalid UTF-8 sequence must decode with the
    # replacement character rather than raising (a strict decode would crash the worker).
    out = run_crash_postmortem(
        vmcore_ref="core-ref",
        debuginfo_ref="debug-ref",
        expected_build_id="deadbeef",
        commands=["bt"],
        fetch_object=lambda ref: b"CORE",
        read_build_id=lambda data: "deadbeef",
        run_crash=lambda vmlinux, core, script: _run(b"head\xfftail"),
        secret_registry=SecretRegistry(),
    )
    assert out.transcript == "head�tail"


def test_build_id_mismatch_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        run_crash_postmortem(
            vmcore_ref="core-ref",
            debuginfo_ref="debug-ref",
            expected_build_id="aaaa",
            commands=["bt"],
            fetch_object=lambda ref: b"CORE",
            read_build_id=lambda data: "bbbb",
            run_crash=lambda vmlinux, core, script: _run(b"OK"),
            secret_registry=SecretRegistry(),
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # The provenance failure is reported verbatim and carries the offending vmcore ref so an
    # operator can trace which core was rejected.
    assert str(exc.value) == "captured vmcore build-id does not match the Run's debuginfo build-id"
    assert exc.value.details == {"vmcore_ref": "core-ref"}


def test_nonzero_exit_with_empty_stdout_is_infrastructure_failure() -> None:
    # crash(8) that cannot initialize over the core/namelist exits non-zero with no usable
    # output; that must surface as a typed failure, not a success-reporting empty transcript.
    with pytest.raises(CategorizedError) as exc:
        run_crash_postmortem(
            vmcore_ref="core-ref",
            debuginfo_ref="debug-ref",
            expected_build_id="deadbeef",
            commands=["sys"],
            fetch_object=lambda ref: b"CORE",
            read_build_id=lambda data: "deadbeef",
            run_crash=lambda v, c, s: CrashResult(
                exit_status=1, stdout=b"  \n", stderr=b"cannot open core"
            ),
            secret_registry=SecretRegistry(),
        )
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details["exit_status"] == 1
    assert exc.value.details["stderr"] == "cannot open core"


def test_nonzero_exit_with_transcript_is_returned_not_discarded() -> None:
    # crash continues a batch past a per-command error and may still exit non-zero; the
    # already-produced transcript must be returned, not thrown away.
    out = run_crash_postmortem(
        vmcore_ref="core-ref",
        debuginfo_ref="debug-ref",
        expected_build_id="deadbeef",
        commands=["sys", "struct nope"],
        fetch_object=lambda ref: b"CORE",
        read_build_id=lambda data: "deadbeef",
        run_crash=lambda v, c, s: CrashResult(
            exit_status=1, stdout=b"SYSTEM MAP: ...\n", stderr=b"struct: invalid"
        ),
        secret_registry=SecretRegistry(),
    )
    assert out.transcript == "SYSTEM MAP: ...\n"


def test_nonzero_exit_stderr_is_redacted_and_capped() -> None:
    # stderr can echo secrets/paths; it is redacted against the supplied registry and capped
    # before it enters the error envelope.
    registry = SecretRegistry()
    registry.register("hunter2-secret", scope=None)
    with pytest.raises(CategorizedError) as exc:
        run_crash_postmortem(
            vmcore_ref="core-ref",
            debuginfo_ref="debug-ref",
            expected_build_id="deadbeef",
            commands=["sys"],
            fetch_object=lambda ref: b"CORE",
            read_build_id=lambda data: "deadbeef",
            run_crash=lambda v, c, s: CrashResult(
                exit_status=2, stdout=b"", stderr=b"key=hunter2-secret " + b"x" * 4000
            ),
            secret_registry=registry,
        )
    stderr = exc.value.details["stderr"]
    assert isinstance(stderr, str)
    assert "hunter2-secret" not in stderr
    assert len(stderr) <= 2048


def test_rejected_command_batch_is_configuration_error() -> None:
    with pytest.raises(CategorizedError) as exc:
        run_crash_postmortem(
            vmcore_ref="core-ref",
            debuginfo_ref="debug-ref",
            expected_build_id="deadbeef",
            commands=["rm -rf /"],
            fetch_object=lambda ref: b"CORE",
            read_build_id=lambda data: "deadbeef",
            run_crash=lambda vmlinux, core, script: _run(b"OK"),
            secret_registry=SecretRegistry(),
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "crash command batch rejected"
    # The validator's rejection reason is surfaced under `reason` so the caller learns why.
    assert "reason" in exc.value.details
    assert exc.value.details["reason"]


def test_real_run_crash_missing_binary_is_missing_dependency() -> None:
    # No crash(8) on the worker host: surface a missing_dependency naming the binary, not the
    # old stub's misleading "runs only under the live_vm gate" message.
    with pytest.raises(CategorizedError) as exc:
        _real_run_crash(Path("/v"), Path("/c"), "sys\nquit\n", crash_path_finder=lambda name: None)
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert "crash" in str(exc.value)


def test_real_run_crash_builds_fixed_argv_and_pipes_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # crash is invoked with a fixed argv (`crash -s <vmlinux> <vmcore>`); the validated batch
    # is fed on stdin only; cwd is the vmcore's worker-owned spool dir, not the process CWD.
    captured: dict[str, object] = {}

    def fake_exec(argv: list[str], script: str, cwd: Path) -> CrashResult:
        captured["argv"] = argv
        captured["script"] = script
        captured["cwd"] = cwd
        return CrashResult(exit_status=0, stdout=b"OK", stderr=b"")

    monkeypatch.setattr(crash_postmortem, "_exec_crash", fake_exec)
    out = _real_run_crash(
        Path("/tmp/x.vmlinux"),
        Path("/tmp/x.vmcore"),
        "sys\nquit\n",
        crash_path_finder=lambda name: "/usr/bin/crash",
    )
    assert out.stdout == b"OK"
    assert captured["argv"] == ["/usr/bin/crash", "-s", "/tmp/x.vmlinux", "/tmp/x.vmcore"]
    assert captured["script"] == "sys\nquit\n"
    assert captured["cwd"] == Path("/tmp")


# ---------------------------------------------------------------------------
# live_vm acceptance: the real /usr/bin/crash over a real captured core (ADR-0249)
# ---------------------------------------------------------------------------
#
# CI deselects ``live_vm`` (``just test`` runs ``-m "not live_vm and not live_stack"``), so
# this is SKIPPED unless the operator runs ``just test-live`` with a real captured core. It is
# the only test that exercises the real ``_exec_crash`` subprocess seam — everything above
# injects a fake ``run_crash``.

_LIVE_VMCORE_ENV = "KDIVE_LIVE_VM_VMCORE"
_LIVE_VMLINUX_ENV = "KDIVE_LIVE_VM_VMLINUX"


@pytest.mark.live_vm
def test_live_vm_real_crash_runs_sys_over_a_real_core() -> None:  # pragma: no cover - live_vm
    """Run the real ``crash(8)`` ``sys`` verb over a real captured core (ADR-0249).

    Skips unless the operator points ``KDIVE_LIVE_VM_VMCORE`` / ``KDIVE_LIVE_VM_VMLINUX`` at a
    real captured vmcore and its matching ``vmlinux`` debuginfo and ``crash(8)`` is installed.
    Proves the production crash invocation (fixed argv, batch on stdin, ``-s`` silent mode)
    actually drives the binary and returns its output.
    """
    import os
    import shutil as _shutil

    vmcore = os.environ.get(_LIVE_VMCORE_ENV)
    vmlinux = os.environ.get(_LIVE_VMLINUX_ENV)
    if not vmcore or not vmlinux:
        pytest.skip(
            f"{_LIVE_VMCORE_ENV}/{_LIVE_VMLINUX_ENV} not set; needs a real captured core + vmlinux"
        )
    if not _shutil.which("crash"):
        pytest.skip("crash(8) not installed on this host")

    result = _real_run_crash(Path(vmlinux), Path(vmcore), "sys\nquit\n")

    assert result.exit_status == 0, result.stderr.decode("utf-8", "replace")
    # crash's `sys` banner always prints these labels over a real core.
    assert b"KERNEL:" in result.stdout or b"DUMPFILE:" in result.stdout
