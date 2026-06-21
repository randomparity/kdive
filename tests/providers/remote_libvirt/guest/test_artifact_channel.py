"""Unit tests for the in-target artifact channel (issue #202, ADR-0078).

The channel is the load-bearing in-target install/retrieve seam: it registers a minted
presigned URL (a bearer capability) in the redaction registry **before** the guest-agent
exec, runs a constrained in-guest command that carries the URL, redacts the captured
transcript by exact value, persists the redacted bytes, and releases the per-op scope
**only after** the persist. These tests prove that discipline with a fake guest-agent and
a fake store; no real host or MinIO.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.guest.agent import AgentExecResult
from kdive.providers.remote_libvirt.guest.artifact_channel import InTargetArtifactChannel
from kdive.security.secrets.redaction import REDACTION
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.fakes import FakeDomain

# A presigned URL with no secret-name substring (token/secret/password/api_key), so the
# only thing that can mask it in the transcript is the registry's exact-value masking —
# never the Redactor's independent key=value regex. This keeps the assertion a real test
# of the register->mask path, mirroring the ADR-0073/0075 fault-inject loops.
_CAPABILITY_URL = (
    "https://store.example/t/runs/r1/kernel?X-Amz-Signature=deadbeefcafe&X-Amz-Expires=600"
)
_OTHER_CAPABILITY_URL = (
    "https://store.example/t/runs/r2/kernel?X-Amz-Signature=beadfeedcafe&X-Amz-Expires=600"
)


class _FakeStore:
    """Records ``put_artifact`` calls; the persisted bytes are inspected by the tests."""

    def __init__(self) -> None:
        self.writes: list[ArtifactWriteRequest] = []

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.writes.append(request)
        return StoredArtifact(request.key(), "etag-1", request.sensitivity, request.retention_class)


class _RecordingExec:
    """A guest-agent exec double that snapshots the registry at run time."""

    def __init__(self, registry: SecretRegistry, result: AgentExecResult) -> None:
        self._registry = registry
        self._result = result
        self.seen_registered: frozenset[str] | None = None
        self.argv: list[str] | None = None
        self.domain: object | None = None

    def run(self, domain: object, argv: list[str]) -> AgentExecResult:
        self.seen_registered = self._registry.snapshot()
        self.argv = argv
        self.domain = domain
        return self._result


class _FailingExec:
    """A guest-agent exec double that raises, to prove scope release on failure."""

    def run(self, domain: object, argv: list[str]) -> AgentExecResult:
        raise CategorizedError("guest agent unreachable", category=ErrorCategory.TRANSPORT_FAILURE)


class _NestedExec:
    """Reuses the same channel during run() to exercise independent per-call scopes."""

    def __init__(self, channel_ref: list[InTargetArtifactChannel]) -> None:
        self._channel_ref = channel_ref
        self._nested = False

    def run(self, domain: object, argv: list[str]) -> AgentExecResult:
        if not self._nested:
            self._nested = True
            _run(self._channel_ref[0], capability_url=_OTHER_CAPABILITY_URL)
        return AgentExecResult(0, f"used {argv[-1]}".encode(), b"")


def _channel(registry: SecretRegistry, store: _FakeStore, agent_exec: Any, scope: object):
    return InTargetArtifactChannel(
        registry=registry,
        agent_exec=agent_exec,
        store_factory=lambda: store,
        scope=scope,
    )


def _run(
    channel: InTargetArtifactChannel,
    *,
    system_id=None,
    capability_url: str = _CAPABILITY_URL,
):
    return channel.exec_with_capability(
        FakeDomain("build-vm"),
        capability_url=capability_url,
        argv=["/usr/bin/curl", "-fsS", "-o", "/boot/vmlinuz", capability_url],
        owner_kind="systems",
        owner_id=str(system_id or uuid4()),
    )


def test_capability_url_is_registered_before_the_exec_runs() -> None:
    registry = SecretRegistry()
    store = _FakeStore()
    scope = object()
    agent = _RecordingExec(registry, AgentExecResult(0, b"ok", b""))
    _run(_channel(registry, store, agent, scope))
    assert agent.seen_registered is not None
    assert _CAPABILITY_URL in agent.seen_registered  # registered before run() was entered


def test_persisted_transcript_masks_the_capability_url() -> None:
    registry = SecretRegistry()
    store = _FakeStore()
    scope = object()
    # The guest command echoes the URL on stderr too (a curl progress/error line).
    agent = _RecordingExec(
        registry, AgentExecResult(0, b"saved", f"fetching {_CAPABILITY_URL}".encode())
    )
    output = _run(_channel(registry, store, agent, scope))
    assert len(store.writes) == 1
    persisted = store.writes[0].data.decode("utf-8")
    assert _CAPABILITY_URL not in persisted
    assert REDACTION in persisted
    assert store.writes[0].sensitivity is Sensitivity.REDACTED
    # The surfaced snippet is masked too.
    assert _CAPABILITY_URL not in output.transcript_snippet
    assert REDACTION in output.transcript_snippet


def test_exit_status_and_raw_stdout_are_surfaced_to_the_caller() -> None:
    registry = SecretRegistry()
    store = _FakeStore()
    agent = _RecordingExec(registry, AgentExecResult(7, b"install output", b""))
    output = _run(_channel(registry, store, agent, object()))
    assert output.result.exit_status == 7
    assert output.result.stdout == b"install output"


def test_domain_artifact_and_retention_class_are_threaded_through() -> None:
    registry = SecretRegistry()
    store = _FakeStore()
    domain = FakeDomain("build-vm")
    agent = _RecordingExec(registry, AgentExecResult(0, b"ok", b""))
    channel = _channel(registry, store, agent, object())
    output = channel.exec_with_capability(
        domain,
        capability_url=_CAPABILITY_URL,
        argv=["/usr/bin/curl", "-fsS", _CAPABILITY_URL],
        owner_kind="systems",
        owner_id="sys-1",
    )
    # The exec runs against the domain handle the caller passed, not a placeholder.
    assert agent.domain is domain
    # The returned artifact is the one the store persisted (not a dropped/None value).
    assert output.artifact is not None
    [write] = store.writes
    assert output.artifact.key == write.key()
    assert write.retention_class == "console"
    assert write.tenant == "remote-libvirt"
    assert write.name == "in-target-exec-redacted"


def test_transcript_renders_command_line_streams_and_exit_status() -> None:
    registry = SecretRegistry()
    store = _FakeStore()
    agent = _RecordingExec(registry, AgentExecResult(3, b"out-line", b"err-line"))
    channel = _channel(registry, store, agent, object())
    channel.exec_with_capability(
        FakeDomain("build-vm"),
        capability_url=_CAPABILITY_URL,
        argv=["/usr/bin/curl", "-fsS", _CAPABILITY_URL],
        owner_kind="systems",
        owner_id="sys-1",
    )
    transcript = store.writes[0].data.decode("utf-8")
    lines = transcript.split("\n")
    assert lines[0] == "$ /usr/bin/curl -fsS " + REDACTION
    assert lines[1] == "out-line"
    assert lines[2] == "err-line"
    assert lines[3] == "exit status: 3"


def test_transcript_replaces_invalid_utf8_in_streams() -> None:
    registry = SecretRegistry()
    store = _FakeStore()
    # 0xff is not valid UTF-8; errors="replace" must turn it into U+FFFD, never raise.
    agent = _RecordingExec(registry, AgentExecResult(0, b"head\xfftail", b"e\xff"))
    channel = _channel(registry, store, agent, object())
    output = channel.exec_with_capability(
        FakeDomain("build-vm"),
        capability_url=_CAPABILITY_URL,
        argv=["/usr/bin/curl", _CAPABILITY_URL],
        owner_kind="systems",
        owner_id="sys-1",
    )
    transcript = store.writes[0].data.decode("utf-8")
    assert "head�tail" in transcript
    assert "e�" in transcript
    assert output.result.stdout == b"head\xfftail"


def test_scope_released_after_persist() -> None:
    registry = SecretRegistry()
    store = _FakeStore()
    scope = object()
    agent = _RecordingExec(registry, AgentExecResult(0, b"ok", b""))
    _run(_channel(registry, store, agent, scope))
    assert _CAPABILITY_URL not in registry.snapshot()  # not left registered past the op


def test_scope_released_even_when_the_exec_fails() -> None:
    registry = SecretRegistry()
    store = _FakeStore()
    scope = object()
    channel = _channel(registry, store, _FailingExec(), scope)
    with pytest.raises(CategorizedError) as excinfo:
        _run(channel)
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert _CAPABILITY_URL not in registry.snapshot()  # released despite the failure
    assert store.writes == []  # nothing persisted when the exec never produced a transcript


def test_reusing_channel_does_not_release_another_call_scope() -> None:
    registry = SecretRegistry()
    store = _FakeStore()
    channel_ref: list[InTargetArtifactChannel] = []
    channel = _channel(registry, store, _NestedExec(channel_ref), object())
    channel_ref.append(channel)

    output = _run(channel)

    assert len(store.writes) == 2
    persisted = "\n".join(write.data.decode("utf-8") for write in store.writes)
    assert _CAPABILITY_URL not in persisted
    assert _OTHER_CAPABILITY_URL not in persisted
    assert _CAPABILITY_URL not in output.transcript_snippet
    assert _OTHER_CAPABILITY_URL not in registry.snapshot()
