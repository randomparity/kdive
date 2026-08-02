"""Coverage anchor for the split install run handler module."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.handlers.runs import install as runs_install
from kdive.jobs.handlers.runs import registrar as runs
from kdive.providers.ports.lifecycle import InstallRequest


def test_install_handler_is_exported_through_runs_facade() -> None:
    assert runs.install_handler is runs_install.install_handler


def test_reusable_install_requires_every_referenced_artifact_version() -> None:
    refs = {"kernel": "k", "initrd": "i", "vmlinux": "v"}

    assert runs_install._validated_artifact_versions(None, refs, None) is None
    assert runs_install._validated_artifact_versions(
        "digest.generation", refs, {"kernel": "kv", "initrd": "iv", "vmlinux": "vv"}
    ) == {"kernel": "kv", "initrd": "iv", "vmlinux": "vv"}

    for versions in (None, {}, {"kernel": "kv"}, {"kernel": "kv", "initrd": ""}):
        try:
            runs_install._validated_artifact_versions("digest.generation", refs, versions)
        except CategorizedError as exc:
            assert exc.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            assert exc.details["reason"] == "reusable_build_versions_incomplete"
        else:
            raise AssertionError(f"incomplete versions unexpectedly accepted: {versions!r}")


def test_cancelled_install_waits_for_provider_thread_before_abandoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot let the outer handler release its build pin early."""
    entered = threading.Event()
    release = threading.Event()
    abandoned_after_thread: list[bool] = []

    class Installer:
        def install(self, request: object) -> None:
            entered.set()
            assert release.wait(10)

    async def claimed(*args: object) -> object:
        return SimpleNamespace(claimed=True)

    async def abandon(*args: object) -> None:
        abandoned_after_thread.append(release.is_set())

    monkeypatch.setattr(runs_install, "claim_run_step", claimed)
    monkeypatch.setattr(runs_install, "abandon_run_step_best_effort", abandon)

    async def exercise() -> None:
        task = asyncio.create_task(
            runs_install._run_install_step(
                cast(AsyncConnection, object()),
                uuid4(),
                Installer(),
                cast(InstallRequest, object()),
            )
        )
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert abandoned_after_thread == [True]
