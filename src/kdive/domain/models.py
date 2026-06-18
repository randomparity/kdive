"""Compatibility facade for durable domain records.

The bounded domain modules own their records and enums:

* :mod:`kdive.domain.catalog.resources` owns resources.
* :mod:`kdive.domain.lifecycle` owns allocations, systems, runs, and investigations.
* :mod:`kdive.domain.jobs` owns jobs and power/job vocabulary.
* :mod:`kdive.domain.accounting`, :mod:`kdive.domain.catalog.images`, and
  :mod:`kdive.domain.catalog.artifacts` own their respective catalog records.

This module remains as the legacy aggregate import surface for repository code and tests while
call sites move to the bounded modules.
"""

from __future__ import annotations

from kdive.domain._records import DomainBase as DomainBase
from kdive.domain._records import DomainModel as DomainModel
from kdive.domain.accounting import Budget as Budget
from kdive.domain.accounting import CostClassCoefficient as CostClassCoefficient
from kdive.domain.accounting import LedgerEntry as LedgerEntry
from kdive.domain.accounting import LedgerEventType as LedgerEventType
from kdive.domain.accounting import Quota as Quota
from kdive.domain.catalog.artifacts import Artifact as Artifact
from kdive.domain.catalog.artifacts import Sensitivity as Sensitivity
from kdive.domain.catalog.images import ImageCatalogEntry as ImageCatalogEntry
from kdive.domain.catalog.images import ImageState as ImageState
from kdive.domain.catalog.images import ImageVisibility as ImageVisibility
from kdive.domain.catalog.resources import ManagedBy as ManagedBy
from kdive.domain.catalog.resources import Resource as Resource
from kdive.domain.catalog.resources import ResourceKind as ResourceKind
from kdive.domain.jobs import DESTRUCTIVE_JOB_KINDS as DESTRUCTIVE_JOB_KINDS
from kdive.domain.jobs import DestructiveJobKind as DestructiveJobKind
from kdive.domain.jobs import Job as Job
from kdive.domain.jobs import JobAuthorizing as JobAuthorizing
from kdive.domain.jobs import JobKind as JobKind
from kdive.domain.jobs import PowerAction as PowerAction
from kdive.domain.lifecycle import Allocation as Allocation
from kdive.domain.lifecycle import Attribution as Attribution
from kdive.domain.lifecycle import DebugSession as DebugSession
from kdive.domain.lifecycle import ExpectedBootFailure as ExpectedBootFailure
from kdive.domain.lifecycle import ExternalRef as ExternalRef
from kdive.domain.lifecycle import Investigation as Investigation
from kdive.domain.lifecycle import Run as Run
from kdive.domain.lifecycle import System as System
from kdive.domain.lifecycle import SystemShape as SystemShape
from kdive.domain.pcie import PCIeClaim as PCIeClaim

__all__ = [
    "DESTRUCTIVE_JOB_KINDS",
    "Allocation",
    "Artifact",
    "Attribution",
    "Budget",
    "CostClassCoefficient",
    "DebugSession",
    "DestructiveJobKind",
    "DomainBase",
    "DomainModel",
    "ExpectedBootFailure",
    "ExternalRef",
    "ImageCatalogEntry",
    "ImageState",
    "ImageVisibility",
    "Investigation",
    "Job",
    "JobAuthorizing",
    "JobKind",
    "LedgerEntry",
    "LedgerEventType",
    "ManagedBy",
    "PCIeClaim",
    "PowerAction",
    "Quota",
    "Resource",
    "ResourceKind",
    "Run",
    "Sensitivity",
    "System",
    "SystemShape",
]
