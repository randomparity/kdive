"""Direct unit tests for the Investigation metadata-mutation guards.

These exercise the transport-neutral validation branches that reject before any pool/DB
work — the pure decision surface of the module. The locked link/unlink/set write paths
are Postgres-backed and covered by the service-level suite.
"""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.security.authz.context import RequestContext
from kdive.services.investigations.common import (
    ExternalRefKey,
    InvestigationErrorReason,
    InvestigationServiceError,
)
from kdive.services.investigations.metadata import (
    set_investigation_record,
    unlink_external_ref_record,
)

_POOL = cast(AsyncConnectionPool, object())
_CTX = cast(RequestContext, object())


def test_unlink_rejects_a_ref_key_without_a_natural_key() -> None:
    with pytest.raises(InvestigationServiceError) as err:
        asyncio.run(
            unlink_external_ref_record(
                _POOL, _CTX, uuid4(), cast(ExternalRefKey, {}), raw_id="raw-1"
            )
        )
    assert err.value.reason is InvestigationErrorReason.INVALID_EXTERNAL_REF
    assert err.value.object_id == "raw-1"
    assert err.value.detail == "ref key must carry a non-empty tracker and id"


def test_unlink_rejects_a_ref_key_with_an_empty_tracker() -> None:
    with pytest.raises(InvestigationServiceError) as err:
        asyncio.run(
            unlink_external_ref_record(
                _POOL, _CTX, uuid4(), cast(ExternalRefKey, {"tracker": "", "id": "x"}), raw_id="r"
            )
        )
    assert err.value.reason is InvestigationErrorReason.INVALID_EXTERNAL_REF


def test_set_requires_at_least_one_of_title_or_description() -> None:
    with pytest.raises(InvestigationServiceError) as err:
        asyncio.run(set_investigation_record(_POOL, _CTX, uuid4(), raw_id="raw-2"))
    assert err.value.reason is InvestigationErrorReason.MISSING_REQUIRED_FIELD
    assert err.value.object_id == "raw-2"


def test_set_rejects_an_empty_title() -> None:
    with pytest.raises(InvestigationServiceError) as err:
        asyncio.run(set_investigation_record(_POOL, _CTX, uuid4(), raw_id="raw-3", title=""))
    assert err.value.reason is InvestigationErrorReason.INVALID_TEXT


def test_set_rejects_an_over_long_title() -> None:
    with pytest.raises(InvestigationServiceError) as err:
        asyncio.run(set_investigation_record(_POOL, _CTX, uuid4(), raw_id="raw-4", title="x" * 201))
    assert err.value.reason is InvestigationErrorReason.INVALID_TEXT
