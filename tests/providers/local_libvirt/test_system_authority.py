"""Private intent persistence checks for local authority-owned System operations."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.providers.local_libvirt.lifecycle.boot.session import owned_system_semantic_identity
from kdive.providers.local_libvirt.lifecycle.provisioning import LocalLibvirtProvisioning
from kdive.providers.local_libvirt.lifecycle.rootfs.baseline_kernel import BaselineKernel
from kdive.providers.local_libvirt.lifecycle.storage import (
    ProvisioningFiles,
    baseline_dir,
    overlay_path,
)
from kdive.providers.local_libvirt.system_authority import (
    LocalAuthoritySystemError,
    LocalAuthoritySystemProvider,
    LocalAuthoritySystemTopology,
    _Completion,
    _Intent,
)
from kdive.providers.ports.external_boot import RootSpecV1
from kdive.providers.system_authority import (
    AuthoritySystemCommitContextV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
    AuthoritySystemProvisionFacts,
    AuthoritySystemProvisionSnapshot,
)
from kdive.providers.system_authority.composition import _AuthorityProvisioner
from tests.providers.local_libvirt.fakes import FakeLibvirtConn
from tests.providers.local_libvirt.lifecycle.boot.session_support import _xml

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
        assert_no_sibling_attachment=lambda _system_id, _overlay, _baseline: None,
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


def test_authority_tcg_defined_domain_matches_retained_intent_identity(tmp_path: Path) -> None:
    profile = ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "ppc64le",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "linux-test",
            "provider": {
                "local-libvirt": {
                    "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/base.qcow2"}
                }
            },
        }
    )
    intent = _intent(tmp_path)
    intent = replace(
        intent,
        overlay=overlay_path(intent.system_id),
        baseline=baseline_dir(intent.system_id),
    )
    root_spec = RootSpecV1(
        architecture="ppc64le",
        root="/dev/vda1",
        arguments=("root=/dev/vda1",),
        authority="stage-inspection",
        source={"kind": "staged-image", "identity": _DIGEST},
    )
    bootstrap_key = "ssh-ed25519 YWFhYQ== kdive-system"
    snapshot = AuthoritySystemProvisionSnapshot(
        system_id=intent.system_id,
        allocation_id=intent.allocation_id,
        resource_id=intent.resource_id,
        project="project-a",
        provider_kind="local-libvirt",
        resource_name="local-a",
        authority_instance=intent.authority_instance,
        profile=profile,
        profile_identity="sha256:" + profile_digest(profile),
        source_image_id=uuid4(),
        root_identity=_DIGEST,
        root_spec=root_spec,
        bootstrap_public_key=bootstrap_key,
        bootstrap_identity="sha256:" + hashlib.sha256(bootstrap_key.encode()).hexdigest(),
    )
    conn = FakeLibvirtConn(
        caps_xml=(
            "<capabilities><host><cpu><arch>x86_64</arch></cpu></host>"
            "<guest><os_type>hvm</os_type><arch name='x86_64'>"
            "<emulator>/usr/bin/qemu-system-x86_64</emulator>"
            "<domain type='qemu'/><domain type='kvm'/></arch></guest>"
            "<guest><os_type>hvm</os_type><arch name='ppc64le'>"
            "<emulator>/usr/bin/qemu-system-ppc64</emulator>"
            "<domain type='qemu'/></arch></guest></capabilities>"
        )
    )
    expected_guest_arch = ("tcg", "/usr/bin/qemu-system-ppc64")
    provisioner = LocalLibvirtProvisioning(
        connect=lambda: conn,
        files=ProvisioningFiles(
            make_overlay=lambda _base, _overlay: None,
            remove_overlay=lambda _overlay: None,
            remove_baseline=lambda _baseline: None,
            overlay_exists=lambda _overlay: False,
            baseline_exists=lambda _baseline: False,
            prepare_console_log=lambda _path: None,
            overlay_virtual_size=lambda _overlay: 1 << 60,
        ),
        materialize_rootfs=lambda rootfs, _system_id, _arch, *, job_id=None: rootfs.path,
        extract_baseline_kernel=lambda _base, dest, _hint=None: BaselineKernel(
            kernel=dest / "kernel", initrd=None
        ),
        free_port=lambda: 2200,
    )
    provider = LocalAuthoritySystemProvider(
        provisioner=_AuthorityProvisioner(provisioner, expected_guest_arch),
        topology=LocalAuthoritySystemTopology(
            intent_root=tmp_path / "intents",
            overlay_root=tmp_path / "overlays",
            baseline_root=tmp_path / "baseline",
            staged_bases={_DIGEST: tmp_path / "base.qcow2"},
            accel=expected_guest_arch[0],
            emulator=expected_guest_arch[1],
        ),
        readiness_probe=lambda _system_id: False,
        open_teardown=lambda *_args: _AbsentTeardown(),
        assert_no_sibling_attachment=lambda _system_id, _overlay, _baseline: None,
        allocate_port=lambda: 2200,
    )
    try:
        provider._provisioner.provision(
            intent.system_id,
            profile,
            selected_ssh_port=intent.ssh_port,
        )
        intent_with_xml = provider._intent_with_xml(
            intent, snapshot, BaselineKernel(kernel=Path(intent.baseline) / "kernel", initrd=None)
        )
    finally:
        provider.close()

    assert len(conn.defined_xml) == 1
    assert intent_with_xml.xml_digest == owned_system_semantic_identity(
        conn.defined_xml[0], intent.system_id, intent.overlay
    )


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
    expected = _xml(overlay=intent.overlay, system_id=intent.system_id)
    divergent = expected.replace("<kernel>/old</kernel>", "<kernel>/different</kernel>")
    intent = replace(
        intent,
        xml_digest=owned_system_semantic_identity(expected, intent.system_id, intent.overlay),
    )

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
        assert_no_sibling_attachment=lambda _system_id, _overlay, _baseline: None,
        allocate_port=lambda: 2200,
    )

    with pytest.raises(LocalAuthoritySystemError, match="does not match"):
        provider._verify_retained_provision_identity(intent)


def test_teardown_checks_retained_identity_and_siblings_before_destroy(tmp_path: Path) -> None:
    intent = _intent(tmp_path)
    Path(intent.overlay).parent.mkdir()
    Path(intent.overlay).write_bytes(b"qcow2")
    Path(intent.baseline).mkdir(parents=True)
    xml = _xml(overlay=intent.overlay, system_id=intent.system_id)
    intent = replace(
        intent,
        xml_digest=owned_system_semantic_identity(xml, intent.system_id, intent.overlay),
    )
    events: list[str] = []

    class Inspection:
        domain_absent = False
        domain_validated = True
        overlay_absent = False
        baseline_absent = False

    class Teardown:
        def inspect(self) -> Inspection:
            return Inspection()

        def owned_xml(self) -> tuple[str, str]:
            events.append("xml")
            return xml, xml

        def destroy(self) -> None:
            events.append("destroy")

        def undefine(self) -> None:
            events.append("undefine")

        def remove_overlay(self) -> None:
            events.append("overlay")

        def remove_baseline(self) -> None:
            events.append("baseline")

        def close(self) -> None:
            events.append("close")

    def reject_sibling(_system_id: object, _overlay: object, _baseline: object) -> None:
        events.append("siblings")
        raise LocalAuthoritySystemError("foreign overlay attachment")

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
        assert_no_sibling_attachment=reject_sibling,
        allocate_port=lambda: 2200,
    )
    provider._store_intent(intent)
    request = AuthoritySystemMutationRequestV1(
        system_id=intent.system_id,
        allocation_id=intent.allocation_id,
        resource_id=intent.resource_id,
        provider_kind="local-libvirt",
        resource_name="local-a",
        authority_instance=intent.authority_instance,
        profile_identity=_DIGEST,
        root_identity=intent.root_identity,
        operation=AuthoritySystemOperation.PREACTIVATION_TEARDOWN,
        operation_identity="teardown-a",
        authority_id=uuid4(),
        generation=1,
        attempt_id=uuid4(),
        operation_digest=_DIGEST,
        bootstrap_identity=intent.bootstrap_identity,
    )
    context = AuthoritySystemCommitContextV1(
        attempt_id=request.attempt_id,
        operation=AuthoritySystemOperation.PREACTIVATION_TEARDOWN,
        journal_sequence=1,
        journal_digest=_DIGEST,
    )

    with pytest.raises(LocalAuthoritySystemError, match="foreign overlay"):
        asyncio.run(provider.execute_preactivation_teardown(request, context))

    assert events == ["xml", "close", "siblings", "close"]
    assert (tmp_path / "intents" / f"{intent.system_id}.json").exists()


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
        assert_no_sibling_attachment=lambda _system_id, _overlay, _baseline: None,
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
    receipt = tmp_path / "intents" / f"{request.system_id}.preactivation-teardown.complete.json"
    before = receipt.read_bytes()
    replayed = asyncio.run(provider.observe_preactivation_teardown(request, context))

    assert replayed.complete
    assert replayed.completed_at == facts.completed_at
    assert receipt.read_bytes() == before
    assert calls == ["inspect", "close", "inspect", "close"]


def test_manifest_binding_rejects_wrong_resource_before_teardown_provider_call(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    base = tmp_path / "base.qcow2"
    base.touch()

    def open_teardown(*_args: object) -> _AbsentTeardown:
        calls.append("teardown")
        return _AbsentTeardown()

    provider = LocalAuthoritySystemProvider(
        provisioner=_Provisioner(),
        topology=LocalAuthoritySystemTopology(
            intent_root=tmp_path / "intents",
            overlay_root=tmp_path / "overlays",
            baseline_root=tmp_path / "baseline",
            staged_bases={_DIGEST: base},
        ),
        readiness_probe=lambda _system_id: False,
        open_teardown=open_teardown,
        assert_no_sibling_attachment=lambda _system_id, _overlay, _baseline: None,
        allocate_port=lambda: 2200,
        manifest_binding=("local-libvirt", "local-a", "authority-a"),
    )
    request = AuthoritySystemMutationRequestV1(
        system_id=uuid4(),
        allocation_id=uuid4(),
        resource_id=uuid4(),
        provider_kind="local-libvirt",
        resource_name="local-b",
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

    with pytest.raises(LocalAuthoritySystemError, match="not bound"):
        asyncio.run(provider.execute_preactivation_teardown(request, context))

    assert calls == []


def test_completion_receipt_replays_a_stable_terminal_timestamp(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    intent = _intent(tmp_path)
    request = AuthoritySystemMutationRequestV1(
        system_id=intent.system_id,
        allocation_id=intent.allocation_id,
        resource_id=intent.resource_id,
        provider_kind="local-libvirt",
        resource_name="local-a",
        authority_instance=intent.authority_instance,
        profile_identity=_DIGEST,
        root_identity=intent.root_identity,
        operation=AuthoritySystemOperation.PROVISION,
        operation_identity="provision-a",
        authority_id=uuid4(),
        generation=1,
        attempt_id=uuid4(),
        operation_digest=intent.operation_digest,
        bootstrap_identity=intent.bootstrap_identity,
    )
    candidate = AuthoritySystemProvisionFacts(
        intent_identity=intent.identity,
        domain_owned=True,
        root_storage_owned=True,
        boot_ready=True,
        bootstrap_ready=False,
        quarantine_retained=True,
        completed_at=None,
    )

    completed = provider._complete_provision_facts(intent, request, candidate, create=True)
    receipt = tmp_path / "intents" / f"{intent.system_id}.provision.complete.json"
    before = receipt.read_bytes()
    replayed = provider._complete_provision_facts(intent, request, candidate, create=False)

    assert completed.complete
    assert replayed == completed
    assert receipt.read_bytes() == before


_BOOTSTRAP_KEY = "ssh-ed25519 YWFhYQ== kdive-system"
_BOOTSTRAP_IDENTITY = "sha256:" + hashlib.sha256(_BOOTSTRAP_KEY.encode()).hexdigest()


def _generation_digest(generation: int) -> str:
    return "sha256:" + f"{generation:064x}"


def _provision_inputs(
    intent: _Intent, *, generation: int, allocation_id: UUID | None = None
) -> tuple[
    AuthoritySystemMutationRequestV1,
    AuthoritySystemCommitContextV1,
    AuthoritySystemProvisionSnapshot,
]:
    """One attempt generation's request, as migration 0149 binds it: a new digest per generation."""
    profile = ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "ppc64le",
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 20,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "linux-test",
            "provider": {
                "local-libvirt": {
                    "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/base.qcow2"}
                }
            },
        }
    )
    allocation = intent.allocation_id if allocation_id is None else allocation_id
    request = AuthoritySystemMutationRequestV1(
        system_id=intent.system_id,
        allocation_id=allocation,
        resource_id=intent.resource_id,
        provider_kind="local-libvirt",
        resource_name="local-a",
        authority_instance=intent.authority_instance,
        profile_identity="sha256:" + profile_digest(profile),
        root_identity=intent.root_identity,
        operation=AuthoritySystemOperation.PROVISION,
        operation_identity="provision-a",
        authority_id=uuid4(),
        generation=generation,
        attempt_id=uuid4(),
        operation_digest=_generation_digest(generation),
        bootstrap_identity=_BOOTSTRAP_IDENTITY,
    )
    context = AuthoritySystemCommitContextV1(
        attempt_id=request.attempt_id,
        operation=AuthoritySystemOperation.PROVISION,
        journal_sequence=1,
        journal_digest=_DIGEST,
    )
    snapshot = AuthoritySystemProvisionSnapshot(
        system_id=intent.system_id,
        allocation_id=allocation,
        resource_id=intent.resource_id,
        project="project-a",
        provider_kind="local-libvirt",
        resource_name="local-a",
        authority_instance=intent.authority_instance,
        profile=profile,
        profile_identity=request.profile_identity,
        source_image_id=uuid4(),
        root_identity=intent.root_identity,
        root_spec=RootSpecV1(
            architecture="ppc64le",
            root="/dev/vda1",
            arguments=("root=/dev/vda1",),
            authority="stage-inspection",
            source={"kind": "staged-image", "identity": intent.root_identity},
        ),
        bootstrap_public_key=_BOOTSTRAP_KEY,
        bootstrap_identity=_BOOTSTRAP_IDENTITY,
    )
    return request, context, snapshot


def _generation_one_intent(tmp_path: Path) -> _Intent:
    return replace(
        _intent(tmp_path),
        bootstrap_identity=_BOOTSTRAP_IDENTITY,
        operation_digest=_generation_digest(1),
    )


def test_later_generation_observes_the_retained_intent(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    intent = _generation_one_intent(tmp_path)
    provider._store_intent(intent)

    facts = asyncio.run(provider.observe_system_provision(*_provision_inputs(intent, generation=2)))

    assert facts.intent_identity == intent.identity
    assert not facts.complete


def test_retained_intent_rejects_another_allocation(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    intent = _generation_one_intent(tmp_path)
    provider._store_intent(intent)
    inputs = _provision_inputs(intent, generation=2, allocation_id=uuid4())

    with pytest.raises(LocalAuthoritySystemError, match="does not match the request"):
        asyncio.run(provider.observe_system_provision(*inputs))


def test_later_generation_execute_resumes_a_present_domain(tmp_path: Path) -> None:
    intent = _generation_one_intent(tmp_path)
    Path(intent.overlay).parent.mkdir()
    Path(intent.overlay).write_bytes(b"qcow2")
    Path(intent.baseline).mkdir(parents=True)
    xml = _xml(overlay=intent.overlay, system_id=intent.system_id)
    intent = replace(
        intent, xml_digest=owned_system_semantic_identity(xml, intent.system_id, intent.overlay)
    )

    class Inspection(_AbsentInspection):
        domain_absent = False
        domain_validated = True

    class Teardown(_AbsentTeardown):
        def inspect(self) -> Inspection:
            return Inspection()

        def owned_xml(self) -> tuple[str, str]:
            return xml, xml

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
        readiness_probe=lambda _system_id: True,
        open_teardown=lambda *_args: Teardown(),
        assert_no_sibling_attachment=lambda _system_id, _overlay, _baseline: None,
        allocate_port=lambda: 2200,
        now=lambda: intent.deadline + timedelta(minutes=1),
    )
    provider._store_intent(intent)

    facts = asyncio.run(provider.execute_system_provision(*_provision_inputs(intent, generation=2)))

    assert facts.complete
    assert facts.intent_identity == intent.identity


def test_later_generation_execute_past_deadline_with_absent_domain_fails_closed(
    tmp_path: Path,
) -> None:
    intent = _generation_one_intent(tmp_path)
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
        readiness_probe=lambda _system_id: True,
        open_teardown=lambda *_args: _AbsentTeardown(),
        assert_no_sibling_attachment=lambda _system_id, _overlay, _baseline: None,
        allocate_port=lambda: 2200,
        now=lambda: intent.deadline,
    )
    provider._store_intent(intent)

    with pytest.raises(LocalAuthoritySystemError, match="deadline expired"):
        asyncio.run(provider.execute_system_provision(*_provision_inputs(intent, generation=2)))


def test_provision_completion_replays_for_a_later_generation(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    intent = _generation_one_intent(tmp_path)
    first, _context, _snapshot = _provision_inputs(intent, generation=1)
    later, _context, _snapshot = _provision_inputs(intent, generation=2)
    candidate = AuthoritySystemProvisionFacts(
        intent_identity=intent.identity,
        domain_owned=True,
        root_storage_owned=True,
        boot_ready=True,
        bootstrap_ready=False,
        quarantine_retained=True,
        completed_at=None,
    )

    completed = provider._complete_provision_facts(intent, first, candidate, create=True)
    replayed = provider._complete_provision_facts(intent, later, candidate, create=False)

    assert replayed == completed


def _teardown_inputs(
    system_id: UUID, generation: int
) -> tuple[AuthoritySystemMutationRequestV1, AuthoritySystemCommitContextV1]:
    request = AuthoritySystemMutationRequestV1(
        system_id=system_id,
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
        generation=generation,
        attempt_id=uuid4(),
        operation_digest=_generation_digest(generation),
        bootstrap_identity=_DIGEST,
    )
    context = AuthoritySystemCommitContextV1(
        attempt_id=request.attempt_id,
        operation=AuthoritySystemOperation.PREACTIVATION_TEARDOWN,
        journal_sequence=1,
        journal_digest=_DIGEST,
    )
    return request, context


def test_teardown_completion_replays_for_a_later_generation(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    system_id = uuid4()

    first = asyncio.run(provider.execute_preactivation_teardown(*_teardown_inputs(system_id, 1)))
    later = asyncio.run(provider.observe_preactivation_teardown(*_teardown_inputs(system_id, 2)))

    assert first.complete
    assert later.complete
    assert later.completed_at == first.completed_at


def test_retained_intent_teardown_accepts_an_earlier_generation_receipt(tmp_path: Path) -> None:
    class Teardown(_AbsentTeardown):
        def destroy(self) -> None:
            return None

        def undefine(self) -> None:
            return None

        def remove_overlay(self) -> None:
            return None

        def remove_baseline(self) -> None:
            return None

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
        assert_no_sibling_attachment=lambda _system_id, _overlay, _baseline: None,
        allocate_port=lambda: 2200,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    intent = _intent(tmp_path)
    provider._store_intent(intent)
    request, context = _teardown_inputs(intent.system_id, 2)
    request = request.model_copy(
        update={
            "allocation_id": intent.allocation_id,
            "resource_id": intent.resource_id,
        }
    )
    stored = provider._store_completion(
        _Completion(
            system_id=intent.system_id,
            operation=AuthoritySystemOperation.PREACTIVATION_TEARDOWN,
            operation_digest=_generation_digest(1),
            completed_at=datetime(2026, 9, 5, tzinfo=UTC),
        )
    )

    facts = asyncio.run(provider.execute_preactivation_teardown(request, context))

    assert facts.complete
    assert facts.completed_at == stored.completed_at
    assert provider._load_intent(intent.system_id) is None
