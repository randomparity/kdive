"""Crash-safe authority-journal codec tests (ADR-0584, #2126)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    GENESIS_DIGEST,
    JournalPhase,
    JournalRecordV1,
    RecoveryObjectBindingV1,
    canonical_record_bytes,
    record_digest,
)

_DIGEST = "sha256:" + "a" * 64
_AUTHORITY_ID = uuid4()
_SYSTEM_ID = uuid4()
_ACTIVATION_ID = uuid4()
_RUN_ID = uuid4()
_ATTEMPT_ID = uuid4()


def _record(
    sequence: int = 1,
    previous_digest: str = GENESIS_DIGEST,
    *,
    phase: JournalPhase = JournalPhase.WATERMARK_INSTALLED,
    **changes: object,
) -> JournalRecordV1:
    values: dict[str, object] = {
        "sequence": sequence,
        "previous_digest": previous_digest,
        "phase": phase,
        "authority_id": _AUTHORITY_ID,
        "generation": 1,
        "system_id": _SYSTEM_ID,
        "activation_id": _ACTIVATION_ID,
        "run_id": _RUN_ID,
        "plan_identity": _DIGEST,
        "purpose": "recover",
        "provider_kind": "remote-libvirt",
        "authority_instance": "authority-a",
        "operation_identity": "operation-a",
        "operation_digest": _DIGEST,
        "attempt_id": _ATTEMPT_ID,
    }
    if phase not in {
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_SUPERSEDED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    }:
        values |= {
            "expected_source_identity": _DIGEST,
            "intended_target_identity": _DIGEST,
            "recovery_objects": (),
        }
    if phase in {JournalPhase.TAKEOVER_SUPERSEDED, JournalPhase.TAKEOVER_ACKNOWLEDGED}:
        values |= {"watermark_sequence": 1, "watermark_digest": _DIGEST}
    if phase is JournalPhase.TAKEOVER_SUPERSEDED:
        values["predecessor_generation"] = 1
    values.update(changes)
    return JournalRecordV1.model_validate(values)


def _recovery_object(reference: str) -> RecoveryObjectBindingV1:
    return RecoveryObjectBindingV1(
        system_id=_SYSTEM_ID, activation_id=_ACTIVATION_ID, reference=reference
    )


def test_append_creates_private_file_and_loads_exact_records(tmp_path: Path) -> None:
    path = tmp_path / "journal.ndjson"
    journal = FileAuthorityJournal(path)
    first = _record()
    journal.append(first)
    second = _record(
        2,
        record_digest(first),
        phase=JournalPhase.TAKEOVER_ACKNOWLEDGED,
        watermark_digest=record_digest(first),
    )
    journal.append(second)

    assert path.stat().st_mode & 0o777 == 0o600
    assert journal.load() == (first, second)
    assert (
        path.read_bytes()
        == canonical_record_bytes(first) + b"\n" + canonical_record_bytes(second) + b"\n"
    )


def test_append_fsyncs_parent_on_first_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        calls.append(os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    FileAuthorityJournal(tmp_path / "journal.ndjson").append(_record())

    assert any(stat.S_ISDIR(mode) for mode in calls)


def test_existing_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("")
    path = tmp_path / "journal"
    path.symlink_to(target)
    with pytest.raises(OSError):
        FileAuthorityJournal(path).load()


@pytest.mark.parametrize("mode", [0o620, 0o602])
def test_existing_group_or_other_writable_file_is_rejected(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "journal"
    path.write_text("")
    path.chmod(mode)
    with pytest.raises(PermissionError):
        FileAuthorityJournal(path).load()


def test_non_regular_path_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "journal"
    path.mkdir()
    with pytest.raises(OSError):
        FileAuthorityJournal(path).load()


def test_partial_final_record_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "journal"
    path.write_bytes(canonical_record_bytes(_record()))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="partial"):
        FileAuthorityJournal(path).load()


def test_duplicate_or_broken_chain_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "journal"
    first = _record()
    duplicate = _record(1, record_digest(first))
    path.write_bytes(
        canonical_record_bytes(first) + b"\n" + canonical_record_bytes(duplicate) + b"\n"
    )
    path.chmod(0o600)
    with pytest.raises(ValueError, match="sequence"):
        FileAuthorityJournal(path).load()


def test_foreign_lane_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "journal"
    first = _record()
    second = _record(2, record_digest(first)).model_copy(update={"system_id": uuid4()})
    path.write_bytes(canonical_record_bytes(first) + b"\n" + canonical_record_bytes(second) + b"\n")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="lane"):
        FileAuthorityJournal(path).load()


def test_invalid_phase_ordering_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "journal"
    first = _record()
    second = _record(2, record_digest(first), phase=JournalPhase.PROVIDER_RETURNED)
    path.write_bytes(canonical_record_bytes(first) + b"\n" + canonical_record_bytes(second) + b"\n")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="phase ordering"):
        FileAuthorityJournal(path).load()


def test_corrupt_previous_digest_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "journal"
    first = _record()
    second = _record(2, _DIGEST, phase=JournalPhase.TAKEOVER_ACKNOWLEDGED)
    path.write_bytes(canonical_record_bytes(first) + b"\n" + canonical_record_bytes(second) + b"\n")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="digest chain"):
        FileAuthorityJournal(path).load()


def test_existing_file_owned_by_another_identity_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "journal"
    path.write_text("")
    path.chmod(0o600)
    with pytest.raises(PermissionError, match="service identity"):
        FileAuthorityJournal(path, owner_uid=os.geteuid() + 1).load()


@pytest.mark.parametrize("phase", [JournalPhase.OBSERVED, JournalPhase.WATERMARK_INSTALLED])
def test_append_rejects_skipped_or_reversed_phase_without_changing_bytes(
    tmp_path: Path, phase: JournalPhase
) -> None:
    path = tmp_path / "journal"
    journal = FileAuthorityJournal(path)
    first = _record()
    journal.append(first)
    before = path.read_bytes()
    changes: dict[str, object] = {}
    if phase is JournalPhase.OBSERVED:
        changes = {
            "expected_source_identity": _DIGEST,
            "intended_target_identity": _DIGEST,
            "observation": {
                "observation_id": str(uuid4()),
                "category": "source",
                "composite_state": _DIGEST,
            },
        }
    candidate = _record(2, record_digest(first), phase=phase, **changes)

    with pytest.raises(ValueError, match="phase ordering"):
        journal.append(candidate)

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_source_identity": "source-b"},
        {"intended_target_identity": "target-b"},
        {"recovery_objects": ()},
        {"recovery_objects": (_recovery_object("object-b"),)},
    ],
)
def test_append_rejects_mutation_evidence_drift_without_changing_bytes(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    path = tmp_path / "journal"
    journal = FileAuthorityJournal(path)
    first = _record(
        phase=JournalPhase.ADMITTED,
        expected_source_identity="source-a",
        intended_target_identity="target-a",
        recovery_objects=(_recovery_object("object-a"),),
    )
    journal.append(first)
    before = path.read_bytes()
    candidate = _record(
        2,
        record_digest(first),
        phase=JournalPhase.MUTATION_STARTED,
        **(
            {
                "expected_source_identity": "source-a",
                "intended_target_identity": "target-a",
                "recovery_objects": (_recovery_object("object-a"),),
            }
            | changes
        ),
    )

    with pytest.raises(ValueError, match="binding changed"):
        journal.append(candidate)

    assert path.read_bytes() == before


def test_duplicate_generation_watermark_is_rejected_on_load_and_append(tmp_path: Path) -> None:
    path = tmp_path / "journal"
    journal = FileAuthorityJournal(path)
    first = _record(operation_identity="takeover-a")
    journal.append(first)
    before = path.read_bytes()
    duplicate = _record(
        2,
        record_digest(first),
        operation_identity="takeover-b",
        attempt_id=uuid4(),
    )

    with pytest.raises(ValueError, match="multiple watermarks"):
        journal.append(duplicate)
    assert path.read_bytes() == before

    path.write_bytes(before + canonical_record_bytes(duplicate) + b"\n")
    with pytest.raises(ValueError, match="multiple watermarks"):
        journal.load()


def test_acknowledgement_cannot_cross_link_another_same_generation_operation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal"
    journal = FileAuthorityJournal(path)
    first = _record(operation_identity="takeover-a")
    journal.append(first)
    before = path.read_bytes()
    other = _record(operation_identity="takeover-b", attempt_id=uuid4())
    crossed = _record(
        2,
        record_digest(first),
        phase=JournalPhase.TAKEOVER_ACKNOWLEDGED,
        operation_identity="takeover-a",
        watermark_sequence=other.sequence,
        watermark_digest=record_digest(other),
    )

    with pytest.raises(ValueError, match="watermark link"):
        journal.append(crossed)
    assert path.read_bytes() == before

    path.write_bytes(before + canonical_record_bytes(crossed) + b"\n")
    with pytest.raises(ValueError, match="watermark link"):
        journal.load()
