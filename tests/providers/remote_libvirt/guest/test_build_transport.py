"""Unit tests for GuestExecBuildTransport (ADR-0100).

A fake ``agent_command`` drives the two-phase guest-exec/guest-exec-status protocol with no
libvirt host. The transport composes one ``/bin/sh -c "cd <cwd> && exec <argv>"`` hop per
command (the sibling SSH posture), reuses the ShellBuildTransport base for read/clone/upload,
and registers the presigned URL for redaction before an in-guest curl.
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

import libvirt
import pytest

from kdive.artifacts.storage import PresignedUpload
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.guest.build_transport import GuestExecBuildTransport
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import libvirt_error
from tests.providers.remote_libvirt.fakes import FakeDomain


class _FakeAgent:
    """Two-phase guest-agent fake: records spawned argvs; returns canned exit/out/err.

    ``never_exits`` makes guest-exec-status always report not-exited (drives the timeout path).
    """

    def __init__(
        self,
        *,
        exitcode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        never_exits: bool = False,
    ) -> None:
        self.spawned: list[dict[str, Any]] = []
        self.domains: list[Any] = []
        self._exitcode = exitcode
        self._stdout = stdout
        self._stderr = stderr
        self._never_exits = never_exits

    def __call__(self, domain: Any, command: str, timeout: int, flags: int) -> str:
        msg = json.loads(command)
        if msg["execute"] == "guest-exec":
            self.spawned.append(msg["arguments"])
            self.domains.append(domain)
            return json.dumps({"return": {"pid": 4321}})
        # guest-exec-status
        if self._never_exits:
            return json.dumps({"return": {"exited": False}})
        return json.dumps(
            {
                "return": {
                    "exited": True,
                    "exitcode": self._exitcode,
                    "out-data": base64.b64encode(self._stdout).decode(),
                    "err-data": base64.b64encode(self._stderr).decode(),
                }
            }
        )


def _transport(
    agent: _FakeAgent, *, registry: SecretRegistry | None = None
) -> GuestExecBuildTransport:
    return GuestExecBuildTransport(
        domain=FakeDomain("build-vm"),
        agent_command=agent,
        secret_registry=registry or SecretRegistry(),
        poll_s=0.0,
        sleep=lambda _s: None,
        monotonic=_clock(),
    )


def _clock() -> Any:
    """A monotonic that advances 1s per call (so a never-exits poll loop hits its deadline)."""
    ticks = iter(range(0, 100000))
    return lambda: float(next(ticks))


def test_run_composes_single_sh_c_hop() -> None:
    agent = _FakeAgent(exitcode=0, stdout=b"ok")
    domain = FakeDomain("build-vm")
    transport = GuestExecBuildTransport(
        domain=domain,
        agent_command=agent,
        secret_registry=SecretRegistry(),
        poll_s=0.0,
        sleep=lambda _s: None,
        monotonic=_clock(),
    )
    result = transport.run(["make", "-C", "/ws", "x"], cwd="/ws", timeout_s=60)
    assert result.returncode == 0
    assert result.stdout == "ok"
    args = agent.spawned[0]
    assert args["path"] == "/bin/sh"
    assert args["arg"] == ["-c", "cd /ws && exec make -C /ws x"]
    # The command runs against the transport's own domain handle, not some other.
    assert agent.domains == [domain]


def test_run_decodes_non_utf8_output_with_replacement_not_raising() -> None:
    # guest stdout/stderr are decoded with errors="replace": a build tool can emit non-UTF-8
    # bytes (a localized libc message), and the transport must surface a lossy string rather
    # than crash the whole build on a decode error.
    bad = b"warn: \xff\xfe done"
    agent = _FakeAgent(exitcode=0, stdout=bad, stderr=bad)
    result = _transport(agent).run(["make"], cwd="/ws", timeout_s=10)
    assert result.stdout == bad.decode("utf-8", "replace")
    assert result.stderr == bad.decode("utf-8", "replace")
    assert "�" in result.stdout  # the invalid bytes became the replacement char


def test_run_quotes_cwd_and_argv() -> None:
    agent = _FakeAgent()
    _transport(agent).run(["echo", "a b"], cwd="/has space", timeout_s=10)
    assert agent.spawned[0]["arg"] == ["-c", "cd '/has space' && exec echo 'a b'"]


def test_run_non_zero_exit_is_returned_not_raised() -> None:
    agent = _FakeAgent(exitcode=2, stderr=b"boom")
    result = _transport(agent).run(["make", "-C", "/ws"], cwd="/ws", timeout_s=10)
    assert result.returncode == 2
    assert result.stderr == "boom"


def test_run_timeout_maps_to_transport_failure() -> None:
    agent = _FakeAgent(never_exits=True)
    with pytest.raises(CategorizedError) as exc:
        _transport(agent).run(["make"], cwd="/ws", timeout_s=3)
    assert exc.value.category == ErrorCategory.TRANSPORT_FAILURE


def _raising_agent(exc: BaseException) -> Any:
    def _agent(domain: Any, command: str, timeout: int, flags: int) -> str:
        raise exc

    return _agent


def test_post_readiness_code_86_maps_to_non_retryable_configuration_error() -> None:
    # The build session runs the guest-ping readiness gate before binding this transport, so a
    # subsequent AGENT_UNRESPONSIVE (code 86) is a deterministic dead agent for the build path —
    # the transport classifies it CONFIGURATION_ERROR (retryable=false), not transport_failure.
    agent = _raising_agent(libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE))
    with pytest.raises(CategorizedError) as exc:
        _transport(agent).run(["make"], cwd="/ws", timeout_s=10)
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["libvirt_error_code"] == libvirt.VIR_ERR_AGENT_UNRESPONSIVE


def test_transient_non_86_error_stays_transport_failure() -> None:
    # A non-deterministic libvirt error on the build transport still maps to retryable transport.
    agent = _raising_agent(libvirt_error(libvirt.VIR_ERR_OPERATION_FAILED))
    with pytest.raises(CategorizedError) as exc:
        _transport(agent).run(["make"], cwd="/ws", timeout_s=10)
    assert exc.value.category == ErrorCategory.TRANSPORT_FAILURE


def test_read_bytes_round_trips_via_base64() -> None:
    payload = b"\x00\x01config\xff"
    agent = _FakeAgent(stdout=base64.b64encode(payload))
    # read_bytes runs `base64 -w0 <path>`; the fake returns its stdout as the b64 of payload.
    result = _transport(agent).read_bytes("/build/.config")
    assert result == payload
    assert agent.spawned[0]["arg"] == ["-c", "cd / && exec base64 -w0 /build/.config"]


def test_write_bytes_composes_pipeline_not_exec() -> None:
    data = b"\x00config\xff"
    agent = _FakeAgent(exitcode=0)
    _transport(agent).write_bytes("/build/dest.bin", data)
    encoded = base64.b64encode(data).decode()
    arg = agent.spawned[0]["arg"]
    assert arg[0] == "-c"
    # The pipeline must NOT be wrapped in the exec-join form (a pipe cannot be exec'd).
    assert arg[1] == f"printf %s {encoded} | base64 -d > /build/dest.bin"
    assert "exec" not in arg[1]


def test_write_bytes_non_zero_is_infrastructure_failure() -> None:
    agent = _FakeAgent(exitcode=1, stderr=b"No space left")
    with pytest.raises(CategorizedError) as exc:
        _transport(agent).write_bytes("/build/dest.bin", b"x")
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE
    # The error names the failed path in both the message and the structured details.
    assert "/build/dest.bin" in str(exc.value)
    assert exc.value.details["path"] == "/build/dest.bin"
    assert exc.value.details["stderr"] == "No space left"


def test_write_bytes_failure_redacts_registered_secret_in_stderr() -> None:
    # write_bytes runs stderr through redacted_tail with the transport's registry; a registered
    # secret appearing in the in-guest stderr must be masked in the error detail.
    registry = SecretRegistry()
    registry.register("tok-supersecret", scope=None)  # pragma: allowlist secret
    agent = _FakeAgent(exitcode=1, stderr=b"auth failed for tok-supersecret")
    with pytest.raises(CategorizedError) as exc:
        _transport(agent, registry=registry).write_bytes("/build/dest.bin", b"x")
    assert "tok-supersecret" not in str(exc.value.details)
    assert "[REDACTED]" in cast(str, exc.value.details["stderr"])


def test_clone_runs_init_fetch_verify_checkout_via_agent() -> None:
    agent = _FakeAgent(exitcode=0, stdout=b"deadbeef\n")
    _transport(agent).clone("https://git.example/linux.git", "v6.9", "/src")
    cmds = [s["arg"][1] for s in agent.spawned]
    assert cmds[0] == "cd / && exec git init /src"
    assert "fetch --depth 1 https://git.example/linux.git v6.9" in cmds[1]
    assert cmds[2] == "cd / && exec git -C /src rev-parse --verify --quiet FETCH_HEAD"
    assert cmds[3] == "cd / && exec git -C /src checkout FETCH_HEAD"


def test_upload_file_registers_url_before_exec_and_redacts_on_failure() -> None:
    registry = SecretRegistry()
    presigned = PresignedUpload(
        url="https://s3.example/put?X-Amz-Signature=secretsig",
        required_headers={"x-amz-checksum-sha256": "abc"},
    )
    agent = _FakeAgent(exitcode=22)  # curl failure
    with pytest.raises(CategorizedError) as exc:
        _transport(agent, registry=registry).upload_file("/build/bzImage", presigned)
    assert exc.value.category == ErrorCategory.INFRASTRUCTURE_FAILURE
    # The URL was registered for redaction (so any transcript masks it).
    assert presigned.url in registry.snapshot()
    # The error detail carries only the query-stripped URL, never the live signature.
    assert "secretsig" not in str(exc.value.details)


def test_upload_file_success_parses_etag() -> None:
    _EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"  # pragma: allowlist secret
    headers = f'HTTP/1.1 200 OK\r\nETag: "{_EMPTY_MD5}"\r\n\r\n'
    agent = _FakeAgent(exitcode=0, stdout=headers.encode())
    etag = _transport(agent).upload_file(
        "/build/bzImage", PresignedUpload(url="https://s3/p", required_headers={})
    )
    assert etag == _EMPTY_MD5
    # The local file path is forwarded into the in-guest curl upload command.
    assert "--upload-file /build/bzImage" in agent.spawned[0]["arg"][1]
