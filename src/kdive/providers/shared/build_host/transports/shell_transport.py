"""ShellBuildTransport: the shared BuildTransport surface over a single host-exec primitive.

The two remote build transports — ``SshBuildTransport`` (over ``ssh``) and
``GuestExecBuildTransport`` (over the qemu-guest-agent exec channel) — share their entire
:class:`BuildTransport` surface and differ only in the primitive that runs one ``argv`` on
the host. This base implements ``run``/``read_text``/``read_bytes``/``clone``/``upload_file``/
``cleanup`` in terms of an abstract ``_run_remote``; subclasses provide ``_run_remote`` and
``write_bytes`` (whose framing — a stdin stream vs an in-line base64 pipeline — the
single-argv primitive does not generalize).
"""

from __future__ import annotations

import base64
import binascii
import logging

from kdive.artifacts.storage import PresignedUpload
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.build_transport import CommandResult
from kdive.providers.shared.build_host.clone_recipe import (
    GitCloneFailureMessages,
    GitCommandResult,
    run_git_clone_recipe,
)
from kdive.providers.shared.build_host.configuration.git_source import (
    _UNSAFE_CHARS,
    validate_git_arg,
)
from kdive.providers.shared.build_host.workspaces.workspace import redacted_tail
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)

# read_bytes/read_text base64-capture a whole remote file into memory. These reads are small
# (.config, the build-id note) — cap the captured (base64) output well above any legitimate
# value so a mis-pointed path cannot exhaust worker memory.
_MAX_REMOTE_READ_B64_BYTES = 8 * 1024 * 1024

# Clone operations get a longer budget than the small reads.
_CLONE_TIMEOUT_S = 10 * 60
_UPLOAD_TIMEOUT_S = 5 * 60

# The build-host agent diagnostic surfaced on a toolchain-missing build failure (ADR-0196). It is a
# literal here, not imported from `diagnostics/checks.py` (the legal import direction is
# diagnostics → providers), and matches the same pointer the registration rejection emits.
_BUILDHOST_AGENT_DIAGNOSTIC = "ops.diagnostics --with-buildhost-agent"

_REMOTE_CLONE_MESSAGES = GitCloneFailureMessages(
    init_failed="git init failed on remote",
    fetch_failed="git fetch failed on remote",
    missing_fetch_head="git fetch produced no FETCH_HEAD on remote (the fetch did not complete)",
    checkout_failed="git checkout FETCH_HEAD failed on remote",
    head_failed="git rev-parse HEAD failed on remote",
)


def _is_command_not_found(result: GitCommandResult, stderr_tail: str) -> bool:
    """Return True when *result* is a ``git: not found``-class failure (ADR-0196).

    The reliable primary signal is exit code 127 (the guest-exec / SSH shell reports it for a
    missing program). The stderr backstop covers a transport that does not surface 127: it
    requires *git* named together with a not-found token, read from the already-redacted tail so
    no unredacted bytes are inspected. A non-127 exit with any other stderr (a permission/disk
    fault) is not a command-not-found shape.
    """
    if result.returncode == 127:
        return True
    lowered = stderr_tail.lower()
    return "git" in lowered and ("not found" in lowered or "no such file" in lowered)


def _map_remote_git_init_failure(
    result: GitCommandResult, stderr_tail: str
) -> CategorizedError | None:
    if not _is_command_not_found(result, stderr_tail):
        return None
    return CategorizedError(
        "the build host's base image is missing the kernel build toolchain (git)",
        category=ErrorCategory.MISSING_DEPENDENCY,
        details={"diagnostic": _BUILDHOST_AGENT_DIAGNOSTIC, "stderr": stderr_tail},
    )


def _validate_url(url: str) -> None:
    """Reject a URL containing a control character before it reaches a remote command."""
    if any(c in _UNSAFE_CHARS for c in url):
        raise CategorizedError(
            "presigned URL contains a control character",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


def _extract_etag_from_headers(header_dump: str) -> str:
    """Parse the ETag value from a curl ``-D -`` header dump.

    Returns an empty string if the ETag header is absent.
    """
    for line in header_dump.splitlines():
        if line.lower().startswith("etag:"):
            return line.split(":", 1)[1].strip()
    return ""


class ShellBuildTransport:
    """Common BuildTransport methods over an abstract single-argv host-exec primitive.

    Subclasses MUST set ``self._secret_registry`` (for redacting secrets out of error details)
    and implement :meth:`_run_remote` and :meth:`write_bytes`.
    """

    _secret_registry: SecretRegistry

    # ------------------------------------------------------------------
    # Subclass-provided primitives
    # ------------------------------------------------------------------

    def _run_remote(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        """Run *argv* in *cwd* on the host with a hard *timeout_s* deadline."""
        raise NotImplementedError

    def write_bytes(self, path: str, data: bytes) -> None:
        """Write *data* to *path* on the host (framing is subclass-specific)."""
        raise NotImplementedError

    def _upload_url_detail(self, url: str) -> str:
        """The form of a presigned URL safe to place in an error detail (raw by default)."""
        return url

    # ------------------------------------------------------------------
    # BuildTransport surface (shared)
    # ------------------------------------------------------------------

    def run(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        """Run *argv* in *cwd* on the host."""
        return self._run_remote(argv, cwd=cwd, timeout_s=timeout_s)

    def read_text(self, path: str) -> str:
        """Read *path* as UTF-8 text from the host.

        Decodes the bytes from :meth:`read_bytes` as UTF-8 rather than relying on the
        transport's locale-default decoding.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the content is not valid UTF-8.
        """
        raw = self.read_bytes(path)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CategorizedError(
                f"remote file {path!r} is not valid UTF-8",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"path": path},
            ) from exc

    def read_bytes(self, path: str) -> bytes:
        """Read *path* as raw bytes from the host (via ``base64 -w0``).

        The captured base64 output is size-capped (``_MAX_REMOTE_READ_B64_BYTES``) so a
        mis-pointed path cannot exhaust worker memory; these reads are small by design.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` if the remote read fails;
                ``CONFIGURATION_ERROR`` if the captured output exceeds the size cap.
        """
        result = self._run_remote(["base64", "-w0", path], cwd="/", timeout_s=30)
        if result.returncode != 0:
            raise CategorizedError(
                f"remote read_bytes failed for {path!r}",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={
                    "path": path,
                    "stderr": redacted_tail(result.stderr, self._secret_registry),
                },
            )
        encoded = result.stdout.strip()
        if len(encoded) > _MAX_REMOTE_READ_B64_BYTES:
            raise CategorizedError(
                f"remote file {path!r} exceeds the maximum readable size",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"path": path, "max_b64_bytes": _MAX_REMOTE_READ_B64_BYTES},
            )
        try:
            return base64.b64decode(encoded)
        except (binascii.Error, ValueError) as exc:
            raise CategorizedError(
                "remote read_bytes returned malformed base64",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"path": path},
            ) from exc

    def clone(self, remote: str, ref: str, dest: str) -> str:
        """Clone *remote* at *ref* into *dest* using a shallow fetch; return the resolved commit.

        Validates *remote* and *ref* for control characters and leading dashes before issuing
        any host command. Uses ``git init`` + ``git fetch --depth 1`` + ``git checkout
        FETCH_HEAD`` to minimize data transferred (resolves an arbitrary ref/sha, which a plain
        ``clone --depth 1`` cannot).

        After the fetch — regardless of its reported exit status — ``FETCH_HEAD`` is verified
        to resolve before ``checkout`` runs. A fetch whose real failure is masked to exit 0 (a
        transport that does not propagate the remote command's status) leaves no ``FETCH_HEAD``;
        without this guard the next ``checkout FETCH_HEAD`` fails with an unrelated-looking
        ``pathspec 'FETCH_HEAD' did not match`` and the actionable network error in the fetch's
        own stderr is lost. The guard surfaces the fetch's stderr instead.

        Returns:
            The full 40-char commit SHA that ``ref`` resolved to (``git rev-parse HEAD`` after
            the checkout), threaded into the build's provenance record.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unsafe remote/ref, a failed
                ``git fetch``, or a failed ``git checkout FETCH_HEAD``; ``TRANSPORT_FAILURE``
                when the fetch reported success but produced no ``FETCH_HEAD`` (the fetch's own
                stderr is surfaced, not masked behind a later FETCH_HEAD pathspec error), or
                when the final ``git rev-parse HEAD`` fails;
                ``MISSING_DEPENDENCY`` when ``git init`` is a ``git: not found``-class failure
                (the base image lacks the build toolchain; ADR-0196), carrying a
                ``details["diagnostic"]`` pointer to the build-host agent check;
                ``INFRASTRUCTURE_FAILURE`` for any other failed ``git init`` (an environment
                fault).
        """
        validate_git_arg(remote, "remote")
        validate_git_arg(ref, "ref")

        return run_git_clone_recipe(
            remote=remote,
            ref=ref,
            dest=dest,
            run=lambda args: self._run_remote(["git", *args], cwd="/", timeout_s=_CLONE_TIMEOUT_S),
            redact_stderr=lambda stderr: redacted_tail(stderr, self._secret_registry),
            messages=_REMOTE_CLONE_MESSAGES,
            map_init_failure=_map_remote_git_init_failure,
        )

    def upload_file(self, path: str, presigned: PresignedUpload) -> str:
        """Upload *path* from the host to *presigned* URL via ``curl``; return the ETag.

        Runs ``curl -fsS -X PUT --upload-file <path> <url>`` with each required header via
        ``-H``, dumps the response headers to stdout (``-D -``), discards the body
        (``-o /dev/null``), and parses the ETag from the dumped headers.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if the URL contains control characters;
                ``INFRASTRUCTURE_FAILURE`` when curl exits non-zero or the upload response
                omits an ETag.
        """
        _validate_url(presigned.url)
        curl_argv = ["curl", "-fsS", "-X", "PUT", "--upload-file", path]
        for key, value in presigned.required_headers.items():
            curl_argv += ["-H", f"{key}: {value}"]
        curl_argv += ["-D", "-", "-o", "/dev/null", presigned.url]

        result = self._run_remote(curl_argv, cwd="/", timeout_s=_UPLOAD_TIMEOUT_S)
        if result.returncode != 0:
            raise CategorizedError(
                "remote curl PUT failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"url": self._upload_url_detail(presigned.url)},
            )
        etag = _extract_etag_from_headers(result.stdout).strip('"')
        if not etag:
            raise CategorizedError(
                "remote curl PUT response did not include an ETag",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"url": self._upload_url_detail(presigned.url)},
            )
        return etag

    def cleanup(self, path: str) -> None:
        """Remove *path* on the host (``rm -rf``); best-effort, logs on failure."""
        try:
            result = self._run_remote(["rm", "-rf", path], cwd="/", timeout_s=60)
        except Exception:
            _log.warning("remote cleanup of %r failed before completion", path, exc_info=True)
            return
        if result.returncode != 0:
            _log.warning("remote cleanup of %r failed (exit %d)", path, result.returncode)
