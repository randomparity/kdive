"""Bind the six enqueueable external-boot operations into one registry."""

from __future__ import annotations

from kdive.jobs.handlers.external_boot.lifecycle import (
    activate_handler,
    cleanup_handler,
    recover_handler,
    release_handler,
    resolve_conflict_handler,
    teardown_handler,
)
from kdive.jobs.handlers.external_boot.operations import ExternalBootOperations
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts

__all__ = ["build_operations"]


def build_operations(ports: ExternalBootHandlerPorts) -> ExternalBootOperations:
    """Build the operations registry, raising on a duplicate or non-enqueueable binding.

    The mapping is written out rather than derived from a naming convention, so adding a seventh
    operation is a visible edit here and a registry that stopped binding one is a red test rather
    than a job that dispatches nowhere.
    """
    operations = ExternalBootOperations()
    operations.register("activate", activate_handler(ports))
    operations.register("recover", recover_handler(ports))
    operations.register("resolve-conflict", resolve_conflict_handler(ports))
    operations.register("release", release_handler(ports))
    operations.register("cleanup", cleanup_handler(ports))
    operations.register("teardown", teardown_handler(ports))
    return operations
