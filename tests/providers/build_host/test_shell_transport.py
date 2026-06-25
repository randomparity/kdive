"""Unit tests for the ShellBuildTransport base (ADR-0100).

The base implements the BuildTransport surface in terms of an abstract ``_run_remote``. A
tiny recording subclass drives it with no real host, so the shared
read/clone/upload/cleanup behavior is pinned independently of ssh or guest-exec.
"""

from __future__ import annotations

import base64

import pytest

from kdive.artifacts.storage import PresignedUpload
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.build_transport import CommandResult
from kdive.providers.shared.build_host.transports.shell_transport import (
    _CLONE_TIMEOUT_S,
    _MAX_REMOTE_READ_B64_BYTES,
    _UPLOAD_TIMEOUT_S,
    ShellBuildTransport,
    _extract_etag_from_headers,
    _is_command_not_found,
    _validate_url,
)
from kdive.security.secrets.secret_registry import SecretRegistry


def test_validate_url_still_rejects_control_char_after_relocation() -> None:
    # _validate_url reads _UNSAFE_CHARS now imported from git_source (ADR-0162 relocation).
    with pytest.raises(CategorizedError) as exc:
        _validate_url("https://example.com/x\n")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "presigned URL contains a control character"


def test_validate_url_accepts_a_clean_url() -> None:
    _validate_url("https://example.com/object?sig=abc")


def test_is_command_not_found_true_on_exit_127_regardless_of_stderr() -> None:
    # rc 127 is the canonical signal even when stderr does not name git.
    assert _is_command_not_found(_ok(returncode=127, stderr="boom"), "boom") is True


def test_is_command_not_found_requires_git_named_with_a_not_found_token() -> None:
    # The stderr backstop fires only when *git* is named together with a not-found token.
    assert _is_command_not_found(_ok(returncode=1), "git: not found") is True
    assert _is_command_not_found(_ok(returncode=1), "git: no such file") is True
    # git named but no not-found token, or a not-found token without git — both must be False.
    assert _is_command_not_found(_ok(returncode=1), "git: permission denied") is False
    assert _is_command_not_found(_ok(returncode=1), "cc: not found") is False
    assert _is_command_not_found(_ok(returncode=1), "cc: no such file") is False


def test_extract_etag_preserves_a_value_containing_a_colon() -> None:
    # The header value is split on the FIRST colon only, so a colon inside the value survives.
    assert _extract_etag_from_headers('ETag: "a:b:c"') == '"a:b:c"'
    assert _extract_etag_from_headers("Content-Type: text/plain") == ""


class _RecordingTransport(ShellBuildTransport):
    """A ShellBuildTransport whose ``_run_remote`` records calls and returns canned results."""

    def __init__(self, results: list[CommandResult | Exception] | None = None) -> None:
        self._secret_registry = SecretRegistry()
        self.calls: list[tuple[list[str], str, int]] = []
        self._results = results or []

    def _run_remote(self, argv: list[str], *, cwd: str, timeout_s: int) -> CommandResult:
        self.calls.append((argv, cwd, timeout_s))
        if self._results:
            result = self._results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return CommandResult(returncode=0, stdout="", stderr="")

    def write_bytes(self, path: str, data: bytes) -> None:  # pragma: no cover - not under test
        raise NotImplementedError


def _ok(stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def test_read_bytes_issues_base64_and_decodes() -> None:
    payload = b"\x00\x01\x02\xff data"
    t = _RecordingTransport([_ok(stdout=base64.b64encode(payload).decode())])
    assert t.read_bytes("/x.bin") == payload
    argv, cwd, timeout_s = t.calls[0]
    assert argv == ["base64", "-w0", "/x.bin"]
    assert cwd == "/"
    assert timeout_s == 30


def test_read_text_decodes_utf8_from_the_requested_path() -> None:
    text = "# café CONFIG_CRASH_DUMP=y\n"
    t = _RecordingTransport([_ok(stdout=base64.b64encode(text.encode()).decode())])
    assert t.read_text("/.config") == text
    # read_text must forward the exact path through to read_bytes' base64 call.
    assert t.calls[0][0] == ["base64", "-w0", "/.config"]


def test_read_text_invalid_utf8_is_configuration_error() -> None:
    t = _RecordingTransport([_ok(stdout=base64.b64encode(b"\x80\x81").decode())])
    with pytest.raises(CategorizedError) as exc:
        t.read_text("/.config")
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "remote file '/.config' is not valid UTF-8"
    assert exc.value.details == {"path": "/.config"}


def test_read_bytes_oversize_is_configuration_error() -> None:
    t = _RecordingTransport([_ok(stdout="A" * (_MAX_REMOTE_READ_B64_BYTES + 4))])
    with pytest.raises(CategorizedError) as exc:
        t.read_bytes("/huge.bin")
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "remote file '/huge.bin' exceeds the maximum readable size"
    assert exc.value.details == {
        "path": "/huge.bin",
        "max_b64_bytes": _MAX_REMOTE_READ_B64_BYTES,
    }


def test_read_bytes_accepts_output_exactly_at_the_size_cap() -> None:
    # The cap is exclusive (> not >=): output of exactly _MAX bytes is still decoded.
    payload = b"\x00" * 4
    encoded = base64.b64encode(payload).decode()
    encoded = encoded + "A" * (_MAX_REMOTE_READ_B64_BYTES - len(encoded))
    # Pad to exactly the cap with valid base64 ('A' is valid); decode must not raise oversize.
    t = _RecordingTransport([_ok(stdout="A" * _MAX_REMOTE_READ_B64_BYTES)])
    assert isinstance(t.read_bytes("/at-cap.bin"), bytes)


def test_read_bytes_non_zero_is_infrastructure_failure() -> None:
    t = _RecordingTransport([_ok(returncode=1, stderr="No such file")])
    with pytest.raises(CategorizedError) as exc:
        t.read_bytes("/missing")
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(exc.value) == "remote read_bytes failed for '/missing'"
    assert exc.value.details["path"] == "/missing"
    assert "No such file" in str(exc.value.details["stderr"])


def test_read_bytes_malformed_base64_is_infrastructure_failure() -> None:
    t = _RecordingTransport([_ok(stdout="not-base64!")])
    with pytest.raises(CategorizedError) as exc:
        t.read_bytes("/corrupt")
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(exc.value) == "remote read_bytes returned malformed base64"
    assert exc.value.details == {"path": "/corrupt"}


def test_clone_issues_init_fetch_verify_checkout_in_order() -> None:
    # init, fetch, verify, checkout, then the final rev-parse HEAD provenance read.
    t = _RecordingTransport([_ok(), _ok(), _ok(stdout="deadbeef\n"), _ok(), _ok(stdout="c0ffee\n")])
    t.clone("https://git.example/linux.git", "v6.9", "/src")
    argvs = [c[0] for c in t.calls]
    assert argvs[0] == ["git", "init", "/src"]
    assert argvs[1] == [
        "git",
        "-C",
        "/src",
        "fetch",
        "--depth",
        "1",
        "https://git.example/linux.git",
        "v6.9",
    ]
    assert argvs[2] == ["git", "-C", "/src", "rev-parse", "--verify", "--quiet", "FETCH_HEAD"]
    assert argvs[3] == ["git", "-C", "/src", "checkout", "FETCH_HEAD"]
    assert argvs[4] == ["git", "-C", "/src", "rev-parse", "HEAD"]
    # Every clone step runs from / with the clone timeout budget.
    assert all(cwd == "/" for _, cwd, _ in t.calls)
    assert all(timeout_s == _CLONE_TIMEOUT_S for *_, timeout_s in t.calls)


def test_clone_returns_the_resolved_head_commit() -> None:
    # After checkout, clone resolves and returns `git rev-parse HEAD` (stripped) as the SHA.
    sha = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"  # pragma: allowlist secret
    t = _RecordingTransport([_ok(), _ok(), _ok(stdout="deadbeef\n"), _ok(), _ok(stdout=sha + "\n")])
    assert t.clone("https://git.example/linux.git", "v6.9", "/src") == sha


def test_clone_rev_parse_head_failure_is_transport_failure() -> None:
    # A failed final rev-parse HEAD surfaces as TRANSPORT_FAILURE with redacted stderr.
    t = _RecordingTransport(
        [_ok(), _ok(), _ok(stdout="deadbeef\n"), _ok(), _ok(returncode=1, stderr="HEAD boom")]
    )
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "v6.9", "/src")
    assert exc.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert str(exc.value) == "git rev-parse HEAD failed on remote"
    assert "HEAD boom" in str(exc.value.details["stderr"])


def test_clone_init_non_zero_is_infrastructure_failure() -> None:
    t = _RecordingTransport([_ok(returncode=1, stderr="permission denied")])
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "v6.9", "/src")
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(exc.value) == "git init failed on remote"
    assert "permission denied" in str(exc.value.details["stderr"])


def test_clone_rejects_an_unsafe_remote_naming_the_remote_arg() -> None:
    # The "remote"/"ref" label passed to validate_git_arg surfaces in the rejection message.
    t = _RecordingTransport()
    with pytest.raises(CategorizedError) as exc:
        t.clone("-bad", "v6.9", "/src")
    assert "remote" in str(exc.value)
    t = _RecordingTransport()
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "-bad", "/src")
    assert "ref" in str(exc.value)


def test_clone_init_command_not_found_is_missing_dependency_with_diagnostic() -> None:
    # rc 127 is the canonical "git: not found" shape — the base image lacks the build toolchain.
    t = _RecordingTransport([_ok(returncode=127, stderr="sh: git: not found")])
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "v6.9", "/src")
    assert exc.value.category == ErrorCategory.MISSING_DEPENDENCY
    assert str(exc.value) == (
        "the build host's base image is missing the kernel build toolchain (git)"
    )
    assert exc.value.details["diagnostic"] == "ops.diagnostics --with-buildhost-agent"
    assert "git" in str(exc.value.details["stderr"])


def test_clone_init_not_found_stderr_without_127_is_missing_dependency() -> None:
    # Backstop: a transport that does not surface rc 127 but whose stderr names a missing git.
    t = _RecordingTransport([_ok(returncode=1, stderr="sh: 1: git: not found")])
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "v6.9", "/src")
    assert exc.value.category == ErrorCategory.MISSING_DEPENDENCY
    assert exc.value.details["diagnostic"] == "ops.diagnostics --with-buildhost-agent"


def test_clone_fetch_non_zero_is_configuration_error_with_fetch_stderr() -> None:
    # init ok, fetch fails — the regression for the masked-cause bug (checkout never runs).
    t = _RecordingTransport([_ok(), _ok(returncode=128, stderr="Could not resolve host")])
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "v6.9", "/src")
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "git fetch failed on remote"
    assert "Could not resolve host" in str(exc.value.details["stderr"])
    # Only init + fetch ran; checkout was never reached.
    assert [c[0][:2] for c in t.calls] == [["git", "init"], ["git", "-C"]]


def test_clone_checkout_non_zero_is_configuration_error() -> None:
    # init ok, fetch ok, FETCH_HEAD resolves, but the checkout itself fails.
    t = _RecordingTransport(
        [_ok(), _ok(), _ok(stdout="deadbeef\n"), _ok(returncode=1, stderr="checkout boom")]
    )
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "v6.9", "/src")
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "git checkout FETCH_HEAD failed on remote"
    assert "checkout boom" in str(exc.value.details["stderr"])


def test_clone_masked_fetch_without_fetch_head_surfaces_fetch_stderr() -> None:
    # The masked-cause regression: fetch's rc is masked to 0 (companion guest-agent bug),
    # but it produced no FETCH_HEAD. The error must carry the fetch's own stderr and be a
    # transport failure, NOT a downstream checkout pathspec message.
    fetch_stderr = "fatal: unable to access 'https://git.example/': Could not resolve host"
    t = _RecordingTransport(
        [
            _ok(),  # init
            _ok(returncode=0, stderr=fetch_stderr),  # fetch masked to rc 0
            _ok(returncode=1, stderr=""),  # rev-parse --verify FETCH_HEAD fails (no FETCH_HEAD)
        ]
    )
    with pytest.raises(CategorizedError) as exc:
        t.clone("https://git.example/linux.git", "v6.9", "/src")
    assert exc.value.category == ErrorCategory.TRANSPORT_FAILURE
    assert str(exc.value) == (
        "git fetch produced no FETCH_HEAD on remote (the fetch did not complete)"
    )
    assert "Could not resolve host" in str(exc.value.details["stderr"])
    assert "pathspec" not in str(exc.value.details["stderr"])
    # init + fetch + rev-parse ran; checkout was never reached.
    assert [c[0][:2] for c in t.calls] == [["git", "init"], ["git", "-C"], ["git", "-C"]]
    assert t.calls[2][0] == ["git", "-C", "/src", "rev-parse", "--verify", "--quiet", "FETCH_HEAD"]


@pytest.mark.parametrize(
    "remote,ref",
    [("-bad", "v6.9"), ("https://x/linux.git", "-v"), ("https://x/linux\n.git", "v6.9")],
)
def test_clone_rejects_unsafe_args_before_any_run(remote: str, ref: str) -> None:
    t = _RecordingTransport()
    with pytest.raises(CategorizedError) as exc:
        t.clone(remote, ref, "/src")
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert t.calls == []  # validated before any host command


def test_upload_file_builds_curl_and_parses_etag() -> None:
    presigned = PresignedUpload(
        url="https://s3.example/put",
        required_headers={"x-amz-checksum-sha256": "abc123"},
    )
    _EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # pragma: allowlist secret
    headers = f'HTTP/1.1 200 OK\r\nETag: "{_EMPTY_MD5}"\r\n\r\n'
    t = _RecordingTransport([_ok(stdout=headers)])
    etag = t.upload_file("/build/bzImage", presigned)
    assert etag == _EMPTY_MD5
    argv, cwd, timeout_s = t.calls[0]
    # The exact curl invocation matters: PUT, fail-on-error, header dump to stdout, body discarded.
    assert argv == [
        "curl",
        "-fsS",
        "-X",
        "PUT",
        "--upload-file",
        "/build/bzImage",
        "-H",
        "x-amz-checksum-sha256: abc123",
        "-D",
        "-",
        "-o",
        "/dev/null",
        presigned.url,
    ]
    assert cwd == "/"
    assert timeout_s == _UPLOAD_TIMEOUT_S


def test_upload_file_strips_quotes_only_from_the_etag() -> None:
    presigned = PresignedUpload(url="https://s3.example/put", required_headers={})
    t = _RecordingTransport([_ok(stdout='HTTP/1.1 200 OK\r\nETag: "v:1:2"\r\n\r\n')])
    # The ETag value contains colons; the parser splits on the first colon and strips quotes.
    assert t.upload_file("/build/bzImage", presigned) == "v:1:2"


def test_upload_file_non_zero_is_infrastructure_failure() -> None:
    t = _RecordingTransport([_ok(returncode=22)])
    with pytest.raises(CategorizedError) as exc:
        t.upload_file("/build/bzImage", PresignedUpload(url="https://s3/p", required_headers={}))
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(exc.value) == "remote curl PUT failed"
    assert exc.value.details == {"url": "https://s3/p"}


def test_upload_file_missing_etag_is_infrastructure_failure() -> None:
    t = _RecordingTransport([_ok(stdout="HTTP/1.1 200 OK\r\n\r\n")])
    with pytest.raises(CategorizedError) as exc:
        t.upload_file(
            "/build/bzImage",
            PresignedUpload(url="https://s3.example/put?sig=secret", required_headers={}),
        )
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(exc.value) == "remote curl PUT response did not include an ETag"
    assert exc.value.details == {"url": "https://s3.example/put?sig=secret"}


def test_cleanup_issues_rm_rf() -> None:
    t = _RecordingTransport([_ok()])
    t.cleanup("/build/scratch")
    assert t.calls[0] == (["rm", "-rf", "/build/scratch"], "/", 60)


def test_cleanup_suppresses_non_zero_rm() -> None:
    t = _RecordingTransport([_ok(returncode=1, stderr="permission denied")])

    t.cleanup("/build/scratch")

    assert t.calls[0][0] == ["rm", "-rf", "/build/scratch"]


def test_cleanup_suppresses_transport_error() -> None:
    t = _RecordingTransport(
        [
            CategorizedError(
                "remote command failed",
                category=ErrorCategory.TRANSPORT_FAILURE,
            )
        ]
    )

    t.cleanup("/build/scratch")

    assert t.calls[0][0] == ["rm", "-rf", "/build/scratch"]


def test_run_delegates_to_run_remote_with_cwd_and_timeout() -> None:
    t = _RecordingTransport([_ok(stdout="hi")])
    result = t.run(["make", "-j4"], cwd="/ws", timeout_s=99)
    assert result.stdout == "hi"
    assert t.calls[0] == (["make", "-j4"], "/ws", 99)
