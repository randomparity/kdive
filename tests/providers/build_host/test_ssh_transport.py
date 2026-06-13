"""Unit tests for SshBuildTransport + materialized_ssh_identity (Task 6 — ADR-0342).

No real SSH or network connections are made. subprocess.run is monkeypatched at the
module-level target so argv capture is deterministic.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.provider_components.artifacts import PresignedUpload
from kdive.providers.build_host.ssh_transport import SshBuildTransport, materialized_ssh_identity
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN_TARGET = "kdive.providers.build_host.ssh_transport.subprocess.run"

_FAKE_IDENTITY = Path("/tmp/fake-identity.pem")  # noqa: S108 — test constant only
_FAKE_ADDRESS = "builder@10.0.0.1"


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# ---------------------------------------------------------------------------
# 1. run — ssh wrapper argv shape
# ---------------------------------------------------------------------------


def test_run_builds_ssh_wrapper_argv() -> None:
    """run() wraps argv in an ssh invocation with BatchMode, ConnectTimeout, and identity."""
    argv = ["make", "-j4"]
    cwd = "/build/workspace"
    remote_cmd = f"cd {shlex.quote(cwd)} && {shlex.join(argv)}"
    expected_ssh_argv = [
        "ssh",
        "-i",
        str(_FAKE_IDENTITY),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        _FAKE_ADDRESS,
        remote_cmd,
    ]

    with patch(_RUN_TARGET, return_value=_completed()) as mock_run:
        transport = SshBuildTransport(
            address=_FAKE_ADDRESS,
            identity_path=_FAKE_IDENTITY,
            secret_registry=SecretRegistry(),
        )
        result = transport.run(argv, cwd=cwd, timeout_s=120)

    mock_run.assert_called_once_with(
        expected_ssh_argv,
        timeout=120,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# 2. clone — issues init → fetch --depth 1 → checkout FETCH_HEAD in order;
#    non-zero checkout raises CONFIGURATION_ERROR with redacted stderr
# ---------------------------------------------------------------------------


def test_clone_issues_commands_in_order() -> None:
    """clone() runs git init, git fetch --depth 1, git checkout FETCH_HEAD in order."""
    remote = "https://git.kernel.org/pub/scm/linux.git"
    ref = "v6.9"
    dest = "/build/src"

    # All three sub-commands succeed (returncode 0).
    def side_effect(argv: list[str], **kwargs: object) -> MagicMock:
        return _completed(returncode=0)

    with patch(_RUN_TARGET, side_effect=side_effect) as mock_run:
        transport = SshBuildTransport(
            address=_FAKE_ADDRESS,
            identity_path=_FAKE_IDENTITY,
            secret_registry=SecretRegistry(),
        )
        transport.clone(remote, ref, dest)

    assert mock_run.call_count == 3
    calls = mock_run.call_args_list

    # Extract the remote command (last positional element of the ssh argv).
    def remote_cmd(c: Any) -> str:
        ssh_argv: list[str] = c.args[0]
        return ssh_argv[-1]

    assert "git init" in remote_cmd(calls[0])
    assert "fetch" in remote_cmd(calls[1])
    assert "--depth" in remote_cmd(calls[1])
    assert remote in remote_cmd(calls[1])
    assert ref in remote_cmd(calls[1])
    assert "checkout" in remote_cmd(calls[2])
    assert "FETCH_HEAD" in remote_cmd(calls[2])


def test_clone_non_zero_checkout_raises_configuration_error() -> None:
    """clone() raises CONFIGURATION_ERROR when checkout FETCH_HEAD exits non-zero."""
    call_count = 0

    def side_effect(argv: list[str], **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        # init and fetch succeed; checkout fails.
        if call_count == 3:
            return _completed(returncode=1, stderr="error: pathspec 'FETCH_HEAD' did not match")
        return _completed(returncode=0)

    with patch(_RUN_TARGET, side_effect=side_effect):
        transport = SshBuildTransport(
            address=_FAKE_ADDRESS,
            identity_path=_FAKE_IDENTITY,
            secret_registry=SecretRegistry(),
        )
        with pytest.raises(CategorizedError) as exc_info:
            transport.clone("https://git.example.com/linux.git", "v6.9", "/build/src")

    assert exc_info.value.category == ErrorCategory.CONFIGURATION_ERROR


# ---------------------------------------------------------------------------
# 3. Input validation — control chars / leading dash rejected before subprocess
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "remote,ref",
    [
        # Leading dash on remote (looks like an ssh/git option)
        ("-bad-remote", "v6.9"),
        # Leading dash on ref
        ("https://example.com/linux.git", "-v"),
        # Newline in remote
        ("https://example.com/linux\ngit", "v6.9"),
        # Newline in ref
        ("https://example.com/linux.git", "v6.9\nrm -rf"),
        # Control character in remote
        ("https://example.com/linux\x00.git", "v6.9"),
        # Control character in ref
        ("https://example.com/linux.git", "v6.9\x01"),
    ],
)
def test_clone_rejects_invalid_remote_or_ref_before_subprocess(remote: str, ref: str) -> None:
    """clone() raises CONFIGURATION_ERROR for unsafe remote/ref without any subprocess call."""
    with patch(_RUN_TARGET) as mock_run:
        transport = SshBuildTransport(
            address=_FAKE_ADDRESS,
            identity_path=_FAKE_IDENTITY,
            secret_registry=SecretRegistry(),
        )
        with pytest.raises(CategorizedError) as exc_info:
            transport.clone(remote, ref, "/build/src")

    mock_run.assert_not_called()
    assert exc_info.value.category == ErrorCategory.CONFIGURATION_ERROR


# ---------------------------------------------------------------------------
# 4. upload_file — host-side curl PUT with required headers; etag returned
# ---------------------------------------------------------------------------


def test_upload_file_runs_host_side_curl_put_and_returns_etag() -> None:
    """upload_file sends a curl PUT via run() with all required headers; returns etag."""
    presigned = PresignedUpload(
        url="https://s3.example.com/put",
        required_headers={
            "x-amz-checksum-sha256": "abc123",
            "Content-Type": "application/octet-stream",
        },
    )
    remote_path = "/build/workspace/bzImage"
    # Simulate curl -D - outputting HTTP response headers to stdout (body goes to /dev/null).
    # The ETag value is a well-known MD5 of empty string — not a real secret.
    _EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # pragma: allowlist secret
    fake_etag_output = f'HTTP/1.1 200 OK\r\nETag: "{_EMPTY_MD5}"\r\nContent-Length: 0\r\n\r\n'

    with patch(_RUN_TARGET, return_value=_completed(stdout=fake_etag_output)) as mock_run:
        transport = SshBuildTransport(
            address=_FAKE_ADDRESS,
            identity_path=_FAKE_IDENTITY,
            secret_registry=SecretRegistry(),
        )
        etag = transport.upload_file(remote_path, presigned)

    assert mock_run.called
    # The ssh remote_cmd should contain curl and the PUT url.
    ssh_argv: list[str] = mock_run.call_args.args[0]
    remote_cmd = ssh_argv[-1]
    assert "curl" in remote_cmd
    assert presigned.url in remote_cmd
    assert "--upload-file" in remote_cmd
    # Etag returned with quotes stripped (MD5 of empty string — not a real secret).
    assert etag == "d41d8cd98f00b204e9800998ecf8427e"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# 5. materialized_ssh_identity — writes 0600 file, registers secret, yields path,
#    unlinks on exit; also unlinks when the body raises
# ---------------------------------------------------------------------------


_FAKE_PEM_KEY = "-----BEGIN RSA PRIVATE KEY-----\nfakekey\n-----END RSA PRIVATE KEY-----"  # noqa: E501  # pragma: allowlist secret


def test_materialized_ssh_identity_lifecycle(tmp_path: Path) -> None:
    """materialized_ssh_identity writes a 0600 file, registers the value, yields path, unlinks."""
    key_value = _FAKE_PEM_KEY
    registry = SecretRegistry()
    sentinel = object()

    fake_backend = MagicMock()
    fake_backend.resolve.return_value = key_value

    written_path: Path | None = None

    with (
        patch(
            "kdive.providers.build_host.ssh_transport._resolve_ssh_key",
            return_value=key_value,
        ),
        materialized_ssh_identity("ssh_key.pem", registry, scope=sentinel) as identity_path,
    ):
        written_path = identity_path
        assert identity_path.exists()
        # File mode must be 0600.
        assert oct(identity_path.stat().st_mode & 0o777) == oct(0o600)
        # Secret must have been registered.
        assert key_value in registry.snapshot()

    # After the context manager exits, the file is unlinked.
    assert written_path is not None
    assert not written_path.exists()


def test_materialized_ssh_identity_unlinks_on_body_exception(tmp_path: Path) -> None:
    """materialized_ssh_identity unlinks the identity file even when the body raises."""
    key_value = _FAKE_PEM_KEY

    written_path: Path | None = None

    with (
        patch(
            "kdive.providers.build_host.ssh_transport._resolve_ssh_key",
            return_value=key_value,
        ),
        pytest.raises(RuntimeError, match="intentional"),
        materialized_ssh_identity("ssh_key.pem", SecretRegistry()) as identity_path,
    ):
        written_path = identity_path
        raise RuntimeError("intentional")

    assert written_path is not None
    assert not written_path.exists()
