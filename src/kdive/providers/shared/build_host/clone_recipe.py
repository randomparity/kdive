"""Transport-neutral git init/fetch/verify/checkout recipe."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory


class GitCommandResult(Protocol):
    """The command-result fields needed by the shared clone recipe."""

    returncode: int
    stdout: str
    stderr: str


type GitCommandRunner = Callable[[list[str]], GitCommandResult]
type StderrRedactor = Callable[[str], str]
type InitFailureMapper = Callable[[GitCommandResult, str], CategorizedError | None]


@dataclass(frozen=True, slots=True)
class GitCloneFailureMessages:
    """Failure messages for host-specific clone errors."""

    init_failed: str
    fetch_failed: str
    missing_fetch_head: str
    checkout_failed: str
    head_failed: str | None = None


def run_git_clone_recipe(
    *,
    remote: str,
    ref: str,
    dest: str,
    run: GitCommandRunner,
    redact_stderr: StderrRedactor,
    messages: GitCloneFailureMessages,
    map_init_failure: InitFailureMapper | None = None,
) -> str:
    """Run the shared shallow clone recipe and return the resolved commit SHA."""
    init = run(["init", dest])
    if init.returncode != 0:
        stderr = redact_stderr(init.stderr)
        if map_init_failure is not None:
            mapped = map_init_failure(init, stderr)
            if mapped is not None:
                raise mapped
        raise CategorizedError(
            messages.init_failed,
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": stderr},
        )

    fetch = run(["-C", dest, "fetch", "--depth", "1", remote, ref])
    if fetch.returncode != 0:
        raise CategorizedError(
            messages.fetch_failed,
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": redact_stderr(fetch.stderr)},
        )

    # ADR-0154: verify FETCH_HEAD regardless of fetch rc; some transports can mask remote rc 0.
    verify = run(["-C", dest, "rev-parse", "--verify", "--quiet", "FETCH_HEAD"])
    if verify.returncode != 0:
        raise CategorizedError(
            messages.missing_fetch_head,
            category=ErrorCategory.TRANSPORT_FAILURE,
            details={"stderr": redact_stderr(fetch.stderr)},
        )

    checkout = run(["-C", dest, "checkout", "FETCH_HEAD"])
    if checkout.returncode != 0:
        raise CategorizedError(
            messages.checkout_failed,
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"stderr": redact_stderr(checkout.stderr)},
        )

    if messages.head_failed is None:
        return verify.stdout.strip()
    head = run(["-C", dest, "rev-parse", "HEAD"])
    if head.returncode != 0:
        raise CategorizedError(
            messages.head_failed,
            category=ErrorCategory.TRANSPORT_FAILURE,
            details={"stderr": redact_stderr(head.stderr)},
        )
    return head.stdout.strip()
