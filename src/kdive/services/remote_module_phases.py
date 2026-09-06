"""Crash-resumable phase orchestration for remote module recovery (ADR-0585)."""

from __future__ import annotations

from typing import Literal

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleResultV1,
)

type ResumeAction = Literal["install", "finish-install", "restore", "finish-restore"]

_PHASE_ACTIONS: dict[tuple[str, str], ResumeAction] = {
    ("capture_install", "captured"): "install",
    ("capture_install", "staging-intent"): "install",
    ("capture_install", "replacement-ready"): "install",
    ("capture_install", "installed"): "finish-install",
    ("restore", "installed"): "restore",
    ("restore", "restore-ready"): "restore",
    ("restore", "restored"): "finish-restore",
}


def classify_phase(operation: str, result: RemoteModuleResultV1) -> ResumeAction:
    """Map only a valid durable operation/result phase composite to its next action."""
    try:
        return _PHASE_ACTIONS[(operation, result.phase)]
    except KeyError as exc:
        raise CategorizedError(
            "unrecognized remote module phase composite",
            category=ErrorCategory.CONFLICT,
            details={"operation": operation, "phase": result.phase},
        ) from exc
