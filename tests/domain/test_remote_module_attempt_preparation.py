"""Closed module-attempt preparation carrier contracts (ADR-0605)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from kdive.domain.remote_module_attempt_preparation import (
    ModuleAttemptObligationReceiptV1,
    ModuleAttemptPreparationRequestV1,
)

_SYSTEM_ID = UUID("11111111-1111-4111-8111-111111111111")
_RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
_NONCE = "a" * 32


def _receipt() -> ModuleAttemptObligationReceiptV1:
    return ModuleAttemptObligationReceiptV1(
        system_id=_SYSTEM_ID, run_id=_RUN_ID, operation_nonce=_NONCE
    )


def test_request_round_trips_only_canonical_bounded_json() -> None:
    request = ModuleAttemptPreparationRequestV1(module_attempt_obligation=_receipt())
    encoded = request.to_canonical_json()

    assert encoded == (
        b'{"module_attempt_obligation":{"operation_nonce":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"run_id":"22222222-2222-4222-8222-222222222222",'
        b'"schema":"module-attempt-obligation-receipt-v1",'
        b'"system_id":"11111111-1111-4111-8111-111111111111"},'
        b'"schema":"module-attempt-preparation-request-v1"}'
    )
    assert ModuleAttemptPreparationRequestV1.from_canonical_json(encoded) == request


@pytest.mark.parametrize(
    "change",
    [
        lambda value: {**value, "extra": True},
        lambda value: {**value, "schema": "module-attempt-preparation-request-v2"},
        lambda value: {**value, "module_attempt_obligation": True},
    ],
)
def test_request_rejects_closed_shape_and_types(
    change: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    value = ModuleAttemptPreparationRequestV1(module_attempt_obligation=_receipt()).model_dump(
        mode="python", by_alias=True
    )
    with pytest.raises(ValidationError):
        ModuleAttemptPreparationRequestV1.model_validate(change(value))


@pytest.mark.parametrize(
    "encoded",
    [
        b'{"schema":"module-attempt-preparation-request-v1",'
        b'"module_attempt_obligation":{"schema":"module-attempt-obligation-receipt-v1",'
        b'"system_id":"11111111-1111-4111-8111-111111111111",'
        b'"run_id":"22222222-2222-4222-8222-222222222222",'
        b'"operation_nonce":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}',
        ModuleAttemptPreparationRequestV1(module_attempt_obligation=_receipt()).to_canonical_json()
        + b"\n",
        b" " * 4097,
    ],
)
def test_request_rejects_noncanonical_or_oversize_bytes(encoded: bytes) -> None:
    with pytest.raises((ValueError, ValidationError)):
        ModuleAttemptPreparationRequestV1.from_canonical_json(encoded)


def test_malformed_decoder_error_does_not_echo_payload() -> None:
    marker = "traceable-input-marker"

    with pytest.raises(ValueError) as caught:
        ModuleAttemptPreparationRequestV1.from_canonical_json(
            ('{"schema":"' + marker + '"}').encode()
        )

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("nonce", ["A" * 32, "a" * 31, "g" * 32, 1])
def test_receipt_rejects_malformed_nonce_and_wrong_scalar_type(nonce: object) -> None:
    with pytest.raises(ValidationError):
        ModuleAttemptObligationReceiptV1(
            system_id=_SYSTEM_ID,
            run_id=_RUN_ID,
            operation_nonce=nonce,  # ty: ignore[invalid-argument-type]
        )
