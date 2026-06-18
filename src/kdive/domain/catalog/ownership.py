"""Row ownership partitions shared by declarative and runtime inventory."""

from __future__ import annotations

from enum import StrEnum


class ManagedBy(StrEnum):
    """Row-ownership partition for reconciled inventory tables (ADR-0112).

    ``CONFIG`` rows are owned by declarative ``systems.toml`` bring-up; ``DISCOVERY`` rows are
    owned by provider discovery; ``RUNTIME`` rows are owned by imperative agent tools. The
    partition keeps declarative reconcile and imperative registration from pruning or
    overwriting each other's rows.
    """

    CONFIG = "config"
    DISCOVERY = "discovery"
    RUNTIME = "runtime"
