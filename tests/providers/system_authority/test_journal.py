"""Filesystem proofs for the per-System authority journal."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kdive.providers.system_authority.journal import (
    MAX_JOURNAL_FILES,
    FileAuthoritySystemJournal,
    inventory_authority_system_journals,
)
from kdive.providers.system_authority.protocol import (
    GENESIS_DIGEST,
    AuthoritySystemJournalPhase,
    AuthoritySystemJournalRecordV1,
    AuthoritySystemOperation,
    authority_system_record_digest,
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
        "generation": 1,
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
