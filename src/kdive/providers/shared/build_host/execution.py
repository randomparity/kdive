"""Build-host subprocess execution and artifact reader helpers."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - all calls use fixed argv and no shell
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.build_artifacts.validation import parse_gnu_build_id
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.build_host.sandbox import BuildSandbox, sandbox_run
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry

MAKE_TIMEOUT_S = 2 * 60 * 60
OBJCOPY_TIMEOUT_S = 60

# Trailing build-output bytes retained for the build-log artifact (#770, ADR-0238). 16 KiB — eight
# times the 2000-char pre-compile `STDERR_TAIL` (a compiler error trails many recipe-echo lines),
# yet under the 64 KiB inline-serve cap so the whole log returns in one `artifacts.get`.
BUILD_LOG_TAIL_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class CapturedStep:
    """A build step's exit code plus its redacted, tail-capped combined output (ADR-0238)."""

    returncode: int
    output: str

    @classmethod
    def from_streams(
        cls,
        returncode: int,
        stdout: str | None,
        stderr: str | None,
        *,
        registry: SecretRegistry | None = None,
    ) -> CapturedStep:
        """Combine, redact, and tail-cap a step's captured streams into a `CapturedStep`."""
        return cls(returncode, redact_and_cap(stdout, stderr, registry=registry))


def redact_and_cap(
    stdout: str | None, stderr: str | None, *, registry: SecretRegistry | None = None
) -> str:
    """Redact secrets from the combined stdout+stderr and keep the trailing build-log slice.

    The tail is kept (not the head): the failing recipe line and the compiler error live at the
    end of build output, so a head cap would discard exactly the bytes an agent needs.
    """
    combined = (stdout or "") + (stderr or "")
    registry = registry or SecretRegistry()
    redacted = Redactor(registry=registry).redact_text(combined)
    return redacted[-BUILD_LOG_TAIL_BYTES:]


type ReadConfig = Callable[[Path], str]
type RunStep = Callable[[Path], CapturedStep]
type RunModulesInstall = Callable[[Path, Path], int]
type ReadBytes = Callable[[Path], bytes]
type ReadBuildId = Callable[[Path], str]


def read_text_file(path: Path, *, category: ErrorCategory, file_label: str) -> str:
    """Read text or raise a categorized unreadable-file error."""
    try:
        return path.read_text()
    except OSError as exc:
        raise CategorizedError(
            f"{file_label} is missing or unreadable",
            category=category,
            details={"file": file_label},
        ) from exc


def read_bytes_file(path: Path, *, category: ErrorCategory, output: str) -> bytes:
    """Read bytes or raise a categorized unreadable-output error."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CategorizedError(
            f"{output} is missing or unreadable",
            category=category,
            details={"output": output},
        ) from exc


def launch_failure(tool: str, exc: OSError, *, category: ErrorCategory) -> CategorizedError:
    """Map a subprocess launch failure into the provider error taxonomy."""
    if isinstance(exc, FileNotFoundError):
        return CategorizedError(
            f"{tool} is required for kernel builds",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details={"tool": tool},
        )
    return CategorizedError(
        f"{tool} failed to launch",
        category=category,
        details={"tool": tool, "op": "launch"},
    )


def workspace_failure(op: str, path_label: str, exc: OSError) -> CategorizedError:
    """Map workspace filesystem failures into infrastructure failures."""
    return CategorizedError(
        f"build workspace {op} failed",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"op": op, "path": path_label},
    )


def real_read_config(workspace: Path) -> str:  # pragma: no cover - live_vm
    return read_text_file(
        workspace / ".config",
        category=ErrorCategory.CONFIGURATION_ERROR,
        file_label=".config",
    )


def real_read_kernel_image(workspace: Path) -> bytes:  # pragma: no cover - live_vm
    return read_bytes_file(
        workspace / "arch/x86/boot/bzImage",
        category=ErrorCategory.BUILD_FAILURE,
        output="bzImage",
    )


def real_read_vmlinux(workspace: Path) -> bytes:  # pragma: no cover - live_vm
    return read_bytes_file(
        workspace / "vmlinux",
        category=ErrorCategory.BUILD_FAILURE,
        output="vmlinux",
    )


def real_run_make(workspace: Path, sandbox: BuildSandbox | None = None) -> CapturedStep:
    """Run the default parallel kernel build (demoted when a sandbox is active)."""
    return run_make_target(workspace, [f"-j{os.cpu_count() or 1}"], "make", sandbox=sandbox)


def real_run_olddefconfig(workspace: Path, sandbox: BuildSandbox | None = None) -> CapturedStep:
    return run_make_target(workspace, ["olddefconfig"], "make olddefconfig", sandbox=sandbox)


def real_run_modules_install(
    workspace: Path, mod_root: Path, sandbox: BuildSandbox | None = None
) -> int:  # pragma: no cover
    return run_make_target(
        workspace,
        [f"INSTALL_MOD_PATH={mod_root}", "modules_install"],
        "make modules_install",
        sandbox=sandbox,
    ).returncode


def run_make_target(
    workspace: Path, args: list[str], label: str, sandbox: BuildSandbox | None = None
) -> CapturedStep:
    """Run ``make -C <workspace> <args...>`` (demoted when a sandbox is active); capture + map.

    Stdout and stderr are captured (not inherited) so a failing build's compiler output can be
    persisted as a build-log artifact (#770). A timeout still raises, but carries the partial
    captured output on ``details["build_log"]`` so a hung build is not a black hole.
    """
    try:
        completed = sandbox_run(
            sandbox,
            ["make", "-C", str(workspace), *args],
            timeout=MAKE_TIMEOUT_S,
            check=False,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise CategorizedError(
            f"{label} exceeded the build timeout",
            category=ErrorCategory.BUILD_FAILURE,
            details=_timeout_details(exc),
        ) from exc
    except OSError as exc:
        raise launch_failure("make", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE) from exc
    return CapturedStep.from_streams(completed.returncode, completed.stdout, completed.stderr)


def _timeout_details(exc: subprocess.TimeoutExpired) -> dict[str, object]:
    """Build-failure details for a timed-out make, carrying any partial captured output."""
    details: dict[str, object] = {"timeout_s": MAKE_TIMEOUT_S}
    build_log = redact_and_cap(_as_text(exc.stdout), _as_text(exc.stderr))
    if build_log:
        details["build_log"] = build_log
    return details


def _as_text(value: str | bytes | None) -> str | None:
    """Decode a ``TimeoutExpired`` stream (bytes when ``text`` was not honored) to text."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def real_read_build_id(workspace: Path) -> str:  # pragma: no cover - live_vm
    """Extract the produced ``vmlinux`` GNU build-id from its merged ``.notes`` section."""
    with tempfile.NamedTemporaryFile(suffix=".note") as note_file:
        try:
            subprocess.run(
                [
                    "objcopy",
                    "-O",
                    "binary",
                    "--only-section=.notes",
                    str(workspace / "vmlinux"),
                    note_file.name,
                ],
                timeout=OBJCOPY_TIMEOUT_S,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise CategorizedError(
                "objcopy exceeded the build-id extraction timeout",
                category=ErrorCategory.BUILD_FAILURE,
                details={"timeout_s": OBJCOPY_TIMEOUT_S},
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise CategorizedError(
                "objcopy failed to extract vmlinux notes",
                category=ErrorCategory.BUILD_FAILURE,
            ) from exc
        except OSError as exc:
            raise launch_failure(
                "objcopy", exc, category=ErrorCategory.INFRASTRUCTURE_FAILURE
            ) from exc
        notes = read_bytes_file(
            Path(note_file.name),
            category=ErrorCategory.BUILD_FAILURE,
            output="vmlinux notes",
        )
    return parse_gnu_build_id(notes)


def build_failure(message: str, run_id: UUID, *, build_log: str | None = None) -> CategorizedError:
    """A build failure with run-id details, optionally carrying captured build output.

    When ``build_log`` is non-empty it is attached under ``details["build_log"]`` so the builder
    can persist it as a ``build-log`` artifact (#770, ADR-0238); absent or empty, the details are
    unchanged so a pre-compile failure stays a black-box-free no-op on the build-log path.
    """
    details: dict[str, object] = {"run_id": str(run_id)}
    if build_log:
        details["build_log"] = build_log
    return CategorizedError(message, category=ErrorCategory.BUILD_FAILURE, details=details)
