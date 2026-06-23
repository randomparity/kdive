"""Pin the shared Pydantic base records for durable domain rows."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kdive.domain._records import DomainBase, DomainModel


def _model() -> DomainModel:
    now = datetime.now(UTC)
    return DomainModel(id=uuid4(), created_at=now, updated_at=now)


def test_domain_model_carries_identity_and_timestamps() -> None:
    m = _model()
    assert m.id is not None
    assert isinstance(m.created_at, datetime)
    assert isinstance(m.updated_at, datetime)


def test_domain_base_forbids_extra_fields() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        DomainModel(id=uuid4(), created_at=now, updated_at=now, surprise="x")


def test_domain_base_validates_assignment() -> None:
    m = _model()
    with pytest.raises(ValidationError):
        m.id = "not-a-uuid"  # validate_assignment must reject the bad type


def test_domain_model_extends_domain_base() -> None:
    assert issubclass(DomainModel, DomainBase)
