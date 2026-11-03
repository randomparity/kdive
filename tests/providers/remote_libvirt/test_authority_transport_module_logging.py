"""Remote module failures leave a structured authority log line (#3349)."""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

import pytest
from pydantic import SecretStr

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority import transport
from kdive.providers.external_boot_authority.protocol import AuthorityMutationRequestV1
from kdive.providers.external_boot_authority.service import AuthenticatedPeer
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleLifecycleRequestV1,
)
from tests.providers.remote_libvirt.authority_module_support import _remote_preparation_request


def _lifecycle_request() -> RemoteModuleLifecycleRequestV1:
    authority = _remote_preparation_request().authority
    mutation = AuthorityMutationRequestV1.model_validate(
        {
            **authority.model_dump(
                mode="python",
                by_alias=True,
                exclude={"plan", "purpose", "operation", "operation_identity"},
            ),
            "purpose": "recover",
            "operation": "recover",
            "operation_identity": "recover-module-op",
        }
    )
    return RemoteModuleLifecycleRequestV1(authority=mutation, action="restore", budget_seconds=30)


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
