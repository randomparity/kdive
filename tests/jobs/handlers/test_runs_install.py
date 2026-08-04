"""Coverage anchor for the split install run handler module."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from pydantic import SecretStr

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

    async def acquire(*args: object, **kwargs: object) -> None:
        return None

    async def release_use(*args: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(runs_install, "claim_run_step", claimed)
    monkeypatch.setattr(runs_install, "acquire_build_use", acquire)
    monkeypatch.setattr(runs_install, "abandon_run_step_best_effort", abandon)
    monkeypatch.setattr(runs_install, "release_build_use", release_use)

    async def exercise() -> None:
        credential = SecretStr("worker-test-incarnation-credential")
        task = asyncio.create_task(
            runs_install._run_install_step(
                cast(AsyncConnection, object()),
                uuid4(),
                Installer(),
                cast(InstallRequest, object()),
                job_id=uuid4(),
                attempt=1,
                incarnation_credential=credential,
            )
        )
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert abandoned_after_thread == [True]


def test_cancelled_install_keeps_build_use_until_provider_thread_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release_provider = threading.Event()
    provider_exited = threading.Event()
    run_id, expected_job_id, use_id = uuid4(), uuid4(), uuid4()
    credential = SecretStr("worker-test-incarnation-credential")
    active_uses: set[object] = set()
    order: list[str] = []

    class Installer:
        def install(self, request: object) -> None:
            entered.set()
            assert release_provider.wait(10)
            provider_exited.set()
            order.append("provider-exit")

    async def claimed(*args: object) -> object:
        return SimpleNamespace(claimed=True)

    async def acquire(
        conn: object,
        acquired_run_id: object,
        *,
        job_id: object,
        attempt: int,
        incarnation_credential: SecretStr,
    ) -> object:
        assert acquired_run_id == run_id
        assert job_id == expected_job_id
        assert attempt == 2
        assert incarnation_credential is credential
        active_uses.add(use_id)
        return use_id

    async def abandon(*args: object) -> None:
        assert provider_exited.is_set()
        assert active_uses == {use_id}
        order.append("abandon")

    async def release(
        conn: object,
        released_use_id: object,
        *,
        incarnation_credential: SecretStr,
    ) -> bool:
        assert released_use_id == use_id
        assert incarnation_credential is credential
        assert provider_exited.is_set()
        assert active_uses == {use_id}
        active_uses.remove(use_id)
        order.append("release")
        return True

    monkeypatch.setattr(runs_install, "claim_run_step", claimed)
    monkeypatch.setattr(runs_install, "acquire_build_use", acquire, raising=False)
    monkeypatch.setattr(runs_install, "abandon_run_step_best_effort", abandon)
    monkeypatch.setattr(runs_install, "release_build_use", release, raising=False)

    async def exercise() -> None:
        task = asyncio.create_task(
            runs_install._run_install_step(
                cast(AsyncConnection, object()),
                run_id,
                Installer(),
                cast(InstallRequest, object()),
                job_id=expected_job_id,
                attempt=2,
                incarnation_credential=credential,
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            assert active_uses == {use_id}
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert active_uses == {use_id}
            assert order == []
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert active_uses == {use_id}
            assert order == []
        finally:
            release_provider.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert not active_uses
    assert order == ["provider-exit", "abandon", "release"]


def test_cancelled_install_provider_failure_reraises_cancellation_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release_provider = threading.Event()
    provider_exited = threading.Event()
    run_id, expected_job_id, use_id = uuid4(), uuid4(), uuid4()
    credential = SecretStr("worker-test-incarnation-credential")
    active_uses: set[object] = set()
    order: list[str] = []
    provider_error = CategorizedError(
        "provider install failed",
        category=ErrorCategory.INSTALL_FAILURE,
    )

    class Installer:
        def install(self, request: object) -> None:
            entered.set()
            assert release_provider.wait(10)
            provider_exited.set()
            order.append("provider-failed")
            raise provider_error

    async def claimed(*args: object) -> object:
        return SimpleNamespace(claimed=True)

    async def acquire(*args: object, **kwargs: object) -> object:
        active_uses.add(use_id)
        return use_id

    async def abandon(*args: object) -> None:
        assert provider_exited.is_set()
        assert active_uses == {use_id}
        order.append("abandon")

    async def release(*args: object, **kwargs: object) -> bool:
        assert provider_exited.is_set()
        assert active_uses == {use_id}
        active_uses.remove(use_id)
        order.append("release")
        return True

    monkeypatch.setattr(runs_install, "claim_run_step", claimed)
    monkeypatch.setattr(runs_install, "acquire_build_use", acquire)
    monkeypatch.setattr(runs_install, "abandon_run_step_best_effort", abandon)
    monkeypatch.setattr(runs_install, "release_build_use", release)

    async def exercise() -> None:
        task = asyncio.create_task(
            runs_install._run_install_step(
                cast(AsyncConnection, object()),
                run_id,
                Installer(),
                cast(InstallRequest, object()),
                job_id=expected_job_id,
                attempt=2,
                incarnation_credential=credential,
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            assert active_uses == {use_id}
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert active_uses == {use_id}
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert active_uses == {use_id}
        finally:
            release_provider.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert not active_uses
    assert order == ["provider-failed", "abandon", "release"]


@pytest.mark.parametrize("release_fails", [False, True])
def test_cancellation_during_release_drains_release_before_abandoning(
    monkeypatch: pytest.MonkeyPatch, *, release_fails: bool
) -> None:
    provider_exited = threading.Event()
    release_entered = asyncio.Event()
    allow_release = asyncio.Event()
    abandoned = asyncio.Event()
    run_id, expected_job_id, use_id = uuid4(), uuid4(), uuid4()
    credential = SecretStr("worker-test-incarnation-credential")
    active_uses: set[object] = set()
    order: list[str] = []
    release_error = RuntimeError("release failed")

    class Installer:
        def install(self, request: object) -> None:
            provider_exited.set()

    async def claimed(*args: object) -> object:
        return SimpleNamespace(claimed=True)

    async def acquire(*args: object, **kwargs: object) -> object:
        active_uses.add(use_id)
        return use_id

    async def abandon(*args: object) -> None:
        assert provider_exited.is_set()
        assert not active_uses
        order.append("abandon")
        abandoned.set()

    async def release(
        conn: object,
        released_use_id: object,
        *,
        incarnation_credential: SecretStr,
    ) -> bool:
        assert released_use_id == use_id
        assert incarnation_credential is credential
        assert provider_exited.is_set()
        release_entered.set()
        await allow_release.wait()
        assert not abandoned.is_set()
        assert active_uses == {use_id}
        active_uses.remove(use_id)
        order.append("release")
        if release_fails:
            raise release_error
        return True

    monkeypatch.setattr(runs_install, "claim_run_step", claimed)
    monkeypatch.setattr(runs_install, "acquire_build_use", acquire)
    monkeypatch.setattr(runs_install, "abandon_run_step_best_effort", abandon)
    monkeypatch.setattr(runs_install, "release_build_use", release)

    async def exercise() -> None:
        task = asyncio.create_task(
            runs_install._run_install_step(
                cast(AsyncConnection, object()),
                run_id,
                Installer(),
                cast(InstallRequest, object()),
                job_id=expected_job_id,
                attempt=2,
                incarnation_credential=credential,
            )
        )
        await asyncio.wait_for(release_entered.wait(), 10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not abandoned.is_set()
        assert active_uses == {use_id}
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not abandoned.is_set()
        assert active_uses == {use_id}
        allow_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert not active_uses
    assert abandoned.is_set()
    assert order == ["release", "abandon"]


@pytest.mark.parametrize(
    ("provider_fails", "release_fails"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_provider_and_release_outcome_matrix_preserves_failure_precedence(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    provider_fails: bool,
    release_fails: bool,
) -> None:
    run_id, use_id = uuid4(), uuid4()
    credential = SecretStr("worker-test-incarnation-credential")
    provider_error = CategorizedError(
        "provider install failed",
        category=ErrorCategory.INSTALL_FAILURE,
    )
    release_error = RuntimeError(f"release failed: {credential.get_secret_value()}")
    active_uses: set[object] = set()
    claim_running = True
    order: list[str] = []

    class Installer:
        def install(self, request: object) -> None:
            order.append("provider")
            if provider_fails:
                raise provider_error

    async def claimed(*args: object) -> object:
        return SimpleNamespace(claimed=True)

    async def acquire(*args: object, **kwargs: object) -> object:
        active_uses.add(use_id)
        return use_id

    async def abandon(*args: object) -> None:
        nonlocal claim_running
        assert claim_running
        claim_running = False
        order.append("abandon")

    async def release(*args: object, **kwargs: object) -> bool:
        order.append("release")
        if release_fails:
            raise release_error
        active_uses.remove(use_id)
        return True

    monkeypatch.setattr(runs_install, "claim_run_step", claimed)
    monkeypatch.setattr(runs_install, "acquire_build_use", acquire)
    monkeypatch.setattr(runs_install, "abandon_run_step_best_effort", abandon)
    monkeypatch.setattr(runs_install, "release_build_use", release)

    async def exercise() -> None:
        call = runs_install._run_install_step(
            cast(AsyncConnection, object()),
            run_id,
            Installer(),
            cast(InstallRequest, object()),
            job_id=uuid4(),
            attempt=1,
            incarnation_credential=credential,
        )
        if provider_fails:
            with pytest.raises(CategorizedError) as caught:
                await call
            assert caught.value is provider_error
            assert caught.value.category is ErrorCategory.INSTALL_FAILURE
        elif release_fails:
            with pytest.raises(RuntimeError) as caught:
                await call
            assert caught.value is release_error
        else:
            assert await call

    asyncio.run(exercise())
    assert claim_running == (not provider_fails and not release_fails)
    assert active_uses == ({use_id} if release_fails else set())
    expected_order = ["provider"]
    if provider_fails:
        expected_order.append("abandon")
    expected_order.append("release")
    if release_fails and not provider_fails:
        expected_order.append("abandon")
    assert order == expected_order
    if provider_fails and release_fails:
        assert "build-use release failed" in caplog.text
        assert "RuntimeError" in caplog.text
        assert credential.get_secret_value() not in caplog.text


def test_acquire_failure_abandons_claim_without_invoking_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    claimed_steps = {(run_id, "install")}
    provider_invoked = False
    expected = RuntimeError("acquire failed")

    class Installer:
        def install(self, request: object) -> None:
            nonlocal provider_invoked
            provider_invoked = True

    async def claimed(*args: object) -> object:
        return SimpleNamespace(claimed=True)

    async def acquire(*args: object, **kwargs: object) -> None:
        raise expected

    async def abandon(conn: object, abandoned_run_id: UUID, step: str) -> None:
        claimed_steps.remove((abandoned_run_id, step))

    monkeypatch.setattr(runs_install, "claim_run_step", claimed)
    monkeypatch.setattr(runs_install, "acquire_build_use", acquire)
    monkeypatch.setattr(runs_install, "abandon_run_step_best_effort", abandon)

    async def exercise() -> None:
        with pytest.raises(RuntimeError) as caught:
            await runs_install._run_install_step(
                cast(AsyncConnection, object()),
                run_id,
                Installer(),
                cast(InstallRequest, object()),
                job_id=uuid4(),
                attempt=1,
                incarnation_credential=SecretStr("worker-test-incarnation-credential"),
            )
        assert caught.value is expected

    asyncio.run(exercise())
    assert not claimed_steps
    assert not provider_invoked


def test_acquire_cancellation_waits_for_claim_abandonment_without_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquire_entered = asyncio.Event()
    abandon_entered = asyncio.Event()
    allow_abandon = asyncio.Event()
    run_id = uuid4()
    claimed_steps = {(run_id, "install")}
    provider_invoked = False

    class Installer:
        def install(self, request: object) -> None:
            nonlocal provider_invoked
            provider_invoked = True

    async def claimed(*args: object) -> object:
        return SimpleNamespace(claimed=True)

    async def acquire(*args: object, **kwargs: object) -> None:
        acquire_entered.set()
        await asyncio.Event().wait()

    async def abandon(conn: object, abandoned_run_id: UUID, step: str) -> None:
        abandon_entered.set()
        await allow_abandon.wait()
        claimed_steps.remove((abandoned_run_id, step))

    monkeypatch.setattr(runs_install, "claim_run_step", claimed)
    monkeypatch.setattr(runs_install, "acquire_build_use", acquire)
    monkeypatch.setattr(runs_install, "abandon_run_step_best_effort", abandon)

    async def exercise() -> None:
        task = asyncio.create_task(
            runs_install._run_install_step(
                cast(AsyncConnection, object()),
                run_id,
                Installer(),
                cast(InstallRequest, object()),
                job_id=uuid4(),
                attempt=1,
                incarnation_credential=SecretStr("worker-test-incarnation-credential"),
            )
        )
        await asyncio.wait_for(acquire_entered.wait(), 10)
        task.cancel()
        await asyncio.wait_for(abandon_entered.wait(), 10)
        assert not task.done()
        assert claimed_steps == {(run_id, "install")}
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        allow_abandon.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert not claimed_steps
    assert not provider_invoked
