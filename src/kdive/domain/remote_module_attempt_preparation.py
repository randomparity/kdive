"""Server-to-worker module-attempt preparation carrier (ADR-0605)."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

type OperationNonce = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", strict=True)]

_CANONICAL_REQUEST_MAX_BYTES = 4_096


class _ClosedCanonicalValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_alias=True, strict=True)

    def to_canonical_json(self) -> bytes:
        """Return this value's compact, sorted UTF-8 representation."""
        encoded = json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        if len(encoded) > _CANONICAL_REQUEST_MAX_BYTES:
            raise ValueError("module-attempt preparation value exceeds 4096 bytes")
        return encoded

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        """Parse one bounded canonical document, rejecting alternate encodings."""
        if len(data) > _CANONICAL_REQUEST_MAX_BYTES:
            raise ValueError("module-attempt preparation value exceeds 4096 bytes")
        value = cls.model_validate_json(data)
        if value.to_canonical_json() != data:
            raise ValueError("module-attempt preparation value is not canonical JSON")
        return value


class ModuleAttemptObligationReceiptV1(_ClosedCanonicalValue):
    """Evidence naming the exact durable mutation obligation a worker must verify."""

    schema_: Literal["module-attempt-obligation-receipt-v1"] = Field(
        "module-attempt-obligation-receipt-v1", alias="schema"
    )
    system_id: UUID
    run_id: UUID
    operation_nonce: OperationNonce


class ModuleAttemptPreparationRequestV1(_ClosedCanonicalValue):
    """The closed carrier #2173 will place unchanged in its durable job payload."""

    schema_: Literal["module-attempt-preparation-request-v1"] = Field(
        "module-attempt-preparation-request-v1", alias="schema"
    )
    module_attempt_obligation: ModuleAttemptObligationReceiptV1
