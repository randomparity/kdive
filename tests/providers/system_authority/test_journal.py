"""Filesystem proofs for the per-System authority journal."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kdive.providers.system_authority import journal as journal_module
from kdive.providers.system_authority.journal import (
    ABANDONED_DIRECTORY,
    MAX_ABANDONED_PER_SYSTEM,
    MAX_JOURNAL_FILES,
    FileAuthoritySystemJournal,
    inventory_authority_system_journals,
)
from kdive.providers.system_authority.protocol import (
    GENESIS_DIGEST,
    MAX_MESSAGE_BYTES,
    AuthoritySystemJournalPhase,
    AuthoritySystemJournalRecordV1,
    AuthoritySystemOperation,
    authority_system_record_digest,
    canonical_system_authority_bytes,
    canonical_system_record_payload,
)

_DIGEST = "sha256:" + "a" * 64
_ALLOCATION_ID = UUID("10000000-0000-0000-0000-000000000001")
_RESOURCE_ID = UUID("10000000-0000-0000-0000-000000000002")
_AUTHORITY_ID = UUID("10000000-0000-0000-0000-000000000003")
_ATTEMPT_ID = UUID("10000000-0000-0000-0000-000000000004")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "authority"
    root.mkdir(mode=0o700)
    (root / "system-operations").mkdir(mode=0o700)
    return root


def _record(
    system_id: UUID,
    *,
    sequence: int,
    previous_digest: str,
    phase: AuthoritySystemJournalPhase,
    outcome: str | None = None,
    generation: int = 1,
) -> AuthoritySystemJournalRecordV1:
    fields: dict[str, object] = {
        "schema": "authority-system-journal-v1",
        "system_id": system_id,
        "allocation_id": _ALLOCATION_ID,
        "resource_id": _RESOURCE_ID,
        "provider_kind": "local-libvirt",
        "resource_name": "host-a",
        "authority_instance": "auth-a",
        "profile_identity": _DIGEST,
        "root_identity": _DIGEST,
        "operation": AuthoritySystemOperation.PROVISION,
        "operation_identity": "provision-a",
        "authority_id": _AUTHORITY_ID,
        "generation": generation,
        "attempt_id": _ATTEMPT_ID,
        "operation_digest": _DIGEST,
        "bootstrap_identity": _DIGEST,
        "sequence": sequence,
        "previous_digest": previous_digest,
        "phase": phase,
        "observation": None,
        "outcome": outcome,
    }
    return AuthoritySystemJournalRecordV1.model_validate(
        {**fields, "canonical_record": canonical_system_record_payload(fields)}
    )


def test_append_reopen_and_inventory_preserve_global_head(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id = uuid4()
    admitted = _record(
        system_id,
        sequence=1,
        previous_digest=GENESIS_DIGEST,
        phase=AuthoritySystemJournalPhase.ADMITTED,
    )
    with FileAuthoritySystemJournal(root, system_id) as journal:
        first = journal.append(admitted)
    started = _record(
        system_id,
        sequence=2,
        previous_digest=first.digest,
        phase=AuthoritySystemJournalPhase.MUTATION_STARTED,
    )
    with FileAuthoritySystemJournal(root, system_id) as journal:
        assert journal.append(started).digest == authority_system_record_digest(started)
        assert journal.read() == (admitted, started)
    assert inventory_authority_system_journals(root)[0].sequence == 2


def test_noncanonical_or_partial_record_is_rejected_without_append(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id = uuid4()
    path = root / "system-operations" / f"{system_id}.jsonl"
    path.write_bytes(b'{"schema": "authority-system-journal-v1"}')
    os.chmod(path, 0o600)
    with (
        FileAuthoritySystemJournal(root, system_id) as journal,
        pytest.raises(ValueError, match="incomplete final record"),
    ):
        journal.read()
    assert path.read_bytes() == b'{"schema": "authority-system-journal-v1"}'


def test_hard_linked_journal_is_rejected_before_read_or_append(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id = uuid4()
    path = root / "system-operations" / f"{system_id}.jsonl"
    path.touch(mode=0o600)
    os.link(path, root / "system-operations" / "alias")
    with FileAuthoritySystemJournal(root, system_id) as journal:
        with pytest.raises(PermissionError, match="exactly one link"):
            journal.read()
        with pytest.raises(PermissionError, match="exactly one link"):
            journal.append(
                _record(
                    system_id,
                    sequence=1,
                    previous_digest=GENESIS_DIGEST,
                    phase=AuthoritySystemJournalPhase.ADMITTED,
                )
            )


def test_append_rejects_path_replacement_after_validation_without_writing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    system_id = uuid4()
    path = root / "system-operations" / f"{system_id}.jsonl"
    first = _record(
        system_id,
        sequence=1,
        previous_digest=GENESIS_DIGEST,
        phase=AuthoritySystemJournalPhase.ADMITTED,
    )
    journal = FileAuthoritySystemJournal(root, system_id)
    journal.append(first)
    original = path.read_bytes()
    replacement = root / "system-operations" / "replacement"
    replacement.write_bytes(original)
    replacement.chmod(0o600)
    displaced = root / "system-operations" / "displaced"
    real_open = journal._open_append

    def replace_then_open() -> tuple[int, bool]:
        path.rename(displaced)
        replacement.rename(path)
        return real_open()

    monkeypatch.setattr(journal, "_open_append", replace_then_open)
    second = _record(
        system_id,
        sequence=2,
        previous_digest=authority_system_record_digest(first),
        phase=AuthoritySystemJournalPhase.MUTATION_STARTED,
    )
    with pytest.raises(ValueError, match="changed since validation"):
        journal.append(second)
    journal.close()
    assert path.read_bytes() == original
    assert displaced.read_bytes() == original


def test_inventory_rejects_aggregate_4097_before_opening_any_journal(tmp_path: Path) -> None:
    root = _root(tmp_path)
    directory = root / "system-operations"
    first = directory / f"{uuid4()}.jsonl"
    first.write_text("outside", encoding="utf-8")
    os.chmod(first, 0)
    for _ in range(MAX_JOURNAL_FILES):
        path = directory / f"{uuid4()}.jsonl"
        path.touch(mode=0o600)
    with pytest.raises(ValueError, match="4096 files"):
        inventory_authority_system_journals(root)
    os.chmod(first, 0o600)
    assert first.read_text(encoding="utf-8") == "outside"


def test_canonical_payload_rejects_self_inclusion() -> None:
    with pytest.raises(ValueError, match="exclude"):
        canonical_system_record_payload({"canonical_record": json.dumps({})})


def _confirmed_then_extra(
    root: Path, *, extra_generation: int = 1
) -> tuple[UUID, tuple[AuthoritySystemJournalRecordV1, ...]]:
    """Two confirmed records and one unconfirmed record appended after them."""
    system_id = uuid4()
    journal = FileAuthoritySystemJournal(root, system_id)
    admitted = _record(
        system_id,
        sequence=1,
        previous_digest=GENESIS_DIGEST,
        phase=AuthoritySystemJournalPhase.ADMITTED,
    )
    started = _record(
        system_id,
        sequence=2,
        previous_digest=authority_system_record_digest(admitted),
        phase=AuthoritySystemJournalPhase.MUTATION_STARTED,
    )
    extra = _record(
        system_id,
        sequence=3,
        previous_digest=authority_system_record_digest(started),
        phase=(
            AuthoritySystemJournalPhase.PROVIDER_RETURNED
            if extra_generation == 1
            else AuthoritySystemJournalPhase.WATERMARK_INSTALLED
        ),
        generation=extra_generation,
    )
    for record in (admitted, started, extra):
        journal.append(record)
    journal.close()
    return system_id, (admitted, started, extra)


def _evidence_names(root: Path) -> list[str]:
    return sorted(os.listdir(root / "system-operations" / ABANDONED_DIRECTORY))


def test_abandon_unconfirmed_tail_keeps_prefix_and_evidence(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id, (admitted, started, extra) = _confirmed_then_extra(root)
    with FileAuthoritySystemJournal(root, system_id) as journal:
        journal.abandon_unconfirmed_tail(2)
        assert journal.read() == (admitted, started)
        successor = _record(
            system_id,
            sequence=3,
            previous_digest=authority_system_record_digest(started),
            phase=AuthoritySystemJournalPhase.WATERMARK_INSTALLED,
            generation=2,
        )
        journal.append(successor)
        assert journal.read() == (admitted, started, successor)
    directory = root / "system-operations" / ABANDONED_DIRECTORY
    digest = authority_system_record_digest(extra).removeprefix("sha256:")
    assert _evidence_names(root) == [f"{system_id}.3.{digest}.jsonl"]
    assert oct(directory.stat().st_mode & 0o777) == "0o700"
    evidence = directory / f"{system_id}.3.{digest}.jsonl"
    assert oct(evidence.stat().st_mode & 0o777) == "0o600"
    assert evidence.read_bytes() == canonical_system_authority_bytes(extra) + b"\n"
    assert [head.system_id for head in inventory_authority_system_journals(root)] == [system_id]


def test_abandon_refuses_without_exactly_one_extra_record(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id, _ = _confirmed_then_extra(root)
    with FileAuthoritySystemJournal(root, system_id) as journal:
        for confirmed in (1, 3):
            with pytest.raises(ValueError, match="no single unconfirmed record"):
                journal.abandon_unconfirmed_tail(confirmed)
        assert len(journal.read()) == 3
    assert not (root / "system-operations" / ABANDONED_DIRECTORY).exists()


def test_abandon_evidence_is_bounded_per_system(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id, (_, started, _) = _confirmed_then_extra(root)
    with FileAuthoritySystemJournal(root, system_id) as journal:
        journal.abandon_unconfirmed_tail(2)
        for generation in range(2, MAX_ABANDONED_PER_SYSTEM + 2):
            journal.append(
                _record(
                    system_id,
                    sequence=3,
                    previous_digest=authority_system_record_digest(started),
                    phase=AuthoritySystemJournalPhase.WATERMARK_INSTALLED,
                    generation=generation,
                )
            )
            if generation <= MAX_ABANDONED_PER_SYSTEM:
                journal.abandon_unconfirmed_tail(2)
        with pytest.raises(ValueError, match="evidence is full"):
            journal.abandon_unconfirmed_tail(2)
        assert len(journal.read()) == 3
    assert len(_evidence_names(root)) == MAX_ABANDONED_PER_SYSTEM


def test_abandon_is_idempotent_after_partial_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    system_id, (admitted, started, extra) = _confirmed_then_extra(root)
    real_rename = os.rename

    def crash_on_journal_rename(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(dst) == f"{system_id}.jsonl":
            raise OSError("simulated crash before the journal rename")
        return real_rename(src, dst, *args, **kwargs)

    with FileAuthoritySystemJournal(root, system_id) as journal:
        monkeypatch.setattr(os, "rename", crash_on_journal_rename)
        with pytest.raises(OSError, match="simulated crash"):
            journal.abandon_unconfirmed_tail(2)
        monkeypatch.undo()
        assert journal.read() == (admitted, started, extra)
        pending = root / "system-operations" / ABANDONED_DIRECTORY / f"{system_id}.pending"
        pending.write_bytes(b"short")
        journal.abandon_unconfirmed_tail(2)
        assert journal.read() == (admitted, started)
    assert len(_evidence_names(root)) == 1
    assert [head.sequence for head in inventory_authority_system_journals(root)] == [2]


def test_abandoned_directory_must_be_private_and_not_a_link(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id, _ = _confirmed_then_extra(root)
    directory = root / "system-operations" / ABANDONED_DIRECTORY
    other = tmp_path / "elsewhere"
    other.mkdir(mode=0o700)
    directory.symlink_to(other)
    with FileAuthoritySystemJournal(root, system_id) as journal:
        with pytest.raises(OSError):
            journal.abandon_unconfirmed_tail(2)
        assert len(journal.read()) == 3
    with pytest.raises(OSError):
        inventory_authority_system_journals(root)
    directory.unlink()
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with FileAuthoritySystemJournal(root, system_id) as journal, pytest.raises(PermissionError):
        journal.abandon_unconfirmed_tail(2)
    with pytest.raises(PermissionError):
        inventory_authority_system_journals(root)
    assert other.exists() and not list(other.iterdir())


def test_inventory_ignores_abandoned_directory(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id, _ = _confirmed_then_extra(root)
    with FileAuthoritySystemJournal(root, system_id) as journal:
        journal.abandon_unconfirmed_tail(2)
    heads = inventory_authority_system_journals(root)
    assert [(head.system_id, head.sequence) for head in heads] == [(system_id, 2)]


def test_abandon_evidence_is_bounded_across_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    first, _ = _confirmed_then_extra(root)
    second, _ = _confirmed_then_extra(root)
    monkeypatch.setattr(journal_module, "MAX_ABANDONED_FILES", 1)
    with FileAuthoritySystemJournal(root, first) as journal:
        journal.abandon_unconfirmed_tail(2)
    with FileAuthoritySystemJournal(root, second) as journal:
        with pytest.raises(ValueError, match="evidence is full"):
            journal.abandon_unconfirmed_tail(2)
        assert len(journal.read()) == 3


def test_abandon_refuses_a_journal_changed_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    system_id, records = _confirmed_then_extra(root)
    calls: list[str] = []
    real = FileAuthoritySystemJournal._write_scratch

    def touch_journal_after_prefix(directory: int, name: str, payload: bytes) -> None:
        real(directory, name, payload)
        calls.append(name)
        if len(calls) == 2:
            os.utime(root / "system-operations" / f"{system_id}.jsonl", ns=(1, 1))

    monkeypatch.setattr(
        FileAuthoritySystemJournal, "_write_scratch", staticmethod(touch_journal_after_prefix)
    )
    with FileAuthoritySystemJournal(root, system_id) as journal:
        with pytest.raises(ValueError, match="changed since validation"):
            journal.abandon_unconfirmed_tail(2)
        assert journal.read() == records


_TORN_CUTS = ("first-byte", "middle", "all-but-newline")


def _tear(root: Path, record: AuthoritySystemJournalRecordV1, cut: str) -> bytes:
    """Cut the journal's final line as a crash inside `append` would, returning what is left."""
    line = canonical_system_authority_bytes(record) + b"\n"
    kept = {"first-byte": 1, "middle": len(line) // 2, "all-but-newline": len(line) - 1}[cut]
    path = root / "system-operations" / f"{record.system_id}.jsonl"
    os.truncate(path, path.stat().st_size - len(line) + kept)
    return line[:kept]


@pytest.mark.parametrize("cut", _TORN_CUTS)
def test_abandon_torn_tail_keeps_prefix_and_evidence(tmp_path: Path, cut: str) -> None:
    root = _root(tmp_path)
    system_id, (admitted, started, extra) = _confirmed_then_extra(root)
    torn = _tear(root, extra, cut)
    head = authority_system_record_digest(started)
    with FileAuthoritySystemJournal(root, system_id) as journal:
        journal.abandon_torn_tail(2, head)
        assert journal.read() == (admitted, started)
        successor = _record(
            system_id,
            sequence=3,
            previous_digest=head,
            phase=AuthoritySystemJournalPhase.PROVIDER_RETURNED,
        )
        journal.append(successor)
        assert journal.read() == (admitted, started, successor)
    digest = hashlib.sha256(torn).hexdigest()
    assert _evidence_names(root) == [f"{system_id}.3.torn.{digest}.jsonl"]
    evidence = root / "system-operations" / ABANDONED_DIRECTORY / _evidence_names(root)[0]
    assert evidence.read_bytes() == torn
    assert oct(evidence.stat().st_mode & 0o777) == "0o600"
    assert [entry.sequence for entry in inventory_authority_system_journals(root)] == [3]


def test_abandon_torn_tail_of_the_first_record_leaves_an_empty_journal(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id = uuid4()
    first = _record(
        system_id,
        sequence=1,
        previous_digest=GENESIS_DIGEST,
        phase=AuthoritySystemJournalPhase.ADMITTED,
    )
    with FileAuthoritySystemJournal(root, system_id) as journal:
        journal.append(first)
    _tear(root, first, "middle")
    with FileAuthoritySystemJournal(root, system_id) as journal:
        journal.abandon_torn_tail(0, GENESIS_DIGEST)
        assert journal.read() == ()
        journal.append(first)
        assert journal.read() == (first,)
    assert len(_evidence_names(root)) == 1


@pytest.mark.parametrize(
    ("sequence", "digest_of"),
    [(3, "extra"), (1, "admitted"), (2, "wrong")],
    ids=["head-references-the-tail", "head-below-the-prefix", "digest-mismatch"],
)
def test_abandon_torn_tail_refuses_a_head_off_the_prefix(
    tmp_path: Path, sequence: int, digest_of: str
) -> None:
    root = _root(tmp_path)
    system_id, (admitted, _, extra) = _confirmed_then_extra(root)
    digests = {
        "extra": authority_system_record_digest(extra),
        "admitted": authority_system_record_digest(admitted),
        "wrong": _DIGEST,
    }
    _tear(root, extra, "middle")
    path = root / "system-operations" / f"{system_id}.jsonl"
    before = path.read_bytes()
    with (
        FileAuthoritySystemJournal(root, system_id) as journal,
        pytest.raises(ValueError, match="not past the head"),
    ):
        journal.abandon_torn_tail(sequence, digests[digest_of])
    assert path.read_bytes() == before
    assert not (root / "system-operations" / ABANDONED_DIRECTORY).exists()


def test_abandon_torn_tail_refuses_without_a_torn_tail(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id, (_, _, extra) = _confirmed_then_extra(root)
    head = authority_system_record_digest(extra)
    with FileAuthoritySystemJournal(root, system_id) as journal:
        with pytest.raises(ValueError, match="no torn final record"):
            journal.abandon_torn_tail(3, head)
        assert len(journal.read()) == 3
    with (
        FileAuthoritySystemJournal(root, uuid4()) as journal,
        pytest.raises(ValueError, match="no torn final record"),
    ):
        journal.abandon_torn_tail(0, GENESIS_DIGEST)


def test_abandon_torn_tail_refuses_corruption_before_the_tail(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id, (_, started, extra) = _confirmed_then_extra(root)
    _tear(root, extra, "middle")
    path = root / "system-operations" / f"{system_id}.jsonl"
    payload = path.read_bytes().replace(b'"phase":"admitted"', b'"phase":"terminal"', 1)
    path.write_bytes(payload)
    with (
        FileAuthoritySystemJournal(root, system_id) as journal,
        pytest.raises(ValueError, match="authority System journal|validation error"),
    ):
        journal.abandon_torn_tail(2, authority_system_record_digest(started))
    assert path.read_bytes() == payload
    assert not (root / "system-operations" / ABANDONED_DIRECTORY).exists()


def test_abandon_torn_tail_refuses_a_tail_no_record_could_leave(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id, (_, started, _) = _confirmed_then_extra(root)
    path = root / "system-operations" / f"{system_id}.jsonl"
    with path.open("ab") as journal_file:
        journal_file.write(b"x" * (MAX_MESSAGE_BYTES + 1))
    size = path.stat().st_size
    with (
        FileAuthoritySystemJournal(root, system_id) as journal,
        pytest.raises(ValueError, match="oversized"),
    ):
        journal.abandon_torn_tail(2, authority_system_record_digest(started))
    assert path.stat().st_size == size


def test_abandon_torn_tail_counts_against_evidence_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    system_id, (_, started, extra) = _confirmed_then_extra(root)
    torn = _tear(root, extra, "middle")
    monkeypatch.setattr(journal_module, "MAX_ABANDONED_PER_SYSTEM", 0)
    with (
        FileAuthoritySystemJournal(root, system_id) as journal,
        pytest.raises(ValueError, match="evidence is full"),
    ):
        journal.abandon_torn_tail(2, authority_system_record_digest(started))
    path = root / "system-operations" / f"{system_id}.jsonl"
    assert path.read_bytes().endswith(torn)


def test_abandon_torn_tail_is_idempotent_after_partial_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    system_id, (admitted, started, extra) = _confirmed_then_extra(root)
    _tear(root, extra, "middle")
    head = authority_system_record_digest(started)
    real_rename = os.rename

    def crash_on_journal_rename(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(dst) == f"{system_id}.jsonl":
            raise OSError("simulated crash before the journal rename")
        return real_rename(src, dst, *args, **kwargs)

    with FileAuthoritySystemJournal(root, system_id) as journal:
        monkeypatch.setattr(os, "rename", crash_on_journal_rename)
        with pytest.raises(OSError, match="simulated crash"):
            journal.abandon_torn_tail(2, head)
        monkeypatch.undo()
        with pytest.raises(ValueError, match="incomplete final record"):
            journal.read()
        journal.abandon_torn_tail(2, head)
        assert journal.read() == (admitted, started)
    assert len([name for name in _evidence_names(root) if name.endswith(".jsonl")]) == 1


def test_abandon_torn_tail_refuses_a_journal_changed_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    system_id, (_, started, extra) = _confirmed_then_extra(root)
    torn = _tear(root, extra, "middle")
    path = root / "system-operations" / f"{system_id}.jsonl"
    calls: list[str] = []
    real = FileAuthoritySystemJournal._write_scratch

    def touch_journal_after_prefix(directory: int, name: str, payload: bytes) -> None:
        real(directory, name, payload)
        calls.append(name)
        if len(calls) == 2:
            os.utime(path, ns=(1, 1))

    monkeypatch.setattr(
        FileAuthoritySystemJournal, "_write_scratch", staticmethod(touch_journal_after_prefix)
    )
    with (
        FileAuthoritySystemJournal(root, system_id) as journal,
        pytest.raises(ValueError, match="changed since validation"),
    ):
        journal.abandon_torn_tail(2, authority_system_record_digest(started))
    assert path.read_bytes().endswith(torn)


def test_torn_journal_still_refuses_read_head_and_append(tmp_path: Path) -> None:
    root = _root(tmp_path)
    system_id, (_, started, extra) = _confirmed_then_extra(root)
    _tear(root, extra, "all-but-newline")
    with FileAuthoritySystemJournal(root, system_id) as journal:
        with pytest.raises(ValueError, match="incomplete final record"):
            journal.read()
        with pytest.raises(ValueError, match="incomplete final record"):
            journal.head()
        with pytest.raises(ValueError, match="incomplete final record"):
            journal.append(
                _record(
                    system_id,
                    sequence=3,
                    previous_digest=authority_system_record_digest(started),
                    phase=AuthoritySystemJournalPhase.PROVIDER_RETURNED,
                )
            )
    assert not (root / "system-operations" / ABANDONED_DIRECTORY).exists()
