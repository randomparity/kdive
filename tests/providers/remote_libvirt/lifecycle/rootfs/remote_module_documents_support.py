from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[5]
APPLIANCE = ROOT / "deploy" / "remote_module_appliance"
SYSTEM_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000002"
DIGESTS = {letter: "sha256:" + letter * 64 for letter in "abcdef"}
NONCE = "b" * 32


def _appliance_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "task_1_remote_module_appliance", APPLIANCE / "appliance.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operation(operation: str = "capture_install", **changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol": "remote-module-operation-v1",
        "operation": operation,
        "system_id": SYSTEM_ID,
        "run_id": RUN_ID,
        "plan_identity": DIGESTS["a"],
        "operation_nonce": NONCE,
        "release": "6.12.0-kdive",
        "root_volume": {"key": "root-1", "identity": DIGESTS["c"]},
        "source_manifest": DIGESTS["d"],
        "appliance_image_digest": DIGESTS["e"],
    }
    document.update(changes)
    return document


def _result(**changes: object) -> dict[str, object]:
    document: dict[str, object] = {
        "protocol": "remote-module-result-v1",
        "status": "success",
        "phase": "installed",
        "system_id": SYSTEM_ID,
        "run_id": RUN_ID,
        "plan_identity": DIGESTS["a"],
        "operation_nonce": NONCE,
        "appliance_image_digest": DIGESTS["e"],
        "release": "6.12.0-kdive",
        "root_volume_key": "root-1",
        "root_volume_identity": DIGESTS["c"],
        "source_manifest": DIGESTS["d"],
        "installed_manifest": DIGESTS["f"],
        "capture_manifest": DIGESTS["b"],
        "entry_count": 12,
        "content_bytes": 4096,
    }
    document.update(changes)
    return document


def _result_shape_space() -> list[dict[str, object]]:
    identity = {
        "system_id": SYSTEM_ID,
        "run_id": RUN_ID,
        "plan_identity": DIGESTS["a"],
        "operation_nonce": NONCE,
        "appliance_image_digest": DIGESTS["e"],
        "release": "6.12.0-kdive",
        "root_volume_key": "root-1",
        "root_volume_identity": DIGESTS["c"],
        "source_manifest": DIGESTS["d"],
    }
    phases = [
        "accepted",
        "captured",
        "staging-intent",
        "replacement-ready",
        "installed",
        "restore-ready",
        "restored",
    ]
    captures: list[dict[str, object]] = [
        {},
        {"capture_manifest": DIGESTS["b"]},
        {"capture_absent": True},
        {"capture_manifest": DIGESTS["b"], "capture_absent": True},
    ]
    counts: list[dict[str, object]] = [{}, {"entry_count": 12, "content_bytes": 4096}]
    installed: list[dict[str, object]] = [{}, {"installed_manifest": DIGESTS["f"]}]
    shapes = []
    for status, error_code in (
        ("success", None),
        ("failure", "INVALID_DOCUMENT"),
        ("failure", "RECOVERY_CONFLICT"),
    ):
        for has_identity in (True, False):
            for phase in phases:
                for capture in captures:
                    for count in counts:
                        for install in installed:
                            shape: dict[str, object] = {
                                "protocol": "remote-module-result-v1",
                                "status": status,
                                "phase": phase,
                            }
                            if error_code is not None:
                                shape["error_code"] = error_code
                            if has_identity:
                                shape.update(identity)
                            shape.update(capture)
                            shape.update(count)
                            shape.update(install)
                            shapes.append(shape)
    return shapes
