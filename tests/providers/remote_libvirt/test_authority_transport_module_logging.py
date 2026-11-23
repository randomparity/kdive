"""Remote module failures leave a structured authority log line (#3349)."""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

import pytest
from pydantic import SecretStr

from kdive.db.external_boot_authority_journal import AuthenticatedPeer
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority import transport
from kdive.providers.external_boot_authority.module_protocol import (
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModuleVolumePreparationRequestV1,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityInstallRefusalV1,
    AuthorityMutationRequestV1,
    AuthorityServiceError,
)
from tests.providers.remote_libvirt.authority_module_support import (
    _remote_preparation_request,
    _terminal_response,
)


def _lifecycle_request(
    preparation: RemoteModuleVolumePreparationRequestV1 | None = None,
    *,
    action: Literal["restore", "reap"] = "restore",
) -> RemoteModuleLifecycleRequestV1:
    authority = (preparation or _remote_preparation_request()).authority
    mutation = AuthorityMutationRequestV1.model_validate(
        {
            **authority.model_dump(
                mode="python",
                by_alias=True,
                exclude={"plan", "purpose", "operation", "operation_identity"},
            ),
            "purpose": "recover" if action == "restore" else "release",
            "operation": "recover" if action == "restore" else "cleanup",
            "operation_identity": "recover-module-op",
        }
    )
    return RemoteModuleLifecycleRequestV1(authority=mutation, action=action, budget_seconds=30)


class _FailingService:
    def __init__(self, completion: str | None) -> None:
        self._completion = completion

    def _fail(self) -> None:
        raise CategorizedError(
            "appliance console tail",
            category=ErrorCategory.CONFLICT,
            details={"completion": self._completion} if self._completion else {},
        )

    async def execute_remote_module_preparation(self, *_args: object) -> None:
        self._fail()

    async def execute_remote_module_lifecycle(self, *_args: object) -> None:
        self._fail()


async def _authenticate(_credential: SecretStr) -> AuthenticatedPeer:
    return AuthenticatedPeer("worker")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "completion", "category"),
    [
        ("execute-remote-module-preparation", "failed-after-mutation", "remote-module-failed"),
        ("execute-remote-module-lifecycle", "failed-after-mutation", "remote-module-failed"),
        ("execute-remote-module-lifecycle", "refused-before-mutation", "remote-module-refused"),
        ("execute-remote-module-preparation", "refused-before-mutation", "remote-module-refused"),
        ("execute-remote-module-preparation", "unknown", "provider-conflict"),
        ("execute-remote-module-lifecycle", "unknown", "provider-conflict"),
        ("execute-remote-module-preparation", None, "provider-conflict"),
        ("execute-remote-module-lifecycle", None, "provider-conflict"),
    ],
)
async def test_remote_module_completion_is_logged_and_wire_response_unchanged(
    operation: Literal["execute-remote-module-preparation", "execute-remote-module-lifecycle"],
    completion: str | None,
    category: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = (
        _remote_preparation_request()
        if operation == "execute-remote-module-preparation"
        else _lifecycle_request()
    )
    envelope = transport.encode_request_envelope(
        operation, request.model_dump(mode="json", by_alias=True), "credential"
    )
    with caplog.at_level(logging.ERROR, logger=transport.__name__):
        response = await transport._dispatch(
            envelope,
            _authenticate,
            None,
            remote_module_service=cast(Any, _FailingService(completion)),
        )

    assert response == f'{{"category":"{category}","status":"error"}}'.encode()
    [record] = [record for record in caplog.records if record.name == transport.__name__]
    message = record.getMessage()
    assert operation in message
    assert str(completion) in message
    assert "appliance console tail" in message
    assert "CategorizedError" in message
    assert record.exc_info is None
    assert "credential" not in message


_OPERATIONS = ("execute-remote-module-preparation", "execute-remote-module-lifecycle")


class _RecordingService:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, AuthenticatedPeer, object]] = []

    def _complete(self, method: str, peer: AuthenticatedPeer, request: object) -> Any:
        self.calls.append((method, peer, request))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def execute_remote_module_preparation(
        self, peer: AuthenticatedPeer, request: object
    ) -> Any:
        return self._complete("execute-remote-module-preparation", peer, request)

    async def execute_remote_module_lifecycle(
        self, peer: AuthenticatedPeer, request: object
    ) -> Any:
        return self._complete("execute-remote-module-lifecycle", peer, request)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize(
    "outcome", ["success", "service-error", "install-refusal", "unexpected", "serialization"]
)
async def test_remote_module_dispatch_preserves_completion_frames(
    operation: str, outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    preparation = _remote_preparation_request()
    terminal = _terminal_response(preparation)
    request = (
        preparation
        if operation == "execute-remote-module-preparation"
        else _lifecycle_request(preparation, action="reap")
    )
    result = (
        terminal
        if operation == "execute-remote-module-preparation"
        else RemoteModuleLifecycleResponseV1(
            action="reap",
            recovery=terminal.recovery,
            operation=preparation.operation,
            result=terminal.result,
            volumes_absent=True,
        )
    )
    # The value's own canonical format is independent of the transport frame encoder.
    expected = b'{"status":"ok","value":' + result.to_canonical_json() + b"}"
    value: object = result
    if outcome == "service-error":
        value = AuthorityServiceError("superseded")
        expected = b'{"category":"superseded","status":"error"}'
    elif outcome == "install-refusal":
        value = AuthorityServiceError(
            "install_failure",
            install_refusal=AuthorityInstallRefusalV1(
                vmlinux_bytes=3, free_bytes=1, headroom_bytes=2
            ),
        )
        expected = (
            b'{"category":"install-failure","install_refusal":'
            b'{"free_bytes":1,"headroom_bytes":2,"vmlinux_bytes":3},"status":"error"}'
        )
    elif outcome == "unexpected":
        value = RuntimeError("unexpected provider failure")
        expected = b'{"category":"provider-conflict","status":"error"}'
    elif outcome == "serialization":

        class BrokenResponse:
            def model_dump(self, **_kwargs: object) -> dict[str, object]:
                raise ValueError("unserializable provider response")

        value = BrokenResponse()
        expected = b'{"category":"provider-conflict","status":"error"}'
    service = _RecordingService(value)
    envelope = transport.encode_request_envelope(
        cast(Any, operation), request.model_dump(mode="json", by_alias=True), "credential"
    )
    response = await transport._dispatch(
        envelope, _authenticate, None, remote_module_service=cast(Any, service)
    )
    assert response == expected
    [(called, peer, received)] = service.calls
    assert called == operation
    assert peer == AuthenticatedPeer("worker")
    assert received == request
    records = [record for record in caplog.records if record.name == transport.__name__]
    if outcome in {"unexpected", "serialization"}:
        [record] = records
        assert record.exc_info is not None
        assert "credential" not in record.getMessage()
    else:
        assert not records


@pytest.mark.anyio
@pytest.mark.parametrize("operation", _OPERATIONS)
@pytest.mark.parametrize("available", [False, True])
async def test_remote_module_dispatch_rejects_mismatched_requests_before_service(
    operation: str, available: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    opposite = (
        _lifecycle_request()
        if operation == "execute-remote-module-preparation"
        else _remote_preparation_request()
    )
    monkeypatch.setattr(
        transport,
        "_decode_envelope",
        lambda _payload: (operation, opposite, SecretStr("credential")),
    )
    service = _RecordingService(object())
    if available:
        with pytest.raises(transport._TransportError, match="invalid-request"):
            await transport._dispatch(
                b"", _authenticate, None, remote_module_service=cast(Any, service)
            )
    else:
        assert await transport._dispatch(b"", _authenticate, None) == (
            b'{"category":"provider-not-configured","status":"error"}'
        )
    assert service.calls == []
