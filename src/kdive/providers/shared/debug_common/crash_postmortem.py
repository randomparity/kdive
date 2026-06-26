"""Provider-neutral worker-side `crash` postmortem over a captured vmcore (ADR-0084).

The worker-side half of the Retrieve plane is identical for every provider: fetch the
core + debuginfo from the object store, verify the core's build-id matches the Run's,
run a validated `crash` command batch over an injected subprocess, and return the
redacted transcript. Lifted out of `local_libvirt/retrieve.py` so `remote_libvirt`
reuses it without a private copy (the ADR-0083 `debug_common` home for shared
worker-side postmortem code). Slow seams (`fetch_object`, `run_crash`, `read_build_id`)
are injected; the defaults are `live_vm`-only.
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 - fixed argv only, no shell; the command batch goes via stdin
import tempfile
from collections.abc import Callable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports import CrashOutput, CrashResult
from kdive.security.artifacts.crash_commands import validate_crash_commands
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import object_store_from_env

type FetchObject = Callable[[str], bytes]
type ReadBuildId = Callable[[bytes], str]
type RunCrash = Callable[[Path, Path, str], CrashResult]
type CrashPathFinder = Callable[[str], str | None]

# Cap the redacted crash(8) stderr carried in an error envelope; stderr is unbounded.
_STDERR_CAP = 2048
# Bound a wedged crash(8) so it never pins a worker thread. A batch of allowlisted read verbs
# over a multi-GB core can take minutes.
_CRASH_TIMEOUT_S = 300.0


def run_crash_postmortem(
    *,
    vmcore_ref: str,
    debuginfo_ref: str,
    expected_build_id: str,
    commands: list[str],
    fetch_object: FetchObject,
    read_build_id: ReadBuildId,
    run_crash: RunCrash,
    secret_registry: SecretRegistry,
) -> CrashOutput:
    """Symbolize the core against ``debuginfo_ref`` and run the crash command batch.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for a rejected crash command or a
            build-id provenance mismatch; ``STALE_HANDLE`` when a referenced object is
            missing; ``INFRASTRUCTURE_FAILURE`` for object-store IO failures.
    """
    rejected = validate_crash_commands(commands)
    if rejected is not None:
        raise CategorizedError(
            "crash command batch rejected",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": rejected},
        )
    vmcore_bytes = fetch_object(vmcore_ref)
    observed = read_build_id(vmcore_bytes)
    if observed != expected_build_id:
        raise CategorizedError(
            "captured vmcore build-id does not match the Run's debuginfo build-id",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"vmcore_ref": vmcore_ref},
        )
    vmlinux_bytes = fetch_object(debuginfo_ref)
    with (
        tempfile.NamedTemporaryFile(suffix=".vmcore") as core_file,
        tempfile.NamedTemporaryFile(suffix=".vmlinux") as vmlinux_file,
    ):
        core_file.write(vmcore_bytes)
        core_file.flush()
        vmlinux_file.write(vmlinux_bytes)
        vmlinux_file.flush()
        script = "\n".join(commands) + "\nquit\n"
        crash = run_crash(Path(vmlinux_file.name), Path(core_file.name), script)
    redactor = Redactor(registry=secret_registry)
    transcript = redactor.redact_text(crash.stdout.decode("utf-8", "replace"))
    if crash.exit_status != 0 and not transcript.strip():
        # crash continues a batch past a per-command error, so a non-zero exit *with* output is
        # kept (the transcript is still useful). A non-zero exit with no output is the
        # init-failure shape (e.g. an incompatible core it could not open) — surface it instead
        # of reporting a success with an empty transcript.
        stderr = redactor.redact_text(crash.stderr.decode("utf-8", "replace"))
        raise CategorizedError(
            "the crash(8) subprocess exited non-zero with no output; the core was not analyzed",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"exit_status": crash.exit_status, "stderr": stderr[:_STDERR_CAP]},
        )
    return CrashOutput(
        results={cmd: {"ran": True} for cmd in commands},
        transcript=transcript,
        truncated=False,
    )


def default_fetch_object(ref: str) -> bytes:  # pragma: no cover - live_vm
    # The ref is a key the system itself produced; no client etag handle, so the read
    # is unconditional (ADR-0054). A missing object raises STALE_HANDLE in get_artifact.
    return object_store_from_env().get_artifact(ref, None).data


def default_read_vmcore_build_id(data: bytes) -> str:  # pragma: no cover - live_vm
    raise CategorizedError(
        "vmcore build-id extraction runs only under the live_vm gate",
        category=ErrorCategory.MISSING_DEPENDENCY,
    )


def _real_run_crash(
    vmlinux: Path,
    vmcore: Path,
    script: str,
    *,
    crash_path_finder: CrashPathFinder = shutil.which,
) -> CrashResult:
    """Run the real ``crash(8)`` over the spooled core; the batch goes on stdin only.

    The fixed argv ``crash -s <vmlinux> <vmcore>`` (``-s`` suppresses the banner and the
    ``crash>`` prompt echo) reaches the binary; the validated, ``quit``-terminated command
    batch is piped on stdin, never argv. ``crash_path_finder`` is injected so the
    binary-absent branch is testable off the ``live_vm`` gate.

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` when ``crash`` is not installed on this
            worker host; ``INFRASTRUCTURE_FAILURE`` for a launch failure or timeout.
    """
    crash_path = crash_path_finder("crash")
    if crash_path is None:
        raise CategorizedError(
            "the crash(8) utility is not installed on this worker host",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    argv = [crash_path, "-s", str(vmlinux), str(vmcore)]
    return _exec_crash(argv, script, vmcore.parent)


def _exec_crash(  # pragma: no cover - live_vm
    argv: list[str], script: str, cwd: Path
) -> CrashResult:
    """Spawn ``crash`` with the batch on stdin; ``cwd`` is the worker-owned spool dir.

    The ``# pragma: no cover - live_vm`` covers the real subprocess (a host with
    ``/usr/bin/crash`` and a real core). ``cwd`` points at the spool dir so crash never needs
    a writable process CWD; ``_CRASH_TIMEOUT_S`` bounds a wedged crash so the worker thread is
    always released.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; the batch goes via stdin only
            argv,
            input=script.encode("utf-8"),
            timeout=_CRASH_TIMEOUT_S,
            check=False,
            capture_output=True,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            "the crash(8) subprocess exceeded the timeout",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"timeout_s": _CRASH_TIMEOUT_S},
        ) from exc
    except OSError as exc:
        raise CategorizedError(
            "could not launch the crash(8) subprocess",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
    return CrashResult(exit_status=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


__all__ = [
    "FetchObject",
    "ReadBuildId",
    "RunCrash",
    "default_fetch_object",
    "default_read_vmcore_build_id",
    "run_crash_postmortem",
]
