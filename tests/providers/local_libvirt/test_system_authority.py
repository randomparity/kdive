"""Private intent persistence checks for local authority-owned System operations."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.providers.local_libvirt.system_authority import (
    LocalAuthoritySystemError,
    LocalAuthoritySystemProvider,
    LocalAuthoritySystemTopology,
    _Intent,
    _xml_identity,
)
from kdive.providers.system_authority import (
    AuthoritySystemCommitContextV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
)

_DIGEST = "sha256:" + "a" * 64


class _Provisioner:
    def provision(self, *_args: object, **_kwargs: object) -> str:
        raise AssertionError("intent-only tests must not provision")


class _AbsentInspection:
    domain_absent = True
    domain_validated = False
    overlay_absent = True
    baseline_absent = True


class _AbsentTeardown:
    def inspect(self) -> _AbsentInspection:
        return _AbsentInspection()

    def owned_xml(self) -> tuple[str, str]:
        raise AssertionError("absent domain has no XML")

    def destroy(self) -> None:
        raise AssertionError("unexpected teardown")

    def undefine(self) -> None:
        raise AssertionError("unexpected teardown")

    def remove_overlay(self) -> None:
        raise AssertionError("unexpected teardown")

    def remove_baseline(self) -> None:
        raise AssertionError("unexpected teardown")

    def close(self) -> None:
        return None


def _provider(tmp_path: Path) -> LocalAuthoritySystemProvider:
    base = tmp_path / "base.qcow2"
    base.touch()
    return LocalAuthoritySystemProvider(
        provisioner=_Provisioner(),
        topology=LocalAuthoritySystemTopology(
            intent_root=tmp_path / "intents",
            overlay_root=tmp_path / "overlays",
            baseline_root=tmp_path / "baseline",
            staged_bases={_DIGEST: base},
        ),
        readiness_probe=lambda _system_id: False,
        open_teardown=lambda *_args: _AbsentTeardown(),
        allocate_port=lambda: 2200,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )


def _intent(tmp_path: Path) -> _Intent:
    system_id = uuid4()
    return _Intent(
        system_id=system_id,
        allocation_id=uuid4(),
        resource_id=uuid4(),
        authority_instance="authority-a",
        root_identity=_DIGEST,
        bootstrap_identity=_DIGEST,
        operation_digest=_DIGEST,
        deadline=datetime(2026, 9, 6, 0, 15, tzinfo=UTC),
        domain_name=f"kdive-{system_id}",
        overlay=str(tmp_path / "overlays" / f"{system_id}.qcow2"),
        baseline=str(tmp_path / "baseline" / f"{system_id}-baseline"),
        base=str(tmp_path / "base.qcow2"),
        gdb_port=None,
        ssh_port=2200,
        xml_digest=_DIGEST,
    )


def test_private_intent_is_fsynced_private_and_replay_is_exact(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    intent = _intent(tmp_path)

    provider._store_intent(intent)

    path = tmp_path / "intents" / f"{intent.system_id}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert provider._load_intent(intent.system_id) == intent
    provider._store_intent(intent)

    changed = replace(intent, deadline=intent.deadline + timedelta(seconds=1))
    with pytest.raises(LocalAuthoritySystemError, match="replaced"):
        provider._store_intent(changed)


def test_observation_load_does_not_create_private_intent_root(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    assert provider._load_intent(uuid4()) is None

    assert not (tmp_path / "intents").exists()


def test_private_intent_symlink_is_rejected(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    intent = _intent(tmp_path)
    root = tmp_path / "intents"
    root.mkdir(mode=0o700)
    path = root / f"{intent.system_id}.json"
    path.symlink_to(tmp_path / "target")

    with pytest.raises(LocalAuthoritySystemError, match="unsafe"):
        provider._load_intent(intent.system_id)


def test_private_intent_root_symlink_is_rejected(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    (tmp_path / "intents").symlink_to(tmp_path / "other")

    with pytest.raises(LocalAuthoritySystemError, match="unsafe"):
        provider._load_intent(uuid4())


def test_loaded_intent_cannot_redirect_fixed_private_topology(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    intent = replace(_intent(tmp_path), overlay="/foreign/overlay.qcow2")
    provider._store_intent(intent)

    with pytest.raises(LocalAuthoritySystemError, match="fixed topology"):
        provider._load_intent(intent.system_id)


def test_first_attempt_rejects_concrete_retained_baseline_without_intent(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    system_id = uuid4()
    baseline = tmp_path / "baseline" / f"{system_id}-baseline"
    baseline.mkdir(parents=True)

    with pytest.raises(LocalAuthoritySystemError, match="artifact exists"):
        provider._reject_unowned_retained_artifacts(system_id)


def test_retry_rejects_divergent_live_xml_before_provisioner_mutation(tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    Path(intent.overlay).parent.mkdir()
    Path(intent.overlay).write_bytes(b"qcow2")
    Path(intent.baseline).mkdir(parents=True)
    expected = "<domain><name>expected</name></domain>"
    divergent = "<domain><name>different</name></domain>"
    intent = replace(intent, xml_digest=_xml_identity(expected))

    class Inspection:
        domain_absent = False
        domain_validated = True

    class Teardown:
        def inspect(self) -> Inspection:
            return Inspection()

        def owned_xml(self) -> tuple[str, str]:
            return divergent, divergent

        def destroy(self) -> None:
            raise AssertionError("identity check must happen before teardown")

        def undefine(self) -> None:
            raise AssertionError("identity check must happen before teardown")

        def remove_overlay(self) -> None:
            raise AssertionError("identity check must happen before teardown")

        def remove_baseline(self) -> None:
            raise AssertionError("identity check must happen before teardown")

        def close(self) -> None:
            return None

    base = tmp_path / "base.qcow2"
    provider = LocalAuthoritySystemProvider(
        provisioner=_Provisioner(),
        topology=LocalAuthoritySystemTopology(
            intent_root=tmp_path / "intents",
            overlay_root=tmp_path / "overlays",
            baseline_root=tmp_path / "baseline",
            staged_bases={_DIGEST: base},
        ),
        readiness_probe=lambda _system_id: False,
        open_teardown=lambda *_args: Teardown(),
        allocate_port=lambda: 2200,
    )

    with pytest.raises(LocalAuthoritySystemError, match="does not match"):
        provider._verify_retained_provision_identity(intent)


def test_cancellation_drains_the_completion_owned_host_operation(tmp_path: Path) -> None:
    asyncio.run(_cancel_and_drain(tmp_path))


async def _cancel_and_drain(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def operation() -> str:
        started.set()
        assert release.wait(timeout=1)
        return "completed"

    task = asyncio.create_task(provider._offload(operation))
    await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_absent_teardown_replay_inspects_without_creating_or_deleting(tmp_path: Path) -> None:
    calls: list[str] = []

    class Inspection:
        domain_absent = True
        overlay_absent = True
        baseline_absent = True

    class Teardown:
        def inspect(self) -> Inspection:
            calls.append("inspect")
            return Inspection()

        def owned_xml(self) -> tuple[str, str]:
            raise AssertionError("absent replay must not read domain XML")

        def destroy(self) -> None:
            calls.append("destroy")

        def undefine(self) -> None:
            calls.append("undefine")

        def remove_overlay(self) -> None:
            calls.append("overlay")

        def remove_baseline(self) -> None:
            calls.append("baseline")

        def close(self) -> None:
            calls.append("close")

    base = tmp_path / "base.qcow2"
    base.touch()
    provider = LocalAuthoritySystemProvider(
        provisioner=_Provisioner(),
        topology=LocalAuthoritySystemTopology(
            intent_root=tmp_path / "intents",
            overlay_root=tmp_path / "overlays",
            baseline_root=tmp_path / "baseline",
            staged_bases={_DIGEST: base},
        ),
        readiness_probe=lambda _system_id: False,
        open_teardown=lambda *_args: Teardown(),
        allocate_port=lambda: 2200,
    )
    request = AuthoritySystemMutationRequestV1(
        system_id=uuid4(),
        allocation_id=uuid4(),
        resource_id=uuid4(),
        provider_kind="local-libvirt",
        resource_name="local-a",
        authority_instance="authority-a",
        profile_identity=_DIGEST,
        root_identity=_DIGEST,
        operation=AuthoritySystemOperation.PREACTIVATION_TEARDOWN,
        operation_identity="teardown-a",
        authority_id=uuid4(),
        generation=1,
        attempt_id=uuid4(),
        operation_digest=_DIGEST,
        bootstrap_identity=_DIGEST,
    )
    context = AuthoritySystemCommitContextV1(
        attempt_id=request.attempt_id,
        operation=AuthoritySystemOperation.PREACTIVATION_TEARDOWN,
        journal_sequence=1,
        journal_digest=_DIGEST,
    )

    facts = asyncio.run(provider.execute_preactivation_teardown(request, context))

    assert facts.complete
    assert calls == ["inspect", "close"]
    assert not (tmp_path / "intents").exists()
