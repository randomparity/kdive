"""Durable remote-module phase classification."""

from __future__ import annotations

from typing import Any

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleResultV1,
)
from kdive.services.remote_module_phases import classify_phase


def _result(phase: str) -> RemoteModuleResultV1:
    values: dict[str, Any] = {
        "status": "success",
        "phase": phase,
        "system_id": "12345678-1234-4234-8234-123456789abc",
        "run_id": "87654321-4321-4321-8321-cba987654321",
        "plan_identity": "sha256:" + "1" * 64,
        "operation_nonce": "2" * 32,
        "appliance_image_digest": "sha256:" + "3" * 64,
        "release": "6.12.0",
        "root_volume_key": "root",
        "root_volume_identity": "sha256:" + "4" * 64,
        "source_manifest": "sha256:" + "5" * 64,
        "capture_manifest": "sha256:" + "6" * 64,
    }
    if phase == "captured":
        values.update(entry_count=1, content_bytes=10)
    elif phase in {"replacement-ready", "installed"}:
        values.update(
            installed_manifest="sha256:" + "7" * 64,
            entry_count=1,
            content_bytes=10,
        )
    elif phase in {"restore-ready", "restored"}:
        values["installed_manifest"] = "sha256:" + "7" * 64
    return RemoteModuleResultV1.model_validate(values)


@pytest.mark.parametrize(
    ("operation", "phase", "action"),
    [
        ("capture_install", "captured", "install"),
        ("capture_install", "staging-intent", "install"),
        ("capture_install", "replacement-ready", "install"),
        ("capture_install", "installed", "finish-install"),
        ("restore", "installed", "restore"),
        ("restore", "restore-ready", "restore"),
        ("restore", "restored", "finish-restore"),
    ],
)
def test_classify_phase_maps_only_resumable_composites(
    operation: str, phase: str, action: str
) -> None:
    assert classify_phase(operation, _result(phase)) == action


def test_classify_phase_rejects_cross_operation_composite() -> None:
    with pytest.raises(CategorizedError) as caught:
        classify_phase("restore", _result("captured"))

    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details == {"operation": "restore", "phase": "captured"}
