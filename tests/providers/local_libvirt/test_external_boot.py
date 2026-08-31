"""Local-libvirt external-boot recovery state-machine tests (ADR-0586)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    CleanupTombstoneV1,
    FinalizeCleanupProof,
    LibguestfsAuthenticatedGuestTree,
    LocalLibvirtExternalBoot,
    LocalPreStopIntentV1,
    LocalRecoveryMetadataV1,
    ModuleLayout,
    PublicationPhase,
    RealLocalExternalBootIO,
    RecoveryMetadataStore,
    RecoveryPhase,
    TargetProjectionStore,
    TargetProjectionV1,
    advance_absence_publication,
    advance_module_publication,
    convert_kernel_bundle_modules,
    recovery_directory_name,
    render_target_xml,
)
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    MaterializedArtifacts,
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


def _raw_bundle(names: list[tuple[str, bytes]]) -> bytes:
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name, content in names:
            member = tarfile.TarInfo(name)
            member.mode, member.uid, member.gid = 0o640, 17, 23
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return result.getvalue()


def test_bundle_converter_is_deterministic_and_declares_xattrs_unsupported() -> None:
    entries = [
        ("lib/modules/6.12.0/kernel/z.ko", b"z"),
        ("lib/modules/6.12.0/kernel/a.ko", b"a"),
    ]
    outputs: list[bytes] = []
    for ordered in (entries, list(reversed(entries))):
        destination = io.BytesIO()
        digest, size = convert_kernel_bundle_modules(
            io.BytesIO(_raw_bundle(ordered)), destination, release="6.12.0"
        )
        assert digest == "sha256:" + hashlib.sha256(destination.getvalue()).hexdigest()
        assert size == len(destination.getvalue())
        outputs.append(destination.getvalue())
    assert outputs[0] == outputs[1]
    with tarfile.open(fileobj=io.BytesIO(outputs[0]), mode="r:") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == ["kernel/a.ko", "kernel/z.ko"]
    assert all(member.pax_headers == {"KDIVE.xattrs-supported": "0"} for member in members)


@pytest.mark.parametrize(
    "name",
    [
        "lib/modules/6.12.0/../escape",
        "/lib/modules/6.12.0/kernel/a.ko",
        "lib/modules/other/kernel/a.ko",
    ],
)
def test_bundle_converter_rejects_hostile_or_cross_release_name_before_output(name: str) -> None:
    destination = io.BytesIO()
    with pytest.raises(ValueError, match="module bundle"):
        convert_kernel_bundle_modules(
            io.BytesIO(_raw_bundle([(name, b"x")])), destination, release="6.12.0"
        )
    assert destination.getvalue() == b""


def test_bundle_converter_omits_only_root_absolute_build_link() -> None:
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w:gz") as archive:
        link = tarfile.TarInfo("lib/modules/6.12.0/build")
        link.type, link.linkname = tarfile.SYMTYPE, "/build/tree"
        archive.addfile(link)
        regular = tarfile.TarInfo("lib/modules/6.12.0/kernel/a.ko")
        regular.size = 1
        archive.addfile(regular, io.BytesIO(b"a"))
    destination = io.BytesIO()
    convert_kernel_bundle_modules(io.BytesIO(source.getvalue()), destination, release="6.12.0")
    with tarfile.open(fileobj=io.BytesIO(destination.getvalue()), mode="r:") as archive:
        assert archive.getnames() == ["kernel/a.ko"]


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
        ("rollback-complete", ModuleLayout(_PRIOR, _DESIRED, None), "inactive"),
        ("new-live", ModuleLayout(_DESIRED, None, _PRIOR), "remove-old"),
        ("new-live", ModuleLayout(_DESIRED, None, None), "phase:publication-complete"),
        ("publication-complete", ModuleLayout(_DESIRED, None, None), "inactive"),
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
        target_projection_sha256="sha256:" + "d" * 64,
        target_xml=_SOURCE_XML.replace("/old", "/new"),
        source_state=state,
        target_state=state,
        prior_power="running",
        capture={"state": "absent"},
        phase=phase,
    )


def _projection() -> TargetProjectionV1:
    return TargetProjectionV1(
        ownership={"system_id": _BINDING.system_id, "run_id": _BINDING.run_id},
        plan_identity="sha256:" + "6" * 64,
        architecture="x86_64",
        cmdline="root=/dev/vda1 console=ttyS0",
        initrd_filename=None,
    )


def test_target_projection_sidecar_publishes_and_reopens_exactly(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    projection = _projection()
    with TargetProjectionStore(root) as store:
        kernel_ref = store.publish(projection)
        assert store.reopen(kernel_ref, projection.ownership) == projection
    sidecar = (
        root
        / projection.ownership.system_id
        / projection.ownership.run_id
        / projection.digest.removeprefix("sha256:")
        / "target-projection.json"
    )
    assert oct(sidecar.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("interrupted", [b"", b'{"schema":', None])
def test_target_projection_sidecar_retries_interrupted_temporary_publication(
    tmp_path: Path, interrupted: bytes | None
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    projection = _projection()
    directory = root
    for name in (
        projection.ownership.system_id,
        projection.ownership.run_id,
        projection.digest.removeprefix("sha256:"),
    ):
        directory /= name
        directory.mkdir(mode=0o700)
    temporary = directory / ".target-projection.next"
    temporary.write_bytes(projection.canonical_bytes() if interrupted is None else interrupted)
    temporary.chmod(0o600)

    with TargetProjectionStore(root) as store:
        kernel_ref = store.publish(projection)
        assert store.reopen(kernel_ref, projection.ownership) == projection

    assert not temporary.exists()


def test_target_projection_sidecar_rejects_substitution_and_cross_owner(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    projection = _projection()
    with TargetProjectionStore(root) as store:
        kernel_ref = store.publish(projection)
    sidecar = (
        root
        / projection.ownership.system_id
        / projection.ownership.run_id
        / projection.digest.removeprefix("sha256:")
        / "target-projection.json"
    )
    replacement = projection.model_copy(update={"cmdline": "root=/dev/vda2"})
    sidecar.write_bytes(replacement.canonical_bytes())
    with TargetProjectionStore(root) as store, pytest.raises(ValueError, match="digest-bound"):
        store.reopen(kernel_ref, projection.ownership)
    crossed = projection.ownership.model_copy(
        update={"run_id": "00000000-0000-0000-0000-000000000009"}
    )
    with TargetProjectionStore(root) as store, pytest.raises(ValueError, match="cross-owner"):
        store.reopen(kernel_ref, crossed)


def _pre_stop(metadata: LocalRecoveryMetadataV1) -> LocalPreStopIntentV1:
    return LocalPreStopIntentV1.model_validate(
        metadata.model_dump(
            exclude={"schema_", "source_state", "target_state", "capture", "phase"},
            by_alias=True,
        )
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


def test_pre_stop_intent_is_durable_before_complete_publication(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata()
    intent = _pre_stop(metadata)
    with RecoveryMetadataStore(root) as store:
        reference = store.publish_pre_stop(intent)
        assert store.reopen_pre_stop(reference, intent.binding) == intent
        assert store.complete_preparation(reference, intent, metadata) == metadata
        assert store.reopen(reference, intent.binding) == metadata


@pytest.mark.parametrize("interrupted", [b"", b'{"schema":', None])
@pytest.mark.parametrize("replacement", ["complete", "phase", "tombstone"])
def test_recovery_metadata_store_retries_interrupted_temporary_replacement(
    tmp_path: Path, interrupted: bytes | None, replacement: str
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered" if replacement == "tombstone" else "move-ready")
    intent = _pre_stop(metadata)
    with RecoveryMetadataStore(root) as store:
        if replacement == "complete":
            reference = store.publish_pre_stop(intent)
            expected_model = metadata
            temporary_name = ".intent.complete"
        else:
            reference = store.publish(metadata)
            if replacement == "phase":
                updated = metadata.model_copy(update={"phase": "publication-complete"})
                expected_model = updated
                temporary_name = ".intent.next"
            else:
                expected_model = CleanupTombstoneV1(
                    binding=metadata.binding,
                    point_digest=LocalLibvirtExternalBoot.point_digest(_point(metadata)),
                )
                temporary_name = ".tombstone.next"
    expected_bytes = json.dumps(
        expected_model.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    directory_name = recovery_directory_name(reference, metadata.binding)
    directory = root / (
        f".{directory_name}.partial" if replacement == "complete" else directory_name
    )
    temporary = directory / temporary_name
    temporary.write_bytes(expected_bytes if interrupted is None else interrupted)
    temporary.chmod(0o600)

    with RecoveryMetadataStore(root) as store:
        if replacement == "complete":
            assert store.complete_preparation(reference, intent, metadata) == metadata
        elif replacement == "phase":
            assert (
                store.record_phase(
                    reference,
                    metadata.binding,
                    metadata,
                    "publication-complete",
                ).phase
                == "publication-complete"
            )
        else:
            point = _point(metadata)
            tombstone = store.publish_tombstone(
                reference,
                metadata.binding,
                metadata,
                LocalLibvirtExternalBoot.point_digest(point),
            )
            assert tombstone.binding == metadata.binding

    assert not temporary.exists()


def test_pre_stop_substitution_conflicts_before_complete_publication(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata()
    intent = _pre_stop(metadata)
    with RecoveryMetadataStore(root) as store:
        reference = store.publish_pre_stop(intent)
        crossed = intent.model_copy(update={"plan_identity": "sha256:" + "e" * 64})
        with pytest.raises(ValueError, match="exact pre-stop"):
            store.publish_pre_stop(crossed)
        with pytest.raises(ValueError, match="does not extend"):
            store.complete_preparation(
                reference,
                intent,
                metadata.model_copy(update={"plan_identity": "sha256:" + "e" * 64}),
            )


def _materialization() -> ExternalBootMaterialization:
    return ExternalBootMaterialization(
        architecture="x86_64",
        provider_kind="local-libvirt",
        ownership={"system_id": _BINDING.system_id, "run_id": _BINDING.run_id},
        plan_identity="sha256:" + "6" * 64,
        extracted_vmlinuz_sha256="sha256:" + "1" * 64,
        source_module_manifest="sha256:" + "2" * 64,
        installed_module_tree="sha256:" + "3" * 64,
        verified_bundle_sha256="sha256:" + "4" * 64,
        verified_initrd_sha256=None,
        kernel_observation={
            "architecture": "x86_64",
            "release": "6.12.0",
            "gnu_build_id": "01020304",
        },
        artifacts=MaterializedArtifacts(
            kernel=OpaqueProviderRef(ref="artifacts/system/run/kernel"),
            modules=OpaqueProviderRef(ref="artifacts/system/run/modules"),
            initrd=None,
        ),
    )


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
        self.finalized_proof: FinalizeCleanupProof | None = None

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
        self.finalized_proof = proof
        self.tombstone = False


class _RealHost:
    def __init__(self, metadata: LocalRecoveryMetadataV1, root: Path) -> None:
        self.metadata = metadata
        self.root = root
        self.actions: list[str] = []
        self.inspect_allowed = True

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization:
        raise AssertionError("not used")

    def inspect_prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> LocalPreStopIntentV1:
        if not self.inspect_allowed:
            raise AssertionError("reinspected")
        self.actions.append("inspect")
        return _pre_stop(self.metadata)

    def complete_prepare(self, intent: LocalPreStopIntentV1) -> LocalRecoveryMetadataV1:
        reference = OpaqueProviderRef(
            ref=f"local-recovery-v1/{intent.binding.system_id}/{intent.binding.activation_id}"
        )
        with RecoveryMetadataStore(self.root) as store:
            assert store.reopen_pre_stop(reference, intent.binding) == intent
        self.actions.append("first-mutation")
        return self.metadata

    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("activate")

    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("target")

    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation:
        return RunningKernelObservation(
            architecture="x86_64", release=metadata.release, gnu_build_id="01020304"
        )

    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("recover")

    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("source")

    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("power")

    def cleanup_payloads(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("cleanup")


def test_real_adapter_persists_intent_before_first_host_mutation(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    host = _RealHost(metadata, root)
    io = RealLocalExternalBootIO(root, host)
    prepared = io.prepare(materialization, _BINDING, OpaqueProviderRef(ref="authority/current"))
    assert prepared == metadata
    assert host.actions == ["inspect", "first-mutation"]


def test_real_adapter_intent_fsync_fault_prevents_first_host_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    host = _RealHost(metadata, root)
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fault")))
    with pytest.raises(OSError, match="fault"):
        RealLocalExternalBootIO(root, host).prepare(
            materialization, _BINDING, OpaqueProviderRef(ref="authority/current")
        )
    assert host.actions == ["inspect"]


def test_real_adapter_retry_reopens_pre_stop_before_reinspection(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    intent = _pre_stop(metadata)
    with RecoveryMetadataStore(root) as store:
        store.publish_pre_stop(intent)
    host = _RealHost(metadata, root)
    host.inspect_allowed = False

    assert (
        RealLocalExternalBootIO(root, host).prepare(
            materialization, _BINDING, OpaqueProviderRef(ref="authority/current")
        )
        == metadata
    )
    assert host.actions == ["first-mutation"]


def test_real_adapter_retry_rejects_crossed_pre_stop_before_host_access(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    crossed = _pre_stop(metadata).model_copy(update={"plan_identity": "sha256:" + "e" * 64})
    with RecoveryMetadataStore(root) as store:
        store.publish_pre_stop(crossed)
    host = _RealHost(metadata, root)

    with pytest.raises(ValueError, match="pre-stop intent does not match"):
        RealLocalExternalBootIO(root, host).prepare(
            materialization, _BINDING, OpaqueProviderRef(ref="authority/current")
        )
    assert host.actions == []


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


def test_finalize_passes_authenticated_journal_fields_without_local_interpretation() -> None:
    io = _ExternalIO(_metadata("recovered"))
    ports = LocalLibvirtExternalBoot(io)
    point = _point(io.metadata)
    proof = FinalizeCleanupProof(
        point_digest=ports.point_digest(point),
        binding=point.binding,
        operation_id="00000000-0000-0000-0000-000000000099",
        attempt_id="00000000-0000-0000-0000-000000000098",
        journal_sequence=999,
        journal_digest="sha256:" + "f" * 64,
    )
    ports.finalize_cleanup_tombstone(
        point, proof, OpaqueProviderRef(ref="authority/authenticated-by-2140")
    )
    assert io.finalized_proof is proof

    crossed = proof.model_copy(
        update={
            "binding": proof.binding.model_copy(
                update={"activation_id": "00000000-0000-0000-0000-000000000097"}
            )
        }
    )
    with pytest.raises(ValueError, match="proof"):
        ports.finalize_cleanup_tombstone(
            point, crossed, OpaqueProviderRef(ref="authority/authenticated-by-2140")
        )
