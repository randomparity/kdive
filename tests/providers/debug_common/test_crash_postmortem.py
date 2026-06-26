"""Provider-neutral worker-side crash postmortem (ADR-0031/0083/0084)."""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import CrashResult
from kdive.providers.shared.debug_common.crash_postmortem import run_crash_postmortem
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
