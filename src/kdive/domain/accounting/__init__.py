"""Accounting domain vocabulary."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from kdive.domain._records import DomainBase


class LedgerEventType(StrEnum):
    """The two signed metering events on the ledger (ADR-0007 §3).

    ``reserved`` is the at-grant debit (`+estimate`); ``reconciled`` is the
    at-release/expiry adjustment (`actual - sum(reserved)`), which may be negative as a
    credit for an unused reservation window.
    """

    RESERVED = "reserved"
    RECONCILED = "reconciled"


class CostClassCoefficient(DomainBase):
    """One row of the per-``cost_class`` cost multiplier table (ADR-0007 §1)."""

    cost_class: str
    coeff: Decimal
    updated_at: datetime


class Budget(DomainBase):
    """A project's spend budget with the O(1) running spent total (ADR-0007 §3)."""

    project: str
    limit_kcu: Decimal
    spent_kcu: Decimal = Decimal(0)
    updated_at: datetime


class Quota(DomainBase):
    """A project's concurrency caps (ADR-0007 §4, ADR-0069)."""

    project: str
    max_concurrent_allocations: int
    max_concurrent_systems: int
    max_pending_allocations: int = 0
    updated_at: datetime


class LedgerEntry(DomainBase):
    """One append-only, signed metering row (ADR-0007 §3)."""

    id: UUID
    ts: datetime
    project: str
    allocation_id: UUID
    resource_id: UUID | None = None
    cost_class: str
    event_type: LedgerEventType
    kcu_delta: Decimal
    note: str | None = None


__all__ = [
    "Budget",
    "CostClassCoefficient",
    "LedgerEntry",
    "LedgerEventType",
    "Quota",
]
