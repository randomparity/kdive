"""Local-libvirt external-boot recovery state-machine tests (ADR-0586)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    FinalizeCleanupProof,
    LibguestfsAuthenticatedGuestTree,
    LocalLibvirtExternalBoot,
    LocalRecoveryMetadataV1,
    ModuleLayout,
    PublicationPhase,
    RecoveryMetadataStore,
    RecoveryPhase,
    advance_absence_publication,
    advance_module_publication,
    recovery_directory_name,
    render_target_xml,
)
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryPoint,
    RunningKernelObservation,
)

_SOURCE_XML = """<domain xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0">
  <name>kdive-system</name>
  <metadata><owner system="00000000-0000-0000-0000-000000000001" /></metadata>
  <memory unit="MiB">2048</memory>
  <os firmware="efi"><type arch="x86_64">hvm</type><kernel>/old</kernel>
    <initrd>/old-i</initrd><cmdline>old</cmdline></os>
  <devices><disk type="file"><target dev="vda" /></disk></devices>
  <qemu:commandline><qemu:arg value="-S" /></qemu:commandline>
</domain>"""


def test_render_target_xml_changes_only_owned_boot_projection() -> None:
    rendered = render_target_xml(
        _SOURCE_XML,
        kernel="artifacts/kernel",
        initrd="artifacts/initrd",
        cmdline="root=/dev/vda1 console=ttyS0",
    )

    assert '<memory unit="MiB">2048</memory>' in rendered
    assert '<disk type="file"><target dev="vda" /></disk>' in rendered
    assert '<qemu:arg value="-S"' in rendered
    assert "<kernel>artifacts/kernel</kernel>" in rendered
    assert "<initrd>artifacts/initrd</initrd>" in rendered
    assert "<cmdline>root=/dev/vda1 console=ttyS0</cmdline>" in rendered


def test_render_target_xml_omits_optional_initrd() -> None:
    rendered = render_target_xml(
        _SOURCE_XML, kernel="artifacts/kernel", initrd=None, cmdline="root=/dev/vda1"
    )
    assert "<initrd>" not in rendered


@pytest.mark.parametrize(
    "source",
    [
        "<domain>",
        '<!DOCTYPE domain [<!ENTITY x "x">]><domain>&x;</domain>',
        "<domain><name>e\u0301</name></domain>",
    ],
)
def test_render_target_xml_rejects_malformed_forbidden_or_non_nfc(source: str) -> None:
    with pytest.raises(ValueError, match="domain XML"):
        render_target_xml(source, kernel="kernel", initrd=None, cmdline="root=/dev/vda1")


_PRIOR = PresentComponentState(manifest="sha256:" + "1" * 64)
_DESIRED = PresentComponentState(manifest="sha256:" + "2" * 64)


class _PublicationIO:
    def __init__(self, layout: ModuleLayout, *, fail_move: str | None = None) -> None:
        self.layout = layout
        self.actions: list[str] = []
        self.fail_move = fail_move

    def require_inactive(self) -> None:
        self.actions.append("inactive")

    def observe_layout(self) -> ModuleLayout:
        self.actions.append("observe")
        return self.layout

    def move_live_to_old(self) -> None:
        self.actions.append("live-to-old")
        if self.fail_move == "live-to-old":
            raise OSError("ambiguous move result")

    def move_staging_to_live(self) -> None:
        self.actions.append("staging-to-live")
        if self.fail_move == "staging-to-live":
            raise OSError("ambiguous move result")

    def move_old_to_live(self) -> None:
        self.actions.append("old-to-live")
        if self.fail_move == "old-to-live":
            raise OSError("ambiguous move result")

    def remove_old(self) -> None:
        self.actions.append("remove-old")

    def guest_sync(self) -> None:
        self.actions.append("guest-sync")

    def record_phase(self, phase: PublicationPhase) -> None:
        self.actions.append(f"phase:{phase}")


@pytest.mark.parametrize(
    ("phase", "layout", "action"),
    [
        ("move-ready", ModuleLayout(_PRIOR, _DESIRED, None), "live-to-old"),
        ("move-ready", ModuleLayout(None, _DESIRED, _PRIOR), "phase:old-aside"),
        ("old-aside", ModuleLayout(None, _DESIRED, _PRIOR), "staging-to-live"),
        ("old-aside", ModuleLayout(_DESIRED, None, _PRIOR), "phase:new-live"),
        ("rollback-ready", ModuleLayout(None, _DESIRED, _PRIOR), "old-to-live"),
        ("rollback-ready", ModuleLayout(_PRIOR, _DESIRED, None), "phase:rollback-complete"),
        ("new-live", ModuleLayout(_DESIRED, None, _PRIOR), "remove-old"),
        ("new-live", ModuleLayout(_DESIRED, None, None), "phase:publication-complete"),
    ],
)
def test_present_restart_table_has_one_permitted_action(
    phase: PublicationPhase, layout: ModuleLayout, action: str
) -> None:
    io = _PublicationIO(layout)
    advance_module_publication(io, phase=phase, layout=layout, prior=_PRIOR, desired=_DESIRED)
    assert io.actions[-1] == action


def test_unlisted_restart_layout_conflicts_without_mutation() -> None:
    layout = ModuleLayout(_DESIRED, _DESIRED, _PRIOR)
    io = _PublicationIO(layout)
    with pytest.raises(ValueError, match="conflict"):
        advance_module_publication(
            io,
            phase=PublicationPhase.OLD_ASIDE,
            layout=layout,
            prior=_PRIOR,
            desired=_DESIRED,
        )
    assert io.actions == ["inactive"]


@pytest.mark.parametrize(
    ("after_error", "tail"),
    [
        (ModuleLayout(None, _DESIRED, _PRIOR), ["inactive", "observe", "phase:rollback-ready"]),
        (ModuleLayout(_DESIRED, None, _PRIOR), ["observe", "guest-sync", "phase:new-live"]),
    ],
)
def test_staging_move_error_is_classified_from_three_names(
    after_error: ModuleLayout, tail: list[str]
) -> None:
    io = _PublicationIO(after_error, fail_move="staging-to-live")
    advance_module_publication(
        io,
        phase=PublicationPhase.OLD_ASIDE,
        layout=ModuleLayout(None, _DESIRED, _PRIOR),
        prior=_PRIOR,
        desired=_DESIRED,
    )
    assert io.actions[-3:] == tail


def test_staging_move_error_third_layout_conflicts_without_further_mutation() -> None:
    io = _PublicationIO(ModuleLayout(_PRIOR, _DESIRED, _PRIOR), fail_move="staging-to-live")
    with pytest.raises(ValueError, match="conflict"):
        advance_module_publication(
            io,
            phase=PublicationPhase.OLD_ASIDE,
            layout=ModuleLayout(None, _DESIRED, _PRIOR),
            prior=_PRIOR,
            desired=_DESIRED,
        )
    assert io.actions == ["inactive", "staging-to-live", "inactive", "observe"]


@pytest.mark.parametrize(
    ("phase", "before", "after", "move", "next_phase"),
    [
        (
            "move-ready",
            ModuleLayout(_PRIOR, _DESIRED, None),
            ModuleLayout(None, _DESIRED, _PRIOR),
            "live-to-old",
            "old-aside",
        ),
        (
            "rollback-ready",
            ModuleLayout(None, _DESIRED, _PRIOR),
            ModuleLayout(_PRIOR, _DESIRED, None),
            "old-to-live",
            "rollback-complete",
        ),
    ],
)
def test_other_present_move_errors_classify_before_and_after_effect(
    phase: PublicationPhase,
    before: ModuleLayout,
    after: ModuleLayout,
    move: str,
    next_phase: str,
) -> None:
    before_io = _PublicationIO(before, fail_move=move)
    with pytest.raises(OSError):
        advance_module_publication(
            before_io, phase=phase, layout=before, prior=_PRIOR, desired=_DESIRED
        )
    assert before_io.actions[-3:] == ["inactive", "observe", move]

    after_io = _PublicationIO(after, fail_move=move)
    advance_module_publication(after_io, phase=phase, layout=before, prior=_PRIOR, desired=_DESIRED)
    assert after_io.actions[-3:] == ["observe", "guest-sync", f"phase:{next_phase}"]


@pytest.mark.parametrize(
    ("phase", "layout", "action"),
    [
        ("move-ready", ModuleLayout(_PRIOR, None, None), "live-to-old"),
        ("move-ready", ModuleLayout(None, None, _PRIOR), "phase:absence-live"),
        ("move-ready", ModuleLayout(None, None, None), "phase:absence-complete"),
        ("absence-live", ModuleLayout(None, None, _PRIOR), "phase:absence-complete"),
        ("absence-complete", ModuleLayout(None, None, _PRIOR), "remove-old"),
        ("absence-complete", ModuleLayout(None, None, None), "phase:absence-cleaned"),
    ],
)
def test_absence_restart_table_has_one_permitted_action(
    phase: PublicationPhase, layout: ModuleLayout, action: str
) -> None:
    io = _PublicationIO(layout)
    advance_absence_publication(io, phase=phase, layout=layout, prior=_PRIOR)
    assert io.actions[-1] == action


def test_absence_terminal_rejects_reappeared_tree() -> None:
    layout = ModuleLayout(_PRIOR, None, None)
    io = _PublicationIO(layout)
    with pytest.raises(ValueError, match="conflict"):
        advance_absence_publication(
            io, phase=PublicationPhase.ABSENCE_CLEANED, layout=layout, prior=_PRIOR
        )
    assert io.actions == ["inactive"]


@pytest.mark.parametrize(
    ("observed", "tail"),
    [
        (ModuleLayout(_PRIOR, None, None), ["inactive", "observe", "live-to-old"]),
        (ModuleLayout(None, None, _PRIOR), ["observe", "guest-sync", "phase:absence-live"]),
    ],
)
def test_absence_live_move_error_reclassifies_exact_layout(
    observed: ModuleLayout, tail: list[str]
) -> None:
    io = _PublicationIO(observed, fail_move="live-to-old")
    if observed.live is _PRIOR:
        with pytest.raises(OSError):
            advance_absence_publication(
                io,
                phase=PublicationPhase.MOVE_READY,
                layout=ModuleLayout(_PRIOR, None, None),
                prior=_PRIOR,
            )
    else:
        advance_absence_publication(
            io,
            phase=PublicationPhase.MOVE_READY,
            layout=ModuleLayout(_PRIOR, None, None),
            prior=_PRIOR,
        )
    assert io.actions[-3:] == tail


_BINDING = ExternalBootActivationBinding(
    system_id="00000000-0000-0000-0000-000000000001",
    run_id="00000000-0000-0000-0000-000000000002",
    activation_id="00000000-0000-0000-0000-000000000003",
)


def test_recovery_reference_resolves_only_exact_binding() -> None:
    reference = OpaqueProviderRef(
        ref=(
            "local-recovery-v1/00000000-0000-0000-0000-000000000001/"
            "00000000-0000-0000-0000-000000000003"
        )
    )
    assert recovery_directory_name(reference, _BINDING) == (
        "00000000-0000-0000-0000-000000000001.00000000-0000-0000-0000-000000000003"
    )


@pytest.mark.parametrize(
    "reference",
    [
        "local-recovery-v1/00000000-0000-0000-0000-000000000009/"
        "00000000-0000-0000-0000-000000000003",
        "local-recovery-v1/00000000-0000-0000-0000-000000000001/not-a-uuid",
        "other/00000000-0000-0000-0000-000000000001/00000000-0000-0000-0000-000000000003",
    ],
)
def test_recovery_reference_rejects_cross_owner_or_malformed(reference: str) -> None:
    with pytest.raises((ValueError, ValidationError), match="recovery"):
        recovery_directory_name(OpaqueProviderRef(ref=reference), _BINDING)


def test_finalize_cleanup_proof_is_closed_and_mutation_started_only() -> None:
    values = {
        "point_digest": "sha256:" + "3" * 64,
        "binding": _BINDING,
        "operation_id": "00000000-0000-0000-0000-000000000004",
        "attempt_id": "00000000-0000-0000-0000-000000000005",
        "journal_sequence": 7,
        "journal_digest": "sha256:" + "4" * 64,
        "phase": "mutation-started",
    }
    assert FinalizeCleanupProof.model_validate(values).journal_sequence == 7
    with pytest.raises(ValidationError):
        FinalizeCleanupProof.model_validate(values | {"phase": "terminal"})
    with pytest.raises(ValidationError):
        FinalizeCleanupProof.model_validate(values | {"extra": "forbidden"})


def _metadata(phase: RecoveryPhase = "pre-stop-intent") -> LocalRecoveryMetadataV1:
    absent = AbsentComponentState()
    state = ProviderStateIdentity(definition="sha256:" + "5" * 64, modules=absent)
    return LocalRecoveryMetadataV1(
        binding=_BINDING,
        plan_identity="sha256:" + "6" * 64,
        materialization_identity="sha256:" + "7" * 64,
        release="6.12.0",
        materialized_modules=OpaqueProviderRef(ref="artifacts/system/run/modules"),
        materialized_modules_sha256="sha256:" + "8" * 64,
        materialized_modules_bytes=123,
        source_xml_sha256="sha256:" + hashlib.sha256(_SOURCE_XML.encode()).hexdigest(),
        source_xml=_SOURCE_XML,
        source_definition="sha256:" + "a" * 64,
        source_boot="sha256:" + "b" * 64,
        target_boot="sha256:" + "c" * 64,
        source_state=state,
        target_state=state,
        prior_power="running",
        capture={"state": "absent"},
        phase=phase,
    )


def test_recovery_metadata_store_publishes_reopens_and_advances_phase(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata()
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        assert store.reopen(reference, metadata.binding) == metadata
        updated = store.record_phase(reference, metadata.binding, metadata, "publication-complete")
        assert store.reopen(reference, metadata.binding) == updated

    directory = root / recovery_directory_name(reference, metadata.binding)
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    assert oct((directory / "intent.json").stat().st_mode & 0o777) == "0o600"


def test_recovery_metadata_store_rejects_hostile_root_and_partial(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="owner-only"):
        RecoveryMetadataStore(root)

    os.chmod(root, 0o700)
    metadata = _metadata()
    reference = OpaqueProviderRef(
        ref=f"local-recovery-v1/{_BINDING.system_id}/{_BINDING.activation_id}"
    )
    partial = root / f".{recovery_directory_name(reference, _BINDING)}.partial"
    partial.mkdir(mode=0o700)
    (partial / "foreign").write_text("do not remove")
    before = sorted(path.name for path in partial.iterdir())
    with RecoveryMetadataStore(root) as store, pytest.raises(ValueError, match="partial"):
        store.publish(metadata)
    assert sorted(path.name for path in partial.iterdir()) == before


def test_cleanup_tombstone_finalization_is_exact_and_absent_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    point = _point(metadata)
    digest = LocalLibvirtExternalBoot.point_digest(point)
    proof = FinalizeCleanupProof(
        point_digest=digest,
        binding=point.binding,
        operation_id="00000000-0000-0000-0000-000000000004",
        attempt_id="00000000-0000-0000-0000-000000000005",
        journal_sequence=7,
        journal_digest="sha256:" + "4" * 64,
    )
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        store.publish_tombstone(reference, metadata.binding, metadata, digest)
        directory = root / recovery_directory_name(reference, metadata.binding)
        assert sorted(path.name for path in directory.iterdir()) == ["tombstone.json"]
        store.finalize_tombstone(reference, point, proof)
        assert not directory.exists()
        store.finalize_tombstone(reference, point, proof)

        wrong = proof.model_copy(update={"point_digest": "sha256:" + "e" * 64})
        with pytest.raises(ValueError, match="does not match"):
            store.finalize_tombstone(reference, point, wrong)


class _GuestTreeHandle:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def exists(self, path: str) -> int:
        return 1

    def is_dir(self, path: str, *, followsymlinks: bool) -> int:
        return int(path.endswith("-staging"))

    def find(self, path: str) -> list[str]:
        return ["module.ko"]

    def lstatns(self, path: str) -> dict[str, int]:
        return {"st_mode": 0o100600, "st_uid": 1, "st_gid": 2, "st_size": 3, "st_nlink": 1}

    def readlink(self, path: str) -> str:
        raise AssertionError("not a symlink")

    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]:
        return []

    def download(self, remotefilename: str, filename: str) -> None:
        with open(filename, "wb") as output:
            output.write(b"elf")

    def mkdir(self, path: str) -> None:
        self.calls.append(("mkdir", path))

    def upload(self, filename: str, remotefilename: str) -> None:
        self.calls.append(("upload", remotefilename))

    def ln_s(self, target: str, linkname: str) -> None:
        self.calls.append(("symlink", target, linkname))

    def chmod(self, mode: int, path: str) -> None:
        self.calls.append(("chmod", mode, path))

    def chown(self, owner: int, group: int, path: str) -> None:
        self.calls.append(("chown", owner, group, path))

    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None:
        self.calls.append(("xattr", xattr, val, path))

    def rm_rf(self, path: str) -> None:
        self.calls.append(("remove", path))


def test_libguestfs_tree_is_bound_private_and_no_follow() -> None:
    guest = _GuestTreeHandle()
    root = f"/lib/modules/.kdive-{_BINDING.activation_id}-staging"
    tree = LibguestfsAuthenticatedGuestTree(
        guest, binding=_BINDING, release="6.12.0", root=root, mutable=False
    )
    assert tree.root_kind() == "directory"
    entry = next(tree.entries())
    assert entry.path == "module.ko"
    with tree.open_regular(entry.path, entry.size) as content:
        assert content.read() == b"elf"
    with pytest.raises(ValueError, match="read-only"):
        tree.remove_all()
    with pytest.raises(ValueError, match="canonical relative"):
        tree.open_regular("../escape", 0).__enter__()


def test_libguestfs_tree_rejects_cross_activation_root_before_guest_call() -> None:
    guest = _GuestTreeHandle()
    with pytest.raises(ValueError, match="bound release"):
        LibguestfsAuthenticatedGuestTree(
            guest,
            binding=_BINDING,
            release="6.12.0",
            root="/lib/modules/.kdive-00000000-0000-0000-0000-000000000009-staging",
            mutable=True,
        )
    assert guest.calls == []


def _point(metadata: LocalRecoveryMetadataV1) -> RecoveryPoint:
    return RecoveryPoint(
        binding=metadata.binding,
        plan_identity=metadata.plan_identity,
        materialization_identity=metadata.materialization_identity,
        recovery_ref=OpaqueProviderRef(
            ref=f"local-recovery-v1/{_BINDING.system_id}/{_BINDING.activation_id}"
        ),
        source_state=metadata.source_state,
        target_state=metadata.target_state,
    )


class _ExternalIO:
    def __init__(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.metadata = metadata
        self.actions: list[str] = []
        self.tombstone = False

    def materialize(
        self, plan: object, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization:
        self.actions.append("materialize")
        raise RuntimeError("not used")

    def prepare(
        self, materialization: object, binding: object, authority: object
    ) -> LocalRecoveryMetadataV1:
        self.actions.append("prepare")
        return self.metadata

    def recovery_ref(self, binding: ExternalBootActivationBinding) -> OpaqueProviderRef:
        return _point(self.metadata).recovery_ref

    def reopen(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> LocalRecoveryMetadataV1:
        self.actions.append("reopen")
        return self.metadata

    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("activate-modules")

    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("define-target")

    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation:
        self.actions.append("observe-running")
        return RunningKernelObservation(
            architecture="x86_64", release=metadata.release, gnu_build_id="01020304"
        )

    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("recover-modules")

    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("define-source")

    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("restore-power")

    def record_phase(
        self, metadata: LocalRecoveryMetadataV1, phase: str
    ) -> LocalRecoveryMetadataV1:
        self.actions.append(f"phase:{phase}")
        self.metadata = metadata.model_copy(update={"phase": phase})
        return self.metadata

    def cleanup(self, metadata: LocalRecoveryMetadataV1, point_digest: str) -> None:
        self.actions.append("cleanup")
        self.tombstone = True

    def finalize_tombstone(self, recovery: RecoveryPoint, proof: FinalizeCleanupProof) -> None:
        self.actions.append("finalize")
        self.tombstone = False


def test_six_port_activation_recovery_and_cleanup_ordering() -> None:
    io = _ExternalIO(_metadata())
    ports = LocalLibvirtExternalBoot(io)
    point = _point(io.metadata)
    authority = OpaqueProviderRef(ref="authority/current")

    ports.activate(point, authority)
    assert io.actions == [
        "reopen",
        "activate-modules",
        "phase:module-restored",
        "define-target",
        "phase:target-defined",
    ]
    io.actions.clear()
    assert ports.observe(point, authority).release == "6.12.0"
    assert io.actions == ["reopen", "observe-running"]

    io.actions.clear()
    ports.recover(point, authority)
    assert io.actions == [
        "reopen",
        "recover-modules",
        "phase:module-restored",
        "define-source",
        "phase:source-restored",
        "restore-power",
        "phase:recovered",
    ]
    io.actions.clear()
    ports.cleanup(point, authority)
    assert io.actions == ["reopen", "cleanup"]


def test_reopen_rejects_complete_point_substitution_before_mutation() -> None:
    io = _ExternalIO(_metadata())
    ports = LocalLibvirtExternalBoot(io)
    point = _point(io.metadata).model_copy(update={"plan_identity": "sha256:" + "f" * 64})
    with pytest.raises(ValueError, match="does not match"):
        ports.activate(point, OpaqueProviderRef(ref="authority/current"))
    assert io.actions == ["reopen"]


def test_observe_requires_durable_target_definition() -> None:
    io = _ExternalIO(_metadata("module-restored"))
    with pytest.raises(ValueError, match="target-defined"):
        LocalLibvirtExternalBoot(io).observe(
            _point(io.metadata), OpaqueProviderRef(ref="authority/current")
        )
    assert io.actions == ["reopen"]


def test_finalize_requires_exact_cleanup_proof_binding_and_point() -> None:
    io = _ExternalIO(_metadata("recovered"))
    ports = LocalLibvirtExternalBoot(io)
    point = _point(io.metadata)
    ports.cleanup(point, OpaqueProviderRef(ref="authority/current"))
    proof = FinalizeCleanupProof(
        point_digest=ports.point_digest(point),
        binding=point.binding,
        operation_id="00000000-0000-0000-0000-000000000004",
        attempt_id="00000000-0000-0000-0000-000000000005",
        journal_sequence=1,
        journal_digest="sha256:" + "d" * 64,
    )
    ports.finalize_cleanup_tombstone(point, proof, OpaqueProviderRef(ref="authority/current"))
    assert io.actions[-2:] == ["cleanup", "finalize"]

    wrong = proof.model_copy(update={"point_digest": "sha256:" + "e" * 64})
    with pytest.raises(ValueError, match="proof"):
        ports.finalize_cleanup_tombstone(point, wrong, OpaqueProviderRef(ref="authority/current"))
