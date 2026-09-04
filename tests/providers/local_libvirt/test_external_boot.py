"""Local-libvirt external-boot recovery state-machine tests (ADR-0586)."""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import stat
import tarfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from kdive.providers.local_libvirt.lifecycle.boot import external_boot as external_boot_module
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    CleanupTombstoneV1,
    FinalizeCleanupProof,
    LibguestfsAuthenticatedGuestTree,
    LocalLibvirtExternalBoot,
    LocalObservedState,
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
from kdive.providers.local_libvirt.lifecycle.boot.readiness import ProbeFailure, ReadinessResult
from kdive.providers.local_libvirt.lifecycle.boot.recovery import (
    AbsentModuleCapture,
    AuthenticatedGuestTree,
    KernelBundleSource,
    ModuleArchiveCapture,
    RealGuestRecoveryWriter,
    RecoveryArchiveSink,
    RecoveryArchiveSource,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    ClosedDomainInspection,
    ExpectedOperationOwnership,
    InactiveGuestDirectoryEntry,
    LocalExternalBootOperationLease,
    LocalExternalBootSessionFactory,
    OverlayIdentity,
)
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ComponentState,
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
from tests.providers.local_libvirt.external_boot_support import (
    _BINDING,
    _SOURCE_XML,
    _metadata,
    _point,
    _pre_stop,
)


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
        ("rollback-complete", ModuleLayout(_PRIOR, _DESIRED, None), "phase:move-ready"),
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


def test_publication_phase_evidence_follows_guest_sync() -> None:
    layout = ModuleLayout(_DESIRED, None, _PRIOR)
    io = _PublicationIO(layout)

    advance_module_publication(
        io,
        phase=PublicationPhase.OLD_ASIDE,
        layout=layout,
        prior=_PRIOR,
        desired=_DESIRED,
    )

    assert io.actions == ["inactive", "guest-sync", "phase:new-live"]


@pytest.mark.parametrize("prior", [_PRIOR, None])
def test_every_unlisted_present_phase_layout_pair_conflicts_without_mutation(
    prior: PresentComponentState | None,
) -> None:
    foreign = PresentComponentState(manifest="sha256:" + "f" * 64)
    values = {None, _DESIRED, foreign, prior}
    accepted = {
        (PublicationPhase.MOVE_READY, ModuleLayout(prior, _DESIRED, None)),
        (PublicationPhase.MOVE_READY, ModuleLayout(None, _DESIRED, prior)),
        (PublicationPhase.OLD_ASIDE, ModuleLayout(None, _DESIRED, prior)),
        (PublicationPhase.OLD_ASIDE, ModuleLayout(_DESIRED, None, prior)),
        (PublicationPhase.ROLLBACK_READY, ModuleLayout(None, _DESIRED, prior)),
        (PublicationPhase.ROLLBACK_READY, ModuleLayout(prior, _DESIRED, None)),
        (PublicationPhase.ROLLBACK_COMPLETE, ModuleLayout(prior, _DESIRED, None)),
        (PublicationPhase.NEW_LIVE, ModuleLayout(_DESIRED, None, prior)),
        (PublicationPhase.NEW_LIVE, ModuleLayout(_DESIRED, None, None)),
        (PublicationPhase.PUBLICATION_COMPLETE, ModuleLayout(_DESIRED, None, None)),
    }

    combinations = itertools.product(PublicationPhase, itertools.product(values, repeat=3))
    for phase, components in combinations:
        layout = ModuleLayout(*components)
        if (phase, layout) in accepted:
            continue
        publication = _PublicationIO(layout)
        with pytest.raises(ValueError, match="conflict"):
            advance_module_publication(
                publication,
                phase=phase,
                layout=layout,
                prior=prior,
                desired=_DESIRED,
            )
        assert publication.actions == ["inactive"]


def test_every_unlisted_absence_phase_layout_pair_conflicts_without_mutation() -> None:
    foreign = PresentComponentState(manifest="sha256:" + "f" * 64)
    values = {None, _PRIOR, foreign}
    accepted = {
        (PublicationPhase.MOVE_READY, ModuleLayout(_PRIOR, None, None)),
        (PublicationPhase.MOVE_READY, ModuleLayout(None, None, _PRIOR)),
        (PublicationPhase.MOVE_READY, ModuleLayout(None, None, None)),
        (PublicationPhase.ABSENCE_LIVE, ModuleLayout(None, None, _PRIOR)),
        (PublicationPhase.ABSENCE_COMPLETE, ModuleLayout(None, None, _PRIOR)),
        (PublicationPhase.ABSENCE_COMPLETE, ModuleLayout(None, None, None)),
        (PublicationPhase.ABSENCE_CLEANED, ModuleLayout(None, None, None)),
    }

    combinations = itertools.product(PublicationPhase, itertools.product(values, repeat=3))
    for phase, components in combinations:
        layout = ModuleLayout(*components)
        if (phase, layout) in accepted:
            continue
        publication = _PublicationIO(layout)
        with pytest.raises(ValueError, match="conflict"):
            advance_absence_publication(
                publication,
                phase=phase,
                layout=layout,
                prior=_PRIOR,
            )
        assert publication.actions == ["inactive"]


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


def test_staging_move_error_retries_when_recorded_source_was_absent() -> None:
    layout = ModuleLayout(None, _DESIRED, None)
    io = _PublicationIO(layout)
    calls = 0

    def fail_once() -> None:
        nonlocal calls
        calls += 1
        io.actions.append("staging-to-live")
        if calls == 1:
            raise OSError("ambiguous move result")

    io.move_staging_to_live = fail_once  # ty: ignore[invalid-assignment]

    advance_module_publication(
        io,
        phase=PublicationPhase.OLD_ASIDE,
        layout=layout,
        prior=None,
        desired=_DESIRED,
    )

    assert io.actions == [
        "inactive",
        "staging-to-live",
        "inactive",
        "observe",
        "staging-to-live",
    ]


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


@pytest.mark.parametrize("release", ["../escape", "bad/release", ".", ""])
def test_recovery_metadata_rejects_noncanonical_release(release: str) -> None:
    values = _metadata().model_dump(mode="json", by_alias=True)
    values["release"] = release
    with pytest.raises(ValidationError):
        LocalRecoveryMetadataV1.model_validate(values)


def test_recovery_metadata_binds_expected_observation_release() -> None:
    values = _metadata().model_dump(mode="json", by_alias=True)
    values["expected_running"]["release"] = "6.12.1"
    with pytest.raises(ValidationError, match="expected running release"):
        LocalRecoveryMetadataV1.model_validate(values)


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


def test_recovery_store_constructs_sink_and_source_from_exact_owned_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    template = _metadata()
    intent = _pre_stop(template)
    payload = b"bounded recovery archive"
    with RecoveryMetadataStore(root) as store:
        reference = store.publish_pre_stop(intent)
        sink = store.recovery_archive_sink(reference, intent)
        archive_sha256, archive_bytes = sink.publish(io.BytesIO(payload))
        capture = ModuleArchiveCapture(
            manifest="sha256:" + "4" * 64,
            entry_count=0,
            uncompressed_bytes=0,
            archive_sha256=archive_sha256,
            archive_bytes=archive_bytes,
        )
        metadata = template.model_copy(update={"capture": capture})
        completed = store.complete_preparation(reference, intent, metadata)
        source = store.recovery_archive_source(reference, completed)

    with source.stream() as stream:
        assert stream.read() == payload


@pytest.mark.parametrize("interrupted", [b"", b'{"schema":', None])
@pytest.mark.parametrize("publication", ["complete", "pre-stop"])
def test_recovery_metadata_store_retries_interrupted_initial_intent(
    tmp_path: Path, interrupted: bytes | None, publication: str
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata()
    intent = _pre_stop(metadata)
    reference = OpaqueProviderRef(
        ref=f"local-recovery-v1/{metadata.binding.system_id}/{metadata.binding.activation_id}"
    )
    directory = root / f".{recovery_directory_name(reference, metadata.binding)}.partial"
    directory.mkdir(mode=0o700)
    expected_model = metadata if publication == "complete" else intent
    expected_bytes = json.dumps(
        expected_model.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    temporary = directory / ".intent.initial"
    temporary.write_bytes(expected_bytes if interrupted is None else interrupted)
    temporary.chmod(0o600)

    with RecoveryMetadataStore(root) as store:
        actual = (
            store.publish(metadata) if publication == "complete" else store.publish_pre_stop(intent)
        )

    assert actual == reference
    published_directory = (
        root / recovery_directory_name(reference, metadata.binding)
        if publication == "complete"
        else directory
    )
    assert (published_directory / "intent.json").read_bytes() == expected_bytes
    assert not (published_directory / ".intent.initial").exists()


@pytest.mark.parametrize("publication", ["complete", "pre-stop"])
def test_existing_exact_initial_intent_syncs_child_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publication: str
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata()
    intent = _pre_stop(metadata)
    reference = OpaqueProviderRef(
        ref=f"local-recovery-v1/{metadata.binding.system_id}/{metadata.binding.activation_id}"
    )
    directory = root / f".{recovery_directory_name(reference, metadata.binding)}.partial"
    directory.mkdir(mode=0o700)
    expected_model = metadata if publication == "complete" else intent
    (directory / "intent.json").write_bytes(
        json.dumps(
            expected_model.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )
    (directory / "intent.json").chmod(0o600)
    directory_inode = directory.stat().st_ino
    real_fsync = os.fsync
    real_rename = os.rename
    child_synced = False

    def tracking_fsync(fd: int) -> None:
        nonlocal child_synced
        opened = os.fstat(fd)
        if stat.S_ISDIR(opened.st_mode) and opened.st_ino == directory_inode:
            child_synced = True
        real_fsync(fd)

    def checked_rename(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert child_synced
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    monkeypatch.setattr(os, "rename", checked_rename)
    with RecoveryMetadataStore(root) as store:
        actual = (
            store.publish(metadata) if publication == "complete" else store.publish_pre_stop(intent)
        )

    assert actual == reference
    assert child_synced


def test_retry_fsyncs_existing_exact_temporary_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata()
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
    updated = metadata.model_copy(update={"phase": "publication-complete"})
    directory = root / recovery_directory_name(reference, metadata.binding)
    temporary = directory / ".intent.next"
    temporary.write_bytes(
        json.dumps(
            updated.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )
    temporary.chmod(0o600)
    real_fsync = os.fsync
    real_rename = os.rename
    file_synced = False

    def tracking_fsync(fd: int) -> None:
        nonlocal file_synced
        if stat.S_ISREG(os.fstat(fd).st_mode):
            file_synced = True
        real_fsync(fd)

    def checked_rename(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert file_synced
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    monkeypatch.setattr(os, "rename", checked_rename)
    with RecoveryMetadataStore(root) as store:
        assert (
            store.record_phase(
                reference,
                metadata.binding,
                metadata,
                "publication-complete",
            )
            == updated
        )


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


def _plan() -> ExternalBootPlan:
    zero = "sha256:" + "0" * 64
    return ExternalBootPlan.model_validate(
        {
            "architecture": "x86_64",
            "bundle": {
                "decoded_kernel_size_bytes": 200,
                "elf_metadata_bytes": 50,
                "gnu_build_id_size_bytes": 20,
                "key": "bundles/k.tar",
                "member_count": 2,
                "sha256": zero,
                "uncompressed_bytes": 101,
                "version": "v1",
                "vmlinuz_sha256": zero,
                "vmlinuz_size_bytes": 100,
            },
            "cmdline": "root=UUID=x",
            "debug_cmdline": None,
            "initrd": None,
            "module_obligation": {
                "member_count": 1,
                "release": "6.12.0",
                "source_manifest": zero,
                "uncompressed_bytes": 1,
            },
            "ownership": {
                "build_generation": "00000000-0000-0000-0000-000000000001",
                "run_id": _BINDING.run_id,
                "system_id": _BINDING.system_id,
            },
            "platform_arguments": ["root=UUID=x"],
            "root": {
                "architecture": "x86_64",
                "arguments": ["root=UUID=x"],
                "authority": "stage-inspection",
                "root": "UUID=x",
                "source": {"identity": zero, "kind": "staged-image"},
            },
        }
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
        phase="mutation-started",
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


class _TreeCursor(AbstractContextManager[Iterator[InactiveGuestDirectoryEntry]]):
    def __init__(self, owner: _GuestTreeHandle, entries: list[str]) -> None:
        self._owner = owner
        self._entries = entries
        self._closed = False

    def __enter__(self) -> Iterator[InactiveGuestDirectoryEntry]:
        return iter(InactiveGuestDirectoryEntry(path) for path in self._entries)

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._owner.cursor_closes += 1


class _GuestTreeHandle:
    def __init__(
        self,
        entries: list[str] | None = None,
        *,
        present: bool = True,
        directory: bool = True,
        stats: dict[str, dict[str, int]] | None = None,
    ) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.tree_entries = ["module.ko"] if entries is None else entries
        self.present = present
        self.directory = directory
        self.stats = stats or {}
        self.cursor_closes = 0
        self.tree_limits: list[int] = []
        self.lstat_paths: list[str] = []

    def exists(self, path: str) -> int:
        return int(self.present)

    def is_dir(self, path: str, *, followsymlinks: bool) -> int:
        del path
        return int(self.directory and not followsymlinks)

    def open_tree(self, path: str, *, limit: int) -> _TreeCursor:
        self.calls.append(("open-tree", path, limit))
        self.tree_limits.append(limit)
        return _TreeCursor(self, self.tree_entries)

    def lstatns(self, path: str) -> dict[str, int]:
        self.lstat_paths.append(path)
        return self.stats.get(
            path,
            {"st_mode": 0o100600, "st_uid": 1, "st_gid": 2, "st_size": 3, "st_nlink": 1},
        )

    def readlink(self, path: str) -> str:
        raise AssertionError("not a symlink")

    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]:
        return []

    @contextmanager
    def open_regular(self, path: str, *, size: int) -> Iterator[BinaryIO]:
        del size
        self.calls.append(("open-regular", path))
        yield io.BytesIO(b"elf")

    def create_regular(self, content: BinaryIO, path: str, *, size: int) -> None:
        self.calls.append(("create-regular", path, content.read(size)))

    def download(self, remotefilename: str, filename: str) -> None:
        self.calls.append(("download", remotefilename))
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

    def mv(self, source: str, destination: str) -> None:
        self.calls.append(("move", source, destination))

    def rm_rf(self, path: str) -> None:
        self.calls.append(("remove", path))

    def sync(self) -> None:
        self.calls.append(("sync",))


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
    assert guest.tree_limits == [external_boot_module.MAX_ENTRIES]
    assert guest.cursor_closes == 1


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


def _capture_guest_tree(
    tmp_path: Path,
    guest: _GuestTreeHandle,
    *,
    directory_name: str,
) -> AbsentModuleCapture | ModuleArchiveCapture:
    archive = tmp_path / directory_name
    archive.mkdir(mode=0o700)
    descriptor = os.open(archive, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        sink = RecoveryArchiveSink(
            descriptor,
            binding=_BINDING,
            release="6.12.0",
        )
    finally:
        os.close(descriptor)
    tree = LibguestfsAuthenticatedGuestTree(
        guest,
        binding=_BINDING,
        release="6.12.0",
        root="/lib/modules/6.12.0",
        mutable=False,
    )
    return RealGuestRecoveryWriter().capture(tree, "6.12.0", sink)


def test_libguestfs_tree_capture_distinguishes_present_empty_and_absent(
    tmp_path: Path,
) -> None:
    present = _capture_guest_tree(
        tmp_path,
        _GuestTreeHandle(["module.ko"]),
        directory_name="present",
    )
    empty_guest = _GuestTreeHandle([])
    empty = _capture_guest_tree(tmp_path, empty_guest, directory_name="empty")
    absent_guest = _GuestTreeHandle([], present=False)
    absent = _capture_guest_tree(tmp_path, absent_guest, directory_name="absent")

    assert isinstance(present, ModuleArchiveCapture) and present.entry_count == 1
    assert isinstance(empty, ModuleArchiveCapture) and empty.entry_count == 0
    assert isinstance(absent, AbsentModuleCapture)
    assert empty_guest.cursor_closes == 1
    assert absent_guest.tree_limits == []


def test_libguestfs_tree_order_produces_identical_recovery_identity(tmp_path: Path) -> None:
    root = "/lib/modules/6.12.0"
    stats = {
        f"{root}/kernel": {
            "st_mode": stat.S_IFDIR | 0o755,
            "st_uid": 0,
            "st_gid": 0,
            "st_size": 0,
            "st_nlink": 1,
        }
    }
    first = _capture_guest_tree(
        tmp_path,
        _GuestTreeHandle(["z.ko", "kernel", "kernel/a.ko"], stats=stats),
        directory_name="first",
    )
    second = _capture_guest_tree(
        tmp_path,
        _GuestTreeHandle(["kernel/a.ko", "z.ko", "kernel"], stats=stats),
        directory_name="second",
    )

    assert isinstance(first, ModuleArchiveCapture)
    assert isinstance(second, ModuleArchiveCapture)
    assert first.manifest == second.manifest


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        (["same", "same"], "duplicate"),
        (["../escape"], "canonical relative"),
        (["bad//name"], "canonical relative"),
    ],
)
def test_libguestfs_tree_rejects_duplicate_or_hostile_names_before_second_visit(
    tmp_path: Path,
    entries: list[str],
    reason: str,
) -> None:
    guest = _GuestTreeHandle(entries)

    with pytest.raises(ValueError, match=reason):
        _capture_guest_tree(tmp_path, guest, directory_name=reason.replace(" ", "-"))

    expected_visits = 1 if reason == "duplicate" else 0
    assert len(guest.lstat_paths) == expected_visits
    assert guest.cursor_closes == 1


def test_libguestfs_tree_rejects_hard_link_before_content_read(tmp_path: Path) -> None:
    root = "/lib/modules/6.12.0"
    guest = _GuestTreeHandle(
        ["module.ko"],
        stats={
            f"{root}/module.ko": {
                "st_mode": stat.S_IFREG | 0o600,
                "st_uid": 1,
                "st_gid": 2,
                "st_size": 3,
                "st_nlink": 2,
            }
        },
    )

    with pytest.raises(ValueError, match="hard-linked"):
        _capture_guest_tree(tmp_path, guest, directory_name="hard-link")

    assert not any(call[0] == "download" for call in guest.calls)


def test_libguestfs_tree_rejects_limit_signal_before_visiting_extra_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(external_boot_module, "MAX_ENTRIES", 2)
    guest = _GuestTreeHandle(["a", "b", "not-visited"])

    with pytest.raises(ValueError, match="entry-count"):
        _capture_guest_tree(tmp_path, guest, directory_name="over-limit")

    assert guest.tree_limits == [2]
    assert guest.lstat_paths == ["/lib/modules/6.12.0/a", "/lib/modules/6.12.0/b"]
    assert guest.cursor_closes == 1


class _ExternalIO:
    def __init__(
        self,
        metadata: LocalRecoveryMetadataV1,
        *,
        operation_fault: bool = False,
        close_fault: bool = False,
    ) -> None:
        self.metadata = metadata
        self.actions: list[str] = []
        self.tombstone = False
        self.finalized_proof: FinalizeCleanupProof | None = None
        self.operation_fault = operation_fault
        self.close_fault = close_fault
        self.opened: list[ExpectedOperationOwnership] = []
        self.close_attempts = 0

    def open(
        self,
        authority: OpaqueProviderRef,
        expected: ExpectedOperationOwnership,
    ) -> _ExternalContext:
        assert authority == OpaqueProviderRef(ref="authority/current")
        self.opened.append(expected)
        return _ExternalContext(self)

    def materialize(self, plan: ExternalBootPlan) -> ExternalBootMaterialization:
        self.actions.append("materialize")
        if self.operation_fault:
            raise LookupError("operation primary")
        materialization = _materialization()
        return materialization.model_copy(update={"plan_identity": plan.identity})

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
    ) -> LocalRecoveryMetadataV1:
        self.actions.append("prepare")
        if self.operation_fault:
            raise LookupError("operation primary")
        return self.metadata.model_copy(
            update={
                "binding": binding,
                "materialization_identity": materialization.identity,
                "plan_identity": materialization.plan_identity,
            }
        )

    def recovery_ref(self, binding: ExternalBootActivationBinding) -> OpaqueProviderRef:
        return _point(self.metadata).recovery_ref

    def reopen(self, recovery: RecoveryPoint) -> LocalRecoveryMetadataV1:
        self.actions.append("reopen")
        if self.operation_fault:
            raise LookupError("operation primary")
        return self.metadata

    def reopen_binding(self, binding: ExternalBootActivationBinding) -> LocalRecoveryMetadataV1:
        del binding
        self.actions.append("reopen")
        if self.operation_fault:
            raise LookupError("operation primary")
        return self.metadata

    def observe_state(self, metadata: LocalRecoveryMetadataV1) -> LocalObservedState:
        self.actions.append("observe-state")
        if self.operation_fault:
            raise LookupError("operation primary")
        return LocalObservedState(
            definition=metadata.source_state.definition,
            modules=metadata.source_state.modules,
            active=False,
        )

    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("activate-modules")
        self.record_phase(metadata, "module-restored")

    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("define-target")
        self.record_phase(metadata, "target-defined")

    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation:
        self.actions.append("observe-running")
        return RunningKernelObservation(
            architecture="x86_64", release=metadata.release, gnu_build_id="01020304"
        )

    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("recover-modules")
        self.record_phase(metadata, "module-restored")

    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("define-source")
        self.record_phase(metadata, "source-restored")

    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("restore-power")
        self.record_phase(metadata, "recovered")

    def record_phase(
        self, metadata: LocalRecoveryMetadataV1, phase: str
    ) -> LocalRecoveryMetadataV1:
        self.actions.append(f"phase:{phase}")
        self.metadata = metadata.model_copy(update={"phase": phase})
        return self.metadata

    def cleanup_complete(self, recovery: RecoveryPoint) -> bool:
        return self.tombstone

    def cleanup(self, metadata: LocalRecoveryMetadataV1, point_digest: str) -> None:
        self.actions.append("cleanup")
        self.tombstone = True

    def finalize_tombstone(self, recovery: RecoveryPoint, proof: FinalizeCleanupProof) -> None:
        self.actions.append("finalize")
        self.finalized_proof = proof
        self.tombstone = False


class _ExternalContext:
    def __init__(self, operation: _ExternalIO) -> None:
        self.operation = operation

    def __enter__(self) -> _ExternalIO:
        return self.operation

    def __exit__(self, exc_type: object, exc: BaseException | None, traceback: object) -> None:
        del exc_type, traceback
        self.operation.close_attempts += 1
        if self.operation.close_fault:
            close_error = OSError("close secondary")
            if exc is None:
                raise close_error
            exc.add_note(f"cleanup failed: {close_error!r}")


def _exercise_port(method: str, ports: LocalLibvirtExternalBoot, io: _ExternalIO) -> None:
    authority = OpaqueProviderRef(ref="authority/current")
    point = _point(io.metadata)
    if method == "materialize":
        ports.materialize(_plan(), authority)
    elif method == "prepare":
        materialization = _materialization().model_copy(
            update={"plan_identity": io.metadata.plan_identity}
        )
        ports.prepare(materialization, io.metadata.binding, authority)
    elif method == "activate":
        ports.activate(point, authority)
    elif method == "observe":
        io.metadata = io.metadata.model_copy(update={"phase": "target-defined"})
        ports.observe(_point(io.metadata), authority)
    elif method == "recover":
        io.metadata = io.metadata.model_copy(update={"phase": "target-defined"})
        ports.recover(_point(io.metadata), authority)
    else:
        io.metadata = io.metadata.model_copy(update={"phase": "recovered"})
        ports.cleanup(_point(io.metadata), authority)


@pytest.mark.parametrize(
    "method", ["materialize", "prepare", "activate", "observe", "recover", "cleanup"]
)
def test_real_adapter_opens_and_closes_one_operation_per_public_call(method: str) -> None:
    io = _ExternalIO(_metadata())

    _exercise_port(method, LocalLibvirtExternalBoot(io), io)

    assert len(io.opened) == 1
    assert io.close_attempts == 1


@pytest.mark.parametrize(
    "method", ["materialize", "prepare", "activate", "observe", "recover", "cleanup"]
)
def test_real_adapter_preserves_operation_error_when_close_also_fails(method: str) -> None:
    io = _ExternalIO(_metadata(), operation_fault=True, close_fault=True)

    with pytest.raises(LookupError, match="operation primary") as raised:
        _exercise_port(method, LocalLibvirtExternalBoot(io), io)

    assert raised.value.__notes__ == ["cleanup failed: OSError('close secondary')"]
    assert len(io.opened) == 1
    assert io.close_attempts == 1


@pytest.mark.parametrize(
    "method", ["materialize", "prepare", "activate", "observe", "recover", "cleanup"]
)
def test_real_adapter_surfaces_close_fault_after_success(method: str) -> None:
    io = _ExternalIO(_metadata(), close_fault=True)

    with pytest.raises(OSError, match="close secondary"):
        _exercise_port(method, LocalLibvirtExternalBoot(io), io)

    assert len(io.opened) == 1
    assert io.close_attempts == 1


@pytest.mark.parametrize(
    "method", ["materialize", "prepare", "activate", "observe", "recover", "cleanup"]
)
def test_real_adapter_closes_operation_on_coordinator_validation_failure(method: str) -> None:
    io = _ExternalIO(_metadata())
    ports = LocalLibvirtExternalBoot(io)
    authority = OpaqueProviderRef(ref="authority/current")
    point = _point(io.metadata)

    with pytest.raises(ValueError):
        if method == "materialize":
            plan = _plan()
            original = io.materialize
            io.materialize = lambda value: original(value).model_copy(  # ty: ignore[invalid-assignment]
                update={"provider_kind": "foreign"}
            )
            ports.materialize(plan, authority)
        elif method == "prepare":
            materialization = _materialization().model_copy(
                update={
                    "ownership": _materialization().ownership.model_copy(
                        update={"system_id": str(UUID(int=9))}
                    )
                }
            )
            ports.prepare(materialization, _BINDING, authority)
        elif method == "activate":
            crossed = point.model_copy(update={"plan_identity": "sha256:" + "f" * 64})
            ports.activate(crossed, authority)
        elif method == "observe":
            ports.observe(point, authority)
        elif method == "recover":
            ports.recover(point, authority)
        else:
            ports.cleanup(point, authority)

    assert len(io.opened) == 1
    assert io.close_attempts == 1


class _RealPreparation:
    def __init__(self, metadata: LocalRecoveryMetadataV1, root: Path) -> None:
        self.metadata = metadata
        self.root = root
        self.actions: list[str] = []
        self.inspect_allowed = True
        self.work_fault = False

    def materialize(self, plan: ExternalBootPlan, session: object) -> ExternalBootMaterialization:
        del plan, session
        raise AssertionError("not used")

    def inspect_prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        inspection: ClosedDomainInspection,
        session: object,
    ) -> LocalPreStopIntentV1:
        del materialization, binding, inspection, session
        if not self.inspect_allowed:
            raise AssertionError("reinspected")
        self.actions.append("inspect")
        return _pre_stop(self.metadata)


class _RealSession:
    def __init__(self, preparation: _RealPreparation) -> None:
        self.preparation = preparation
        self.close_attempts = 0
        self.close_fault = False
        self.guest_fault = False
        self.inspection = ClosedDomainInspection(
            xml=preparation.metadata.source_xml.encode(),
            active=preparation.metadata.prior_power == "running",
            definition_identity=preparation.metadata.source_definition,
            source_boot_identity=preparation.metadata.source_boot,
            domain_name=f"kdive-{preparation.metadata.binding.system_id}",
            overlay=OverlayIdentity(1, 2),
        )
        self.guest_handle = _GuestTreeHandle([], present=False)

    def inspect_closed(self) -> ClosedDomainInspection:
        return self.inspection

    def stop_and_require_inactive(self) -> None:
        reference = _point(self.preparation.metadata).recovery_ref
        with RecoveryMetadataStore(self.preparation.root) as store:
            store.reopen_pre_stop(reference, self.preparation.metadata.binding)
        self.preparation.actions.append("first-mutation")

    @contextmanager
    def guest(self) -> Iterator[_GuestTreeHandle]:
        if self.guest_fault:
            raise LookupError("guest open primary")
        yield self.guest_handle

    def observe_running(self) -> RunningKernelObservation:
        return RunningKernelObservation(
            architecture="x86_64",
            release=self.preparation.metadata.release,
            gnu_build_id="01020304",
        )

    def define_xml(self, xml: str) -> None:
        self.preparation.actions.append(f"define:{xml}")

    def restore_power(self, prior: str) -> None:
        self.preparation.actions.append(f"power:{prior}")

    def cleanup_payloads(self) -> None:
        self.preparation.actions.append("cleanup")

    def close(self) -> None:
        self.close_attempts += 1
        if self.close_fault:
            raise OSError("session close")


class _ProcessLost(BaseException):
    pass


class _RestartFaults:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.failures: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def run(self, name: str, effect: Callable[[], object] | None = None) -> object | None:
        occurrence = self.counts.get(name, 0) + 1
        self.counts[name] = occurrence
        action = f"{name}#{occurrence}"
        self.actions.append(action)
        failure = self.failures.pop(action, None)
        if failure == "os-before":
            raise OSError(f"{action} failed before effect")
        if failure == "before":
            raise _ProcessLost(f"{action} failed before effect")
        result = effect() if effect is not None else None
        if failure == "os-after":
            raise OSError(f"{action} failed after effect")
        if failure == "after":
            raise _ProcessLost(f"{action} failed after effect")
        return result


class _RestartGuest(_GuestTreeHandle):
    def __init__(
        self,
        faults: _RestartFaults,
        states: dict[str, PresentComponentState],
    ) -> None:
        super().__init__([])
        self.faults = faults
        self.states = states

    def exists(self, path: str) -> int:
        return int(path in self.states)

    def is_dir(self, path: str, *, followsymlinks: bool) -> int:
        del followsymlinks
        return int(path in self.states)

    def mkdir(self, path: str) -> None:
        self.faults.run("mkdir")

    def mv(self, source: str, destination: str) -> None:
        def effect() -> None:
            self.states[destination] = self.states.pop(source)

        self.faults.run(f"move:{Path(source).name}->{Path(destination).name}", effect)

    def rm_rf(self, path: str) -> None:
        self.faults.run(f"remove:{Path(path).name}", lambda: self.states.pop(path, None))

    def sync(self) -> None:
        self.faults.run("sync")


class _RestartWriter:
    def __init__(
        self,
        guest: _RestartGuest,
        *,
        install_state: PresentComponentState,
        restore_state: PresentComponentState | None,
    ) -> None:
        self._guest = guest
        self._install_state = install_state
        self._restore_state = restore_state

    def capture(
        self,
        tree: AuthenticatedGuestTree,
        release: str,
        sink: RecoveryArchiveSink,
    ) -> AbsentModuleCapture:
        del tree, release
        sink.close()
        raise AssertionError("not used")

    @staticmethod
    def _root(tree: AuthenticatedGuestTree) -> str:
        return cast(str, vars(tree)["_root"])

    def observe(self, tree: AuthenticatedGuestTree, release: str) -> ComponentState:
        del release
        return self._guest.states[self._root(tree)]

    def install(
        self,
        tree: AuthenticatedGuestTree,
        release: str,
        source: KernelBundleSource,
    ) -> str:
        del release
        source.close()
        self._guest.faults.run(
            "install",
            lambda: self._guest.states.__setitem__(self._root(tree), self._install_state),
        )
        return self._install_state.manifest

    def restore(
        self,
        tree: AuthenticatedGuestTree,
        release: str,
        capture: AbsentModuleCapture | ModuleArchiveCapture,
        source: RecoveryArchiveSource,
    ) -> str:
        del release, capture
        source.close()
        restore_state = self._restore_state
        assert restore_state is not None
        self._guest.faults.run(
            "restore",
            lambda: self._guest.states.__setitem__(self._root(tree), restore_state),
        )
        return restore_state.manifest


class _RestartSession(_RealSession):
    def __init__(
        self,
        preparation: _RealPreparation,
        guest: _RestartGuest,
        artifact: Path,
    ) -> None:
        super().__init__(preparation)
        self.guest_handle = guest
        self.faults = guest.faults
        self.xml = preparation.metadata.source_xml
        self.active = False
        self.artifact = artifact
        self.readiness_result = ReadinessResult(True, True, None)
        self.running_observation = RunningKernelObservation(
            architecture="x86_64",
            release=preparation.metadata.release,
            gnu_build_id="01020304",
        )

    def inspect_closed(self) -> ClosedDomainInspection:
        return replace(
            self.inspection,
            xml=self.xml.encode(),
            active=self.active,
        )

    def require_inactive(self) -> None:
        if self.active:
            raise RuntimeError("domain must be inactive")

    def stop_and_require_inactive(self) -> None:
        self.faults.run("stop", lambda: setattr(self, "active", False))
        self.require_inactive()

    def open_artifact(self, name: str, flags: int, mode: int = 0o600) -> int:
        del name, mode
        return os.open(self.artifact, flags)

    @contextmanager
    def guest(self) -> Iterator[_RestartGuest]:
        self.require_inactive()
        yield cast(_RestartGuest, self.guest_handle)

    def define_xml(self, xml: str) -> None:
        self.require_inactive()
        self.faults.run("define", lambda: setattr(self, "xml", xml))

    def start(self) -> None:
        self.faults.run("start", lambda: setattr(self, "active", True))

    def readiness(self) -> ReadinessResult:
        self.faults.run("readiness")
        return self.readiness_result

    def observe_running(self) -> RunningKernelObservation:
        return self.running_observation

    def cleanup_payloads(self) -> None:
        self.faults.run("cleanup-payloads", lambda: _RealSession.cleanup_payloads(self))


def _restart_fixture(
    tmp_path: Path,
    *,
    phase: RecoveryPhase,
    source_present: bool,
    prior_power: str = "running",
    xml: str = _SOURCE_XML,
    active: bool = False,
) -> tuple[
    LocalLibvirtExternalBoot,
    LocalRecoveryMetadataV1,
    _RestartSession,
    _RestartGuest,
    Path,
]:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    artifact = tmp_path / "modules"
    artifact.write_bytes(b"bundle")
    artifact.chmod(0o600)
    target = PresentComponentState(manifest="sha256:" + "3" * 64)
    source = PresentComponentState(manifest="sha256:" + "4" * 64)
    capture: AbsentModuleCapture | ModuleArchiveCapture
    if source_present:
        archive = b"recovery"
        capture = ModuleArchiveCapture(
            manifest=source.manifest,
            entry_count=0,
            uncompressed_bytes=0,
            archive_sha256="sha256:" + hashlib.sha256(archive).hexdigest(),
            archive_bytes=len(archive),
        )
        source_modules: ComponentState = source
    else:
        archive = b""
        capture = AbsentModuleCapture()
        source_modules = AbsentComponentState()
    metadata = _metadata(phase).model_copy(
        update={
            "capture": capture,
            "materialized_modules": OpaqueProviderRef(
                ref=(f"local-artifact-v1/{_BINDING.system_id}/{_BINDING.run_id}/{'a' * 64}/modules")
            ),
            "materialized_modules_sha256": (
                "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
            ),
            "materialized_modules_bytes": artifact.stat().st_size,
            "prior_power": prior_power,
            "source_state": ProviderStateIdentity(
                definition=_metadata().source_state.definition,
                modules=source_modules,
            ),
            "target_state": ProviderStateIdentity(
                definition=_metadata().target_state.definition,
                modules=target,
            ),
        }
    )
    live = f"/lib/modules/{metadata.release}"
    target_live = phase in {"module-restored", "target-defined"}
    states = {live: target if target_live else source}
    if not source_present and not target_live:
        states = {}
    faults = _RestartFaults()
    guest = _RestartGuest(faults, states)
    preparation = _RealPreparation(metadata, root)
    session = _RestartSession(preparation, guest, artifact)
    session.xml = xml
    session.active = active
    writer = _RestartWriter(
        guest,
        install_state=target,
        restore_state=source if source_present else None,
    )
    factory = cast(LocalExternalBootSessionFactory, _RealSessionFactory(session))
    io = RealLocalExternalBootIO(
        root,
        preparation,
        writer,
        lambda _authority: cast(LocalExternalBootOperationLease, object()),
        factory,
    )
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
    if source_present:
        directory = root / recovery_directory_name(reference, metadata.binding)
        recovery_archive = directory / "modules.tar"
        recovery_archive.write_bytes(archive)
        recovery_archive.chmod(0o600)
    return LocalLibvirtExternalBoot(io), metadata, session, guest, root


class _FreshRestartHarness:
    def __init__(
        self,
        ports: LocalLibvirtExternalBoot,
        metadata: LocalRecoveryMetadataV1,
        session: _RestartSession,
        guest: _RestartGuest,
        root: Path,
    ) -> None:
        self._ports = ports
        self.metadata = metadata
        self.sessions = [session]
        self.guest = guest
        self.root = root
        self._invocations = 0

    @classmethod
    def create(
        cls,
        tmp_path: Path,
        *,
        phase: RecoveryPhase,
        source_present: bool,
        prior_power: str = "running",
        xml: str = _SOURCE_XML,
        active: bool = False,
    ) -> _FreshRestartHarness:
        return cls(
            *_restart_fixture(
                tmp_path,
                phase=phase,
                source_present=source_present,
                prior_power=prior_power,
                xml=xml,
                active=active,
            )
        )

    @property
    def faults(self) -> _RestartFaults:
        return self.guest.faults

    @property
    def point(self) -> RecoveryPoint:
        return _point(self.metadata)

    def activate(self, point: RecoveryPoint | None = None) -> None:
        ports = self._fresh_ports()
        ports.activate(point or self.point, OpaqueProviderRef(ref="authority/current"))

    def recover(self) -> None:
        ports = self._fresh_ports()
        ports.recover(self.point, OpaqueProviderRef(ref="authority/current"))

    def cleanup(self) -> None:
        ports = self._fresh_ports()
        ports.cleanup(self.point, OpaqueProviderRef(ref="authority/current"))

    def finalize(self, proof: FinalizeCleanupProof) -> None:
        ports = self._fresh_ports()
        ports.finalize_cleanup_tombstone(
            self.point,
            proof,
            OpaqueProviderRef(ref="authority/authenticated-by-2140"),
        )

    @property
    def session(self) -> _RestartSession:
        return self.sessions[-1]

    def discard_process(self) -> None:
        self._invocations = max(self._invocations, 1)

    def _fresh_ports(self) -> LocalLibvirtExternalBoot:
        if self._invocations == 0:
            self._invocations += 1
            return self._ports
        previous = self.sessions[-1]
        session = _RestartSession(previous.preparation, self.guest, previous.artifact)
        session.xml = previous.xml
        session.active = previous.active
        session.readiness_result = previous.readiness_result
        session.running_observation = previous.running_observation
        writer = _RestartWriter(
            self.guest,
            install_state=cast(PresentComponentState, self.metadata.target_state.modules),
            restore_state=(
                self.metadata.source_state.modules
                if isinstance(self.metadata.source_state.modules, PresentComponentState)
                else None
            ),
        )
        io = RealLocalExternalBootIO(
            self.root,
            previous.preparation,
            writer,
            lambda _authority: cast(LocalExternalBootOperationLease, object()),
            cast(LocalExternalBootSessionFactory, _RealSessionFactory(session)),
        )
        self.sessions.append(session)
        self._invocations += 1
        return LocalLibvirtExternalBoot(io)


def test_crash_retry_recreates_real_adapter_from_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="pre-stop-intent",
        source_present=True,
    )
    _record_phase_faults(monkeypatch, harness.faults)
    harness.faults.failures["phase:move-ready#1"] = "after"

    with pytest.raises(_ProcessLost, match="phase:move-ready#1 failed after effect"):
        harness.activate()
    harness.activate()

    assert len(harness.sessions) == 2
    assert harness.sessions[0] is not harness.sessions[1]
    with RecoveryMetadataStore(harness.root) as store:
        assert store.reopen(harness.point.recovery_ref, harness.metadata.binding).phase == (
            "target-defined"
        )


def _recovery_root_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize(
    "crossed",
    [
        lambda point: point.model_copy(update={"plan_identity": "sha256:" + "f" * 64}),
        lambda point: point.model_copy(
            update={
                "binding": point.binding.model_copy(
                    update={"system_id": "00000000-0000-0000-0000-000000000099"}
                )
            }
        ),
        lambda point: point.model_copy(
            update={
                "binding": point.binding.model_copy(
                    update={"run_id": "00000000-0000-0000-0000-000000000099"}
                )
            }
        ),
        lambda point: point.model_copy(
            update={
                "binding": point.binding.model_copy(
                    update={"activation_id": "00000000-0000-0000-0000-000000000099"}
                )
            }
        ),
    ],
)
def test_fresh_adapter_rejects_crossed_point_before_mutation(
    tmp_path: Path,
    crossed: Callable[[RecoveryPoint], RecoveryPoint],
) -> None:
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="pre-stop-intent",
        source_present=True,
    )
    harness.discard_process()
    before_root = _recovery_root_snapshot(harness.root)
    before_guest = dict(harness.guest.states)

    with pytest.raises((FileNotFoundError, ValueError)):
        harness.activate(crossed(harness.point))

    assert _recovery_root_snapshot(harness.root) == before_root
    assert harness.guest.states == before_guest
    assert harness.faults.actions == []


@pytest.mark.parametrize("substituted_name", ["live", "staging", "old"])
def test_fresh_adapter_rejects_substituted_three_name_layout_without_mutation(
    tmp_path: Path,
    substituted_name: str,
) -> None:
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="move-ready",
        source_present=True,
    )
    harness.discard_process()
    live = f"/lib/modules/{harness.metadata.release}"
    names = {
        "live": live,
        "staging": f"/lib/modules/{_STAGING_NAME}",
        "old": f"/lib/modules/{_OLD_NAME}",
    }
    harness.guest.states[names[substituted_name]] = PresentComponentState(
        manifest="sha256:" + "f" * 64
    )
    before_root = _recovery_root_snapshot(harness.root)
    before_guest = dict(harness.guest.states)

    with pytest.raises(ValueError, match="conflict"):
        harness.activate()

    assert _recovery_root_snapshot(harness.root) == before_root
    assert harness.guest.states == before_guest
    assert harness.faults.actions == []


def test_fresh_adapter_rejects_malformed_durable_evidence_before_mutation(
    tmp_path: Path,
) -> None:
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="pre-stop-intent",
        source_present=True,
    )
    harness.discard_process()
    evidence = harness.root / recovery_directory_name(
        harness.point.recovery_ref, harness.metadata.binding
    )
    (evidence / "intent.json").write_bytes(b"{")
    before_root = _recovery_root_snapshot(harness.root)
    before_guest = dict(harness.guest.states)

    with pytest.raises(ValueError):
        harness.activate()

    assert _recovery_root_snapshot(harness.root) == before_root
    assert harness.guest.states == before_guest
    assert harness.faults.actions == []


@pytest.mark.parametrize("source_present", [True, False])
def test_real_activation_publishes_exact_target_and_restores_prior_power(
    tmp_path: Path,
    source_present: bool,
) -> None:
    ports, metadata, session, guest, root = _restart_fixture(
        tmp_path,
        phase="pre-stop-intent",
        source_present=source_present,
    )

    ports.activate(_point(metadata), OpaqueProviderRef(ref="authority/current"))

    live = f"/lib/modules/{metadata.release}"
    assert guest.states == {live: metadata.target_state.modules}
    assert session.xml == metadata.target_xml
    assert session.active
    with RecoveryMetadataStore(root) as store:
        reopened = store.reopen(_point(metadata).recovery_ref, metadata.binding)
        assert reopened.phase == "target-defined"


@pytest.mark.parametrize("source_present", [True, False])
def test_recovery_from_activation_module_phase_restores_exact_source_before_power(
    tmp_path: Path,
    source_present: bool,
) -> None:
    ports, metadata, session, guest, root = _restart_fixture(
        tmp_path,
        phase="module-restored",
        source_present=source_present,
    )

    ports.recover(_point(metadata), OpaqueProviderRef(ref="authority/current"))

    live = f"/lib/modules/{metadata.release}"
    expected = {live: metadata.source_state.modules} if source_present else {}
    assert guest.states == expected
    assert session.xml == metadata.source_xml
    assert session.active
    with RecoveryMetadataStore(root) as store:
        assert store.reopen(_point(metadata).recovery_ref, metadata.binding).phase == "recovered"


def _record_phase_faults(
    monkeypatch: pytest.MonkeyPatch,
    faults: _RestartFaults,
) -> None:
    original = RecoveryMetadataStore.record_phase

    def record(
        store: RecoveryMetadataStore,
        reference: OpaqueProviderRef,
        binding: ExternalBootActivationBinding,
        expected: LocalRecoveryMetadataV1,
        phase: RecoveryPhase,
    ) -> LocalRecoveryMetadataV1:
        result = faults.run(
            f"phase:{phase}",
            lambda: original(store, reference, binding, expected, phase),
        )
        assert isinstance(result, LocalRecoveryMetadataV1)
        return result

    monkeypatch.setattr(RecoveryMetadataStore, "record_phase", record)


def _retry_until_phase(
    operation: Callable[[], None],
    root: Path,
    metadata: LocalRecoveryMetadataV1,
    phase: RecoveryPhase,
) -> None:
    for _attempt in range(4):
        process_lost = False
        try:
            operation()
        except _ProcessLost:
            process_lost = True
        with RecoveryMetadataStore(root) as store:
            if (
                store.reopen(_point(metadata).recovery_ref, metadata.binding).phase == phase
                and not process_lost
            ):
                return
    raise AssertionError(f"external boot did not reach {phase}")


_OLD_NAME = f".kdive-{_BINDING.activation_id}-old"
_STAGING_NAME = f".kdive-{_BINDING.activation_id}-staging"

_PRESENT_PUBLICATION_FAULTS = [
    "sync#1",
    "phase:move-ready#1",
    "move:6.12.0->" + _OLD_NAME + "#1",
    "sync#2",
    "phase:old-aside#1",
    "move:" + _STAGING_NAME + "->6.12.0#1",
    "sync#3",
    "phase:new-live#1",
    "remove:" + _OLD_NAME + "#1",
    "sync#4",
    "phase:publication-complete#1",
]
_PRESENT_ACTIVATION_FAULTS = ["install#1"] + _PRESENT_PUBLICATION_FAULTS


@pytest.mark.parametrize("effect", ["before", "after"])
@pytest.mark.parametrize(
    "fault",
    _PRESENT_ACTIVATION_FAULTS
    + [
        "phase:module-restored#1",
        "define#1",
        "start#1",
        "readiness#1",
        "phase:target-defined#1",
    ],
)
def test_activation_restarts_exactly_around_every_publication_and_host_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    effect: str,
) -> None:
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="pre-stop-intent",
        source_present=True,
    )
    _record_phase_faults(monkeypatch, harness.faults)
    harness.faults.failures[fault] = effect

    _retry_until_phase(harness.activate, harness.root, harness.metadata, "target-defined")

    live = f"/lib/modules/{harness.metadata.release}"
    assert harness.guest.states == {live: harness.metadata.target_state.modules}
    assert harness.session.xml == harness.metadata.target_xml
    assert harness.session.active
    assert len(harness.sessions) >= 2


@pytest.mark.parametrize("effect", ["before", "after"])
@pytest.mark.parametrize(
    "fault",
    [
        "install#1",
        "sync#1",
        "phase:old-aside#1",
        "move:" + _STAGING_NAME + "->6.12.0#1",
        "sync#2",
        "phase:new-live#1",
        "sync#3",
        "phase:publication-complete#1",
        "phase:module-restored#1",
        "define#1",
        "start#1",
        "readiness#1",
        "phase:target-defined#1",
    ],
)
def test_absent_source_activation_restarts_around_every_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    effect: str,
) -> None:
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="pre-stop-intent",
        source_present=False,
    )
    _record_phase_faults(monkeypatch, harness.faults)
    harness.faults.failures[fault] = effect

    _retry_until_phase(harness.activate, harness.root, harness.metadata, "target-defined")

    live = f"/lib/modules/{harness.metadata.release}"
    assert harness.guest.states == {live: harness.metadata.target_state.modules}
    assert harness.session.xml == harness.metadata.target_xml
    assert harness.session.active
    assert len(harness.sessions) >= 2


@pytest.mark.parametrize("effect", ["before", "after"])
@pytest.mark.parametrize(
    "fault",
    ["stop#1", "restore#1"]
    + _PRESENT_PUBLICATION_FAULTS
    + [
        "phase:module-restored#1",
        "define#1",
        "phase:source-restored#1",
        "start#1",
        "readiness#1",
        "phase:recovered#1",
    ],
)
def test_present_recovery_restarts_exactly_around_every_mutation_and_evidence_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    effect: str,
) -> None:
    metadata_template = _metadata("target-defined")
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="target-defined",
        source_present=True,
        xml=metadata_template.target_xml,
        active=True,
    )
    _record_phase_faults(monkeypatch, harness.faults)
    harness.faults.failures[fault] = effect

    _retry_until_phase(harness.recover, harness.root, harness.metadata, "recovered")

    live = f"/lib/modules/{harness.metadata.release}"
    assert harness.guest.states == {live: harness.metadata.source_state.modules}
    assert harness.session.xml == harness.metadata.source_xml
    assert harness.session.active
    assert len(harness.sessions) >= 2


_ABSENCE_PUBLICATION_FAULTS = [
    "sync#1",
    "phase:move-ready#1",
    "move:6.12.0->" + _OLD_NAME + "#1",
    "sync#2",
    "phase:absence-live#1",
    "phase:absence-complete#1",
    "remove:" + _OLD_NAME + "#1",
    "sync#3",
    "phase:absence-cleaned#1",
]


@pytest.mark.parametrize("effect", ["before", "after"])
@pytest.mark.parametrize(
    "fault",
    ["stop#1"]
    + _ABSENCE_PUBLICATION_FAULTS
    + [
        "phase:module-restored#1",
        "define#1",
        "phase:source-restored#1",
        "start#1",
        "readiness#1",
        "phase:recovered#1",
    ],
)
def test_absence_recovery_restarts_exactly_around_every_mutation_and_evidence_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    effect: str,
) -> None:
    metadata_template = _metadata("target-defined")
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="target-defined",
        source_present=False,
        xml=metadata_template.target_xml,
        active=True,
    )
    _record_phase_faults(monkeypatch, harness.faults)
    harness.faults.failures[fault] = effect

    _retry_until_phase(harness.recover, harness.root, harness.metadata, "recovered")

    assert harness.guest.states == {}
    assert harness.session.xml == harness.metadata.source_xml
    assert harness.session.active
    assert len(harness.sessions) >= 2


@pytest.mark.parametrize("effect", ["before", "after"])
@pytest.mark.parametrize(
    "fault",
    [
        "move:" + _OLD_NAME + "->6.12.0#1",
        "sync#3",
        "phase:rollback-complete#1",
        "phase:move-ready#2",
    ],
)
def test_fresh_activation_resumes_every_rollback_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    effect: str,
) -> None:
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="pre-stop-intent",
        source_present=True,
    )
    _record_phase_faults(monkeypatch, harness.faults)
    harness.faults.failures["move:" + _STAGING_NAME + "->6.12.0#1"] = "os-before"
    harness.faults.failures["phase:rollback-ready#1"] = "after"

    with pytest.raises(_ProcessLost, match="rollback-ready"):
        harness.activate()
    harness.faults.failures[fault] = effect
    _retry_until_phase(harness.activate, harness.root, harness.metadata, "target-defined")

    rollback_ready = harness.faults.actions.index("phase:rollback-ready#1")
    rollback_move = next(
        index
        for index, action in enumerate(harness.faults.actions)
        if action.startswith("move:" + _OLD_NAME + "->6.12.0")
    )
    assert rollback_ready < rollback_move
    assert any(action.startswith("phase:rollback-complete") for action in harness.faults.actions)
    assert harness.session.active
    assert len(harness.sessions) >= 3


@pytest.mark.parametrize("effect", ["before", "after"])
def test_fresh_activation_resumes_around_rollback_ready_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effect: str,
) -> None:
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="pre-stop-intent",
        source_present=True,
    )
    _record_phase_faults(monkeypatch, harness.faults)
    harness.faults.failures["move:" + _STAGING_NAME + "->6.12.0#1"] = "os-before"
    harness.faults.failures["phase:rollback-ready#1"] = effect

    _retry_until_phase(harness.activate, harness.root, harness.metadata, "target-defined")

    rollback_moves = [
        action
        for action in harness.faults.actions
        if action.startswith("move:" + _OLD_NAME + "->6.12.0")
    ]
    assert rollback_moves == ([] if effect == "before" else ["move:" + _OLD_NAME + "->6.12.0#1"])
    assert harness.session.active
    assert len(harness.sessions) >= 2


@pytest.mark.parametrize("effect", ["before", "after"])
@pytest.mark.parametrize("boundary", ["cleanup-payloads", "publish-tombstone"])
def test_fresh_cleanup_resumes_every_payload_and_tombstone_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    effect: str,
) -> None:
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="recovered",
        source_present=False,
    )
    harness.faults.failures[f"{boundary}#1"] = effect
    if boundary == "publish-tombstone":
        original = RecoveryMetadataStore.publish_tombstone

        def publish(
            store: RecoveryMetadataStore,
            reference: OpaqueProviderRef,
            binding: ExternalBootActivationBinding,
            expected: LocalRecoveryMetadataV1,
            point_digest: str,
        ) -> CleanupTombstoneV1:
            result = harness.faults.run(
                "publish-tombstone",
                lambda: original(store, reference, binding, expected, point_digest),
            )
            assert isinstance(result, CleanupTombstoneV1)
            return result

        monkeypatch.setattr(RecoveryMetadataStore, "publish_tombstone", publish)

    with pytest.raises(_ProcessLost, match=boundary):
        harness.cleanup()
    harness.cleanup()

    with RecoveryMetadataStore(harness.root) as store:
        assert store.cleanup_complete(harness.point.recovery_ref, harness.point)
    assert len(harness.sessions) == 2


@pytest.mark.parametrize("effect", ["before", "after"])
def test_fresh_finalization_resumes_around_tombstone_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    effect: str,
) -> None:
    harness = _FreshRestartHarness.create(
        tmp_path,
        phase="recovered",
        source_present=False,
    )
    harness.cleanup()
    proof = FinalizeCleanupProof(
        point_digest=LocalLibvirtExternalBoot.point_digest(harness.point),
        binding=harness.point.binding,
        operation_id="00000000-0000-0000-0000-000000000004",
        attempt_id="00000000-0000-0000-0000-000000000005",
        journal_sequence=7,
        journal_digest="sha256:" + "4" * 64,
        phase="mutation-started",
    )
    original = RecoveryMetadataStore.finalize_tombstone

    def finalize(
        store: RecoveryMetadataStore,
        reference: OpaqueProviderRef,
        recovery: RecoveryPoint,
        candidate: FinalizeCleanupProof,
    ) -> None:
        harness.faults.run(
            "delete-tombstone",
            lambda: original(store, reference, recovery, candidate),
        )

    monkeypatch.setattr(RecoveryMetadataStore, "finalize_tombstone", finalize)
    harness.faults.failures["delete-tombstone#1"] = effect

    with pytest.raises(_ProcessLost, match="delete-tombstone"):
        harness.finalize(proof)
    harness.finalize(proof)

    directory = harness.root / recovery_directory_name(
        harness.point.recovery_ref, harness.point.binding
    )
    assert not directory.exists()


@pytest.mark.parametrize(
    "result",
    [
        ReadinessResult(False, False, None),
        ReadinessResult(True, False, None),
        ReadinessResult(True, True, ProbeFailure.VIRSH_PROBE_FAILED),
    ],
)
def test_activation_advances_only_for_exact_readiness_success(
    tmp_path: Path,
    result: ReadinessResult,
) -> None:
    metadata_template = _metadata("module-restored")
    ports, metadata, session, _guest, root = _restart_fixture(
        tmp_path,
        phase="module-restored",
        source_present=False,
        xml=metadata_template.target_xml,
        active=True,
    )
    session.readiness_result = result

    with pytest.raises(ValueError, match="readiness"):
        ports.activate(_point(metadata), OpaqueProviderRef(ref="authority/current"))

    with RecoveryMetadataStore(root) as store:
        assert store.reopen(_point(metadata).recovery_ref, metadata.binding).phase == (
            "module-restored"
        )
    session.readiness_result = ReadinessResult(True, True, None)
    ports.activate(_point(metadata), OpaqueProviderRef(ref="authority/current"))
    assert session.faults.counts["readiness"] == 2


def test_inactive_prior_power_activation_and_recovery_never_start_or_probe(
    tmp_path: Path,
) -> None:
    ports, metadata, session, guest, root = _restart_fixture(
        tmp_path,
        phase="pre-stop-intent",
        source_present=False,
        prior_power="inactive",
    )

    ports.activate(_point(metadata), OpaqueProviderRef(ref="authority/current"))
    assert not session.active
    assert "start" not in session.faults.counts
    assert "readiness" not in session.faults.counts

    ports.recover(_point(metadata), OpaqueProviderRef(ref="authority/current"))
    assert not session.active
    assert guest.states == {}
    assert "start" not in session.faults.counts
    assert "readiness" not in session.faults.counts
    with RecoveryMetadataStore(root) as store:
        assert store.reopen(_point(metadata).recovery_ref, metadata.binding).phase == "recovered"


@pytest.mark.parametrize(
    ("xml", "active", "prior_power"),
    [
        (_SOURCE_XML, True, "running"),
        (_SOURCE_XML, True, "inactive"),
        (_SOURCE_XML.replace("/old", "/new"), True, "inactive"),
        ("<domain><name>substituted</name></domain>", False, "running"),
        ("<domain><name>substituted</name></domain>", True, "running"),
    ],
)
def test_activation_rejects_every_unlisted_xml_power_combination_before_mutation(
    tmp_path: Path,
    xml: str,
    active: bool,
    prior_power: str,
) -> None:
    ports, metadata, session, _guest, root = _restart_fixture(
        tmp_path,
        phase="module-restored",
        source_present=False,
        prior_power=prior_power,
        xml=xml,
        active=active,
    )

    with pytest.raises(ValueError, match="XML|power"):
        ports.activate(_point(metadata), OpaqueProviderRef(ref="authority/current"))

    assert session.faults.actions == []
    with RecoveryMetadataStore(root) as store:
        assert store.reopen(_point(metadata).recovery_ref, metadata.binding).phase == (
            "module-restored"
        )


@pytest.mark.parametrize(
    ("xml", "active", "prior_power"),
    [
        (_SOURCE_XML.replace("/old", "/new"), False, "running"),
        (_SOURCE_XML.replace("/old", "/new"), True, "running"),
        (_SOURCE_XML, True, "inactive"),
        ("<domain><name>substituted</name></domain>", False, "running"),
        ("<domain><name>substituted</name></domain>", True, "running"),
    ],
)
def test_recovery_rejects_every_unlisted_source_xml_power_combination_before_mutation(
    tmp_path: Path,
    xml: str,
    active: bool,
    prior_power: str,
) -> None:
    ports, metadata, session, guest, root = _restart_fixture(
        tmp_path,
        phase="source-restored",
        source_present=False,
        prior_power=prior_power,
        xml=xml,
        active=active,
    )
    guest.states = {}

    with pytest.raises(ValueError, match="XML|power"):
        ports.recover(_point(metadata), OpaqueProviderRef(ref="authority/current"))

    assert session.faults.actions == []
    with RecoveryMetadataStore(root) as store:
        assert store.reopen(_point(metadata).recovery_ref, metadata.binding).phase == (
            "source-restored"
        )


def test_activation_rejects_substituted_module_layout_before_mutation(tmp_path: Path) -> None:
    ports, metadata, session, guest, root = _restart_fixture(
        tmp_path,
        phase="pre-stop-intent",
        source_present=True,
    )
    live = f"/lib/modules/{metadata.release}"
    guest.states[live] = PresentComponentState(manifest="sha256:" + "f" * 64)

    with pytest.raises(ValueError, match="layout"):
        ports.activate(_point(metadata), OpaqueProviderRef(ref="authority/current"))

    assert session.faults.actions == []
    with RecoveryMetadataStore(root) as store:
        assert store.reopen(_point(metadata).recovery_ref, metadata.binding).phase == (
            "pre-stop-intent"
        )


def test_activation_rejects_substituted_artifact_reference_before_guest_mutation(
    tmp_path: Path,
) -> None:
    ports, metadata, session, guest, root = _restart_fixture(
        tmp_path,
        phase="pre-stop-intent",
        source_present=False,
    )
    substituted = metadata.model_copy(
        update={
            "materialized_modules": OpaqueProviderRef(
                ref=f"local-artifact-v1/{_BINDING.system_id}/foreign/{'a' * 64}/modules"
            )
        }
    )
    evidence = root / recovery_directory_name(_point(metadata).recovery_ref, metadata.binding)
    (evidence / "intent.json").write_bytes(
        json.dumps(
            substituted.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    )

    with pytest.raises(ValueError, match="artifact reference"):
        ports.activate(_point(substituted), OpaqueProviderRef(ref="authority/current"))

    assert session.faults.actions == []
    assert guest.states == {}


@pytest.mark.parametrize(
    ("xml", "active"),
    [
        (_SOURCE_XML, True),
        (_SOURCE_XML.replace("/old", "/new"), False),
        ("<domain><name>substituted</name></domain>", False),
    ],
)
def test_activation_rejects_unexpected_xml_or_power_before_module_mutation(
    tmp_path: Path,
    xml: str,
    active: bool,
) -> None:
    ports, metadata, session, guest, root = _restart_fixture(
        tmp_path,
        phase="pre-stop-intent",
        source_present=False,
        xml=xml,
        active=active,
    )

    with pytest.raises(ValueError, match="XML|power"):
        ports.activate(_point(metadata), OpaqueProviderRef(ref="authority/current"))

    assert session.faults.actions == []
    assert guest.states == {}
    with RecoveryMetadataStore(root) as store:
        reopened = store.reopen(_point(metadata).recovery_ref, metadata.binding)
        assert reopened.phase == "pre-stop-intent"


@pytest.mark.parametrize(
    "observation",
    [
        RunningKernelObservation(architecture="ppc64le", release="6.12.0", gnu_build_id="01020304"),
        RunningKernelObservation(architecture="x86_64", release="6.12.1", gnu_build_id="01020304"),
        RunningKernelObservation(architecture="x86_64", release="6.12.0", gnu_build_id="deadbeef"),
    ],
)
def test_observe_rejects_running_kernel_mismatch(
    tmp_path: Path,
    observation: RunningKernelObservation,
) -> None:
    metadata_template = _metadata("target-defined")
    ports, metadata, session, _guest, _root = _restart_fixture(
        tmp_path,
        phase="target-defined",
        source_present=False,
        xml=metadata_template.target_xml,
        active=True,
    )
    session.running_observation = observation

    with pytest.raises(ValueError, match="running kernel"):
        ports.observe(_point(metadata), OpaqueProviderRef(ref="authority/current"))


@pytest.mark.parametrize(
    ("xml", "active"),
    [
        (_SOURCE_XML, True),
        (_SOURCE_XML.replace("/old", "/new"), False),
        ("<domain><name>substituted</name></domain>", True),
    ],
)
def test_observe_requires_exact_running_target_before_kernel_probe(
    tmp_path: Path,
    xml: str,
    active: bool,
) -> None:
    ports, metadata, session, _guest, _root = _restart_fixture(
        tmp_path,
        phase="target-defined",
        source_present=False,
        xml=xml,
        active=active,
    )

    with pytest.raises(ValueError, match="XML"):
        ports.observe(_point(metadata), OpaqueProviderRef(ref="authority/current"))


class _RealSessionFactory:
    def __init__(self, session: _RealSession) -> None:
        self.session = session
        self.expected: list[ExpectedOperationOwnership] = []

    def open(
        self,
        lease: LocalExternalBootOperationLease,
        expected: ExpectedOperationOwnership,
    ) -> _RealSession:
        del lease
        self.expected.append(expected)
        return self.session


class _RecordingRecoveryWriter:
    def __init__(self, preparation: _RealPreparation | None = None) -> None:
        self._preparation = preparation
        self.captures: list[tuple[ExternalBootActivationBinding, str, bool]] = []

    def capture(
        self,
        tree: object,
        release: str,
        sink: RecoveryArchiveSink,
    ) -> AbsentModuleCapture:
        assert isinstance(tree, LibguestfsAuthenticatedGuestTree)
        self.captures.append((tree.binding, release, tree.mutable))
        sink.close()
        if self._preparation is not None and self._preparation.work_fault:
            raise LookupError("prepare primary")
        return AbsentModuleCapture()

    def observe(self, tree: AuthenticatedGuestTree, release: str) -> ComponentState:
        del tree, release
        raise AssertionError("not used")

    def install(
        self,
        tree: AuthenticatedGuestTree,
        release: str,
        source: KernelBundleSource,
    ) -> str:
        del tree, release, source
        raise AssertionError("not used")

    def restore(
        self,
        tree: AuthenticatedGuestTree,
        release: str,
        capture: AbsentModuleCapture | ModuleArchiveCapture,
        source: RecoveryArchiveSource,
    ) -> str:
        del tree, release, capture, source
        raise AssertionError("not used")


def _track_recovery_sink_close(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[RecoveryArchiveSink], list[int]]:
    sinks: list[RecoveryArchiveSink] = []
    close_calls: list[int] = []
    original_open = RecoveryMetadataStore.recovery_archive_sink
    original_close = RecoveryArchiveSink.close

    def open_sink(
        store: RecoveryMetadataStore,
        reference: OpaqueProviderRef,
        intent: LocalPreStopIntentV1,
    ) -> RecoveryArchiveSink:
        sink = original_open(store, reference, intent)
        sinks.append(sink)
        return sink

    def close_sink(sink: RecoveryArchiveSink) -> None:
        close_calls.append(cast(int, vars(sink)["_directory_fd"]))
        original_close(sink)

    monkeypatch.setattr(RecoveryMetadataStore, "recovery_archive_sink", open_sink)
    monkeypatch.setattr(RecoveryArchiveSink, "close", close_sink)
    return sinks, close_calls


def _sink_descriptor(sink: RecoveryArchiveSink) -> int:
    return cast(int, vars(sink)["_directory_fd"])


def _descriptor_is_closed(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return True
    return False


def test_real_adapter_captures_recovery_through_session_owned_capabilities(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    template = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    preparation = _RealPreparation(template, root)
    session = _RealSession(preparation)
    writer = _RecordingRecoveryWriter()
    io = RealLocalExternalBootIO(
        root,
        preparation,
        writer,
        lambda _authority: cast(LocalExternalBootOperationLease, object()),
        cast(LocalExternalBootSessionFactory, _RealSessionFactory(session)),
    )

    prepared = _real_prepare(io, materialization)

    assert writer.captures == [(_BINDING, "6.12.0", False)]
    assert prepared.capture == AbsentModuleCapture()
    assert prepared.source_state == ProviderStateIdentity(
        definition=template.source_boot,
        modules=AbsentComponentState(),
    )
    assert prepared.target_state == ProviderStateIdentity(
        definition=template.target_boot,
        modules=PresentComponentState(manifest=materialization.installed_module_tree),
    )


def test_real_adapter_closes_recovery_sink_when_guest_open_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    preparation = _RealPreparation(metadata, root)
    io, session = _real_io(root, preparation)
    session.guest_fault = True
    sinks, close_calls = _track_recovery_sink_close(monkeypatch)

    with pytest.raises(LookupError, match="guest open primary"):
        _real_prepare(io, materialization)

    assert len(sinks) == 1
    descriptor = _sink_descriptor(sinks[0])
    closed_before_test_cleanup = _descriptor_is_closed(descriptor)
    calls_before_test_cleanup = list(close_calls)
    if not closed_before_test_cleanup:
        sinks[0].close()
    assert closed_before_test_cleanup
    assert calls_before_test_cleanup == [descriptor]


def test_real_adapter_transfers_recovery_sink_once_before_capture_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    preparation = _RealPreparation(metadata, root)
    preparation.work_fault = True
    io, _session = _real_io(root, preparation)
    sinks, close_calls = _track_recovery_sink_close(monkeypatch)

    with pytest.raises(LookupError, match="prepare primary"):
        _real_prepare(io, materialization)

    assert len(sinks) == 1
    descriptor = _sink_descriptor(sinks[0])
    assert _descriptor_is_closed(descriptor)
    assert close_calls == [descriptor]


def _real_io(
    root: Path, preparation: _RealPreparation
) -> tuple[RealLocalExternalBootIO, _RealSession]:
    session = _RealSession(preparation)
    factory = cast(LocalExternalBootSessionFactory, _RealSessionFactory(session))
    io = RealLocalExternalBootIO(
        root,
        preparation,
        _RecordingRecoveryWriter(preparation),
        lambda _authority: cast(LocalExternalBootOperationLease, object()),
        factory,
    )
    return io, session


def _real_prepare(
    io: RealLocalExternalBootIO,
    materialization: ExternalBootMaterialization,
) -> LocalRecoveryMetadataV1:
    expected = ExpectedOperationOwnership(
        UUID(_BINDING.system_id), UUID(_BINDING.run_id), UUID(_BINDING.activation_id)
    )
    with io.open(OpaqueProviderRef(ref="authority/current"), expected) as operation:
        return operation.prepare(materialization, _BINDING)


def test_real_adapter_persists_intent_before_first_host_mutation(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    host = _RealPreparation(metadata, root)
    io, session = _real_io(root, host)
    prepared = _real_prepare(io, materialization)
    assert prepared == metadata
    assert host.actions == ["inspect", "first-mutation"]
    assert session.close_attempts == 1


@pytest.mark.parametrize("effect", ["before", "after"])
@pytest.mark.parametrize("boundary", ["intent", "stop", "capture", "complete"])
def test_fresh_adapter_resumes_every_preparation_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    effect: str,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    preparation = _RealPreparation(metadata, root)
    faults = _RestartFaults()
    faults.failures[f"{boundary}#1"] = effect
    first_io, first_session = _real_io(root, preparation)

    if boundary == "intent":
        original = RecoveryMetadataStore.publish_pre_stop

        def publish_intent(
            store: RecoveryMetadataStore,
            intent: LocalPreStopIntentV1,
        ) -> OpaqueProviderRef:
            result = faults.run("intent", lambda: original(store, intent))
            assert isinstance(result, OpaqueProviderRef)
            return result

        monkeypatch.setattr(RecoveryMetadataStore, "publish_pre_stop", publish_intent)
    elif boundary == "stop":
        original_stop = first_session.stop_and_require_inactive
        monkeypatch.setattr(
            first_session,
            "stop_and_require_inactive",
            lambda: faults.run("stop", original_stop),
        )
    elif boundary == "capture":
        original_capture = _RecordingRecoveryWriter.capture

        def capture(
            writer: _RecordingRecoveryWriter,
            tree: object,
            release: str,
            sink: RecoveryArchiveSink,
        ) -> AbsentModuleCapture:
            result = faults.run("capture", lambda: original_capture(writer, tree, release, sink))
            assert isinstance(result, AbsentModuleCapture)
            return result

        monkeypatch.setattr(_RecordingRecoveryWriter, "capture", capture)
    else:
        original_complete = RecoveryMetadataStore.complete_preparation

        def complete(
            store: RecoveryMetadataStore,
            reference: OpaqueProviderRef,
            intent: LocalPreStopIntentV1,
            completed: LocalRecoveryMetadataV1,
        ) -> LocalRecoveryMetadataV1:
            result = faults.run(
                "complete",
                lambda: original_complete(store, reference, intent, completed),
            )
            assert isinstance(result, LocalRecoveryMetadataV1)
            return result

        monkeypatch.setattr(RecoveryMetadataStore, "complete_preparation", complete)

    with pytest.raises(_ProcessLost, match=boundary):
        _real_prepare(first_io, materialization)
    second_io, second_session = _real_io(root, preparation)

    assert _real_prepare(second_io, materialization) == metadata
    assert first_session is not second_session
    assert first_session.close_attempts == 1
    assert second_session.close_attempts == 1


def test_real_adapter_intent_fsync_fault_prevents_first_host_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    host = _RealPreparation(metadata, root)
    io, session = _real_io(root, host)
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fault")))
    with pytest.raises(OSError, match="fault"):
        _real_prepare(io, materialization)
    assert host.actions == ["inspect"]
    assert session.close_attempts == 1


def test_real_adapter_retry_reopens_pre_stop_before_reinspection(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    intent = _pre_stop(metadata)
    with RecoveryMetadataStore(root) as store:
        store.publish_pre_stop(intent)
    host = _RealPreparation(metadata, root)
    host.inspect_allowed = False
    io, session = _real_io(root, host)

    assert _real_prepare(io, materialization) == metadata
    assert host.actions == ["first-mutation"]
    assert session.close_attempts == 1


def test_real_adapter_retry_rejects_crossed_pre_stop_before_host_access(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    crossed = _pre_stop(metadata).model_copy(update={"plan_identity": "sha256:" + "e" * 64})
    with RecoveryMetadataStore(root) as store:
        store.publish_pre_stop(crossed)
    host = _RealPreparation(metadata, root)
    io, session = _real_io(root, host)

    with pytest.raises(ValueError, match="pre-stop intent does not match"):
        _real_prepare(io, materialization)
    assert host.actions == []
    assert session.close_attempts == 1


def test_real_adapter_retry_rejects_cross_binding_pre_stop_before_host_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    intent = _pre_stop(metadata)
    with RecoveryMetadataStore(root) as store:
        store.publish_pre_stop(intent)
    crossed = intent.model_copy(
        update={
            "binding": intent.binding.model_copy(
                update={"activation_id": "00000000-0000-0000-0000-000000000099"}
            )
        }
    )
    original_reopen = RecoveryMetadataStore.reopen_pre_stop

    def reopen_crossed(
        store: RecoveryMetadataStore,
        reference: OpaqueProviderRef,
        binding: ExternalBootActivationBinding,
    ) -> LocalPreStopIntentV1:
        original_reopen(store, reference, binding)
        return crossed

    monkeypatch.setattr(RecoveryMetadataStore, "reopen_pre_stop", reopen_crossed)
    host = _RealPreparation(metadata, root)
    io, session = _real_io(root, host)

    with pytest.raises(ValueError, match="pre-stop intent does not match"):
        _real_prepare(io, materialization)

    assert host.actions == []
    assert session.close_attempts == 1


def test_real_adapter_retry_rejects_substituted_expected_kernel_before_host_access(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    crossed = _pre_stop(metadata).model_copy(
        update={
            "expected_running": RunningKernelObservation(
                architecture="x86_64",
                release="6.12.0",
                gnu_build_id="deadbeef",
            )
        }
    )
    with RecoveryMetadataStore(root) as store:
        store.publish_pre_stop(crossed)
    host = _RealPreparation(metadata, root)
    io, session = _real_io(root, host)

    with pytest.raises(ValueError, match="pre-stop intent does not match"):
        _real_prepare(io, materialization)
    assert host.actions == []
    assert session.close_attempts == 1


@pytest.mark.parametrize(
    ("field", "substitution", "refusal", "intent_reopenable"),
    [
        # Refused by `_validate_preparation_inspection`, which compares the reopened intent
        # against the host it re-inspected. The record itself stays internally consistent, so
        # it can still be reopened.
        (
            "target_projection_sha256",
            "sha256:" + "f" * 64,
            "changed before recovery capture",
            True,
        ),
        # Refused earlier, by the record's own `target_xml_sha256` binding: substituting the
        # XML alone makes the record fail validation, so it cannot even be rebuilt. The
        # guarantee this test exists for — no `define:` reaches the session — holds either
        # way, and now holds one layer sooner.
        (
            "target_xml",
            "<domain><name>substituted</name></domain>",
            "target domain XML digest does not match bytes",
            False,
        ),
    ],
)
def test_real_adapter_rejects_substituted_target_metadata_before_publication_or_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    substitution: str,
    refusal: str,
    intent_reopenable: bool,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    preparation = _RealPreparation(metadata, root)
    io, session = _real_io(root, preparation)
    original_stop = session.stop_and_require_inactive
    substituted = _pre_stop(metadata).model_copy(update={field: substitution})

    def substitute_after_stop() -> None:
        original_stop()
        intent = (
            root
            / f".{metadata.binding.system_id}.{metadata.binding.activation_id}.partial"
            / "intent.json"
        )
        intent.write_bytes(
            json.dumps(
                substituted.model_dump(mode="json", by_alias=True),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        )

    monkeypatch.setattr(session, "stop_and_require_inactive", substitute_after_stop)

    with pytest.raises(ValueError, match=refusal):
        _real_prepare(io, materialization)

    point = _point(metadata)
    with RecoveryMetadataStore(root) as store:
        if intent_reopenable:
            assert store.reopen_pre_stop(point.recovery_ref, metadata.binding) == substituted
        else:
            with pytest.raises(ValueError, match=refusal):
                store.reopen_pre_stop(point.recovery_ref, metadata.binding)
        with pytest.raises(FileNotFoundError):
            store.reopen(point.recovery_ref, metadata.binding)
    assert not any(action.startswith("define:") for action in preparation.actions)
    assert session.close_attempts == 1


def test_real_adapter_rejects_wrong_domain_state_before_stop(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    preparation = _RealPreparation(metadata, root)
    io, session = _real_io(root, preparation)
    session.inspection = replace(
        session.inspection,
        definition_identity="sha256:" + "f" * 64,
    )

    with pytest.raises(ValueError, match="closed domain inspection"):
        _real_prepare(io, materialization)

    assert preparation.actions == ["inspect"]
    assert session.close_attempts == 1


def test_real_adapter_preserves_prepare_error_when_session_close_fails(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    materialization = _materialization()
    metadata = _metadata().model_copy(update={"materialization_identity": materialization.identity})
    preparation = _RealPreparation(metadata, root)
    preparation.work_fault = True
    io, session = _real_io(root, preparation)
    session.close_fault = True

    with pytest.raises(LookupError, match="prepare primary") as raised:
        _real_prepare(io, materialization)

    assert raised.value.__notes__ == ["cleanup failed: OSError('session close')"]
    assert session.close_attempts == 1


def test_real_adapter_cleanup_retry_accepts_only_exact_tombstone(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    point = _point(metadata)
    host = _RealPreparation(metadata, root)
    io, session = _real_io(root, host)
    ports = LocalLibvirtExternalBoot(io)
    with RecoveryMetadataStore(root) as store:
        store.publish(metadata)

    ports.cleanup(point, OpaqueProviderRef(ref="authority/current"))
    assert host.actions == ["cleanup"]

    ports.cleanup(point, OpaqueProviderRef(ref="authority/current"))
    assert host.actions == ["cleanup"]

    crossed = point.model_copy(update={"plan_identity": "sha256:" + "f" * 64})
    with pytest.raises(ValueError, match="tombstone does not match"):
        ports.cleanup(crossed, OpaqueProviderRef(ref="authority/current"))
    assert host.actions == ["cleanup"]
    assert session.close_attempts == 3


def test_real_adapter_rechecks_exact_metadata_before_cleanup_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    point = _point(metadata)
    host = _RealPreparation(metadata, root)
    io, session = _real_io(root, host)
    ports = LocalLibvirtExternalBoot(io)
    with RecoveryMetadataStore(root) as store:
        store.publish(metadata)

    original_reopen = ports._reopen

    def substitute_after_reopen(
        operation: external_boot_module.LocalExternalBootOperation,
        recovery: RecoveryPoint,
    ) -> LocalRecoveryMetadataV1:
        reopened = original_reopen(operation, recovery)
        with RecoveryMetadataStore(root) as store:
            store.record_phase(recovery.recovery_ref, recovery.binding, reopened, "cleaned")
        return reopened

    monkeypatch.setattr(ports, "_reopen", substitute_after_reopen)

    with pytest.raises(ValueError, match="changed before cleanup"):
        ports.cleanup(point, OpaqueProviderRef(ref="authority/current"))

    assert host.actions == []
    assert session.close_attempts == 1


def test_real_adapter_cleanup_close_failure_retains_published_tombstone(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    point = _point(metadata)
    host = _RealPreparation(metadata, root)
    io, session = _real_io(root, host)
    session.close_fault = True
    with RecoveryMetadataStore(root) as store:
        store.publish(metadata)

    with pytest.raises(OSError, match="session close"):
        LocalLibvirtExternalBoot(io).cleanup(
            point,
            OpaqueProviderRef(ref="authority/current"),
        )

    assert host.actions == ["cleanup"]
    with RecoveryMetadataStore(root) as store:
        assert store.cleanup_complete(point.recovery_ref, point)


@pytest.mark.parametrize("authority", ["authority/stale", "authority/foreign"])
def test_real_adapter_cleanup_complete_still_validates_authority(
    tmp_path: Path,
    authority: str,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    point = _point(metadata)
    host = _RealPreparation(metadata, root)
    session = _RealSession(host)
    resolutions: list[str] = []

    def resolve(reference: OpaqueProviderRef) -> LocalExternalBootOperationLease:
        resolutions.append(reference.ref)
        if reference.ref != "authority/current":
            raise ValueError("operation authority is stale or foreign")
        return cast(LocalExternalBootOperationLease, object())

    io = RealLocalExternalBootIO(
        root,
        host,
        _RecordingRecoveryWriter(host),
        resolve,
        cast(LocalExternalBootSessionFactory, _RealSessionFactory(session)),
    )
    ports = LocalLibvirtExternalBoot(io)
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        store.publish_tombstone(reference, metadata.binding, metadata, ports.point_digest(point))

    with pytest.raises(ValueError, match="stale or foreign"):
        ports.cleanup(point, OpaqueProviderRef(ref=authority))

    assert resolutions == [authority]
    assert host.actions == []
    assert session.close_attempts == 0


def test_real_adapter_finalization_replays_exact_proof_without_session(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    point = _point(metadata)
    host = _RealPreparation(metadata, root)
    session = _RealSession(host)

    def reject_session(_authority: OpaqueProviderRef) -> LocalExternalBootOperationLease:
        raise AssertionError("finalization must not resolve or open an operation session")

    io = RealLocalExternalBootIO(
        root,
        host,
        _RecordingRecoveryWriter(host),
        reject_session,
        cast(LocalExternalBootSessionFactory, _RealSessionFactory(session)),
    )
    ports = LocalLibvirtExternalBoot(io)
    proof = FinalizeCleanupProof(
        point_digest=ports.point_digest(point),
        binding=point.binding,
        operation_id="00000000-0000-0000-0000-000000000004",
        attempt_id="00000000-0000-0000-0000-000000000005",
        journal_sequence=7,
        journal_digest="sha256:" + "4" * 64,
        phase="mutation-started",
    )
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        store.publish_tombstone(reference, metadata.binding, metadata, proof.point_digest)

    authority = OpaqueProviderRef(ref="authority/authenticated-by-2140")
    ports.finalize_cleanup_tombstone(point, proof, authority)
    ports.finalize_cleanup_tombstone(point, proof, authority)

    assert session.close_attempts == 0
    assert not (root / recovery_directory_name(point.recovery_ref, point.binding)).exists()


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
        "reopen",
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
        "reopen",
        "define-source",
        "phase:source-restored",
        "reopen",
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
        phase="mutation-started",
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
        phase="mutation-started",
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


def test_recovery_metadata_refuses_a_substituted_target_xml() -> None:
    values = _metadata().model_dump(mode="json", by_alias=True)
    values["target_xml"] = values["target_xml"] + " "
    with pytest.raises(ValidationError, match="target domain XML digest"):
        LocalRecoveryMetadataV1.model_validate(values)


def test_pre_stop_intent_refuses_a_substituted_target_xml() -> None:
    values = _pre_stop(_metadata()).model_dump(mode="json", by_alias=True)
    values["target_xml"] = values["target_xml"] + " "
    with pytest.raises(ValidationError, match="target domain XML digest"):
        LocalPreStopIntentV1.model_validate(values)


def _substitute_target_xml_on_disk(root: Path, name: str) -> str:
    """Rewrite ``target_xml`` in a published record, leaving every other field alone."""
    record = root / name / "intent.json"
    payload = json.loads(record.read_text())
    payload["target_xml"] = payload["target_xml"] + " "
    record.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return cast(str, payload["target_xml"])


def test_a_target_xml_substituted_on_disk_is_refused_at_reopen(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        name = recovery_directory_name(reference, metadata.binding)
        _substitute_target_xml_on_disk(root, name)
        with pytest.raises(ValidationError, match="target domain XML digest"):
            store.reopen(reference, metadata.binding)


def test_activate_never_defines_a_target_xml_substituted_on_disk(tmp_path: Path) -> None:
    """The digest binding stops a substituted record before the coordinator can use it.

    ``define_target`` reaches ``self._session.define_xml(metadata.target_xml)``, and
    ``_host_state`` compares ``inspection.xml`` against ``metadata.target_xml.encode()``, so
    both take a ``LocalRecoveryMetadataV1``. Binding ``target_xml`` to its own digest means no
    such instance can carry substituted bytes: the store refuses to rebuild one, and
    ``activate`` fails at ``_reopen`` before any host access.

    The discriminating assertion is the ``match``, not the absent ``define:`` action. Delete
    the ``target_xml_sha256`` comparison from the validator and the substituted record
    validates, so the failure changes to ``_host_state``'s "observed domain XML does not
    match recovery metadata" and this test goes red on the message it required.
    """
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("module-restored")
    host = _RealPreparation(metadata, root)
    io, _session = _real_io(root, host)
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        name = recovery_directory_name(reference, metadata.binding)
    _substitute_target_xml_on_disk(root, name)

    with pytest.raises(ValidationError, match="target domain XML digest"):
        LocalLibvirtExternalBoot(io).activate(
            _point(metadata), OpaqueProviderRef(ref="authority/current")
        )

    assert [action for action in host.actions if action.startswith("define:")] == []


def test_finalization_refuses_a_directory_whose_recovery_record_was_never_unlinked(
    tmp_path: Path,
) -> None:
    """`publish_tombstone` writes the tombstone and *then* unlinks `intent.json`.

    A crash between those two writes leaves both files present. `cleanup_complete` reports
    True for that directory, so `LocalLibvirtExternalBoot.cleanup` short-circuits without
    completing the unlink — and finalization then refuses, because the directory holds more
    than the tombstone. No fake can carry this precondition, so it is pinned here against the
    real store. The authority adapter must not finalize in this state; it asks
    `cleanup_is_accounted` first for exactly this reason.
    """
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    metadata = _metadata("recovered")
    point = _point(metadata)
    digest = LocalLibvirtExternalBoot.point_digest(point)
    with RecoveryMetadataStore(root) as store:
        reference = store.publish(metadata)
        name = recovery_directory_name(reference, metadata.binding)
        store.publish_tombstone(reference, metadata.binding, metadata, digest)
        # Restore the record `publish_tombstone` unlinked, reproducing the interrupted state.
        record = root / name / "intent.json"
        record.write_bytes(external_boot_module._metadata_bytes(metadata))
        record.chmod(0o600)

        assert sorted(path.name for path in (root / name).iterdir()) == [
            "intent.json",
            "tombstone.json",
        ]
        assert store.cleanup_complete(reference, point) is True

        proof = FinalizeCleanupProof(
            point_digest=digest,
            binding=point.binding,
            operation_id="op-1",
            attempt_id="00000000-0000-0000-0000-000000000005",
            journal_sequence=1,
            journal_digest="sha256:" + "4" * 64,
            phase="mutation-started",
        )
        with pytest.raises(ValueError, match="unexpected payload"):
            store.finalize_tombstone(reference, point, proof)


def test_target_projection_digest_still_measures_projection_inputs() -> None:
    projection = _projection()
    canonical = projection.canonical_bytes()

    assert projection.digest == "sha256:" + hashlib.sha256(canonical).hexdigest()
    assert "<domain" not in canonical.decode()
    changed = projection.model_copy(update={"cmdline": projection.cmdline + " quiet"})
    assert changed.digest != projection.digest
