"""Post-prepared external-boot reconciler repair contracts (#2203)."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from kdive.jobs.payloads import BootPayload
from kdive.reconciler.repairs import external_boot
from tests.jobs.handlers.external_boot.support import marked_job


@pytest.mark.parametrize(
    ("lane", "predicate"),
    [
        ("activation", "activation_readiness_deadline < now()"),
        ("recovery", "recovery_readiness_deadline < now()"),
        ("release", "reservation.state = 'ready'"),
        ("cleanup", "NOT a.cleanup_complete"),
    ],
)
def test_candidate_lanes_use_durable_state(lane: str, predicate: str) -> None:
    assert predicate in external_boot._CANDIDATE_SQL[lane]
    assert "preparing" not in external_boot._CANDIDATE_SQL[lane]


def test_repair_rebinds_successor_to_current_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    activation_id = uuid4()
    source = marked_job(
        "activate", activation_id=str(activation_id), authority_instance="authority-old"
    )
    candidate = external_boot._Candidate(activation_id, "activate")
    payload = BootPayload.model_validate(source.payload)
    build = AsyncMock(return_value=(source.kind, payload))
    enqueue = AsyncMock()
    monkeypatch.setattr(external_boot, "_live_successor_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(external_boot, "_source_job", AsyncMock(return_value=source))
    monkeypatch.setattr(external_boot, "build_external_boot_payload", build)
    monkeypatch.setattr(external_boot.queue, "enqueue", enqueue)

    repaired = asyncio.run(
        external_boot._enqueue_candidate(
            cast(Any, object()),
            candidate,
            resolver=cast(Any, object()),
            authority_instance="authority-current",
        )
    )

    assert repaired is True
    call = build.await_args
    assert call is not None
    assert call.kwargs["authority_instance"] == "authority-current"
    assert str(source.id) in call.kwargs["operation_identity"]
    assert enqueue.await_count == 1


def test_live_successor_suppresses_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = external_boot._Candidate(uuid4(), "cleanup")
    source = AsyncMock()
    monkeypatch.setattr(external_boot, "_live_successor_exists", AsyncMock(return_value=True))
    monkeypatch.setattr(external_boot, "_source_job", source)

    repaired = asyncio.run(
        external_boot._enqueue_candidate(
            cast(Any, object()),
            candidate,
            resolver=cast(Any, object()),
            authority_instance="authority-current",
        )
    )

    assert repaired is False
    source.assert_not_awaited()


def test_cleanup_uses_release_purpose() -> None:
    assert external_boot._purpose("cleanup") == "release"
    assert external_boot._purpose("teardown") == "teardown"


def test_repair_module_has_no_provider_adapter_imports() -> None:
    source = inspect.getsource(external_boot)
    assert "providers.local_libvirt" not in source
    assert "providers.remote_libvirt" not in source
    assert "import libvirt" not in source
