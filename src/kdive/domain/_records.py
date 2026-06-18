"""Shared Pydantic base records for durable domain rows."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DomainBase(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class DomainModel(DomainBase):
    """Identity and timestamps common to every durable object."""

    id: UUID
    created_at: datetime
    updated_at: datetime
