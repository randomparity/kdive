"""Crash-detecting per-System authority journal (ADR-0623)."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.providers.system_authority.protocol import (
    GENESIS_DIGEST,
    MAX_MESSAGE_BYTES,
    AuthoritySystemJournalPhase,
    AuthoritySystemJournalRecordV1,
    authority_system_record_digest,
    canonical_system_authority_bytes,
)

MAX_JOURNAL_FILES = 4096
MAX_RECORDS_PER_JOURNAL = 1024
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
_OPEN = os.O_CLOEXEC | os.O_NOFOLLOW

_NEXT_PHASES = {
    AuthoritySystemJournalPhase.WATERMARK_INSTALLED: frozenset(
        {
            AuthoritySystemJournalPhase.TAKEOVER_SUPERSEDED,
            AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED,
        }
    ),
    AuthoritySystemJournalPhase.TAKEOVER_SUPERSEDED: frozenset(
        {AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED}
    ),
    AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED: frozenset(
        {AuthoritySystemJournalPhase.ADMITTED}
    ),
    AuthoritySystemJournalPhase.ADMITTED: frozenset(
        {AuthoritySystemJournalPhase.MUTATION_STARTED, AuthoritySystemJournalPhase.TERMINAL}
    ),
    AuthoritySystemJournalPhase.MUTATION_STARTED: frozenset(
        {AuthoritySystemJournalPhase.PROVIDER_RETURNED}
    ),
    AuthoritySystemJournalPhase.PROVIDER_RETURNED: frozenset(
        {AuthoritySystemJournalPhase.OBSERVED}
    ),
    AuthoritySystemJournalPhase.OBSERVED: frozenset({AuthoritySystemJournalPhase.TERMINAL}),
    AuthoritySystemJournalPhase.TERMINAL: frozenset(
        {AuthoritySystemJournalPhase.WATERMARK_INSTALLED, AuthoritySystemJournalPhase.ADMITTED}
    ),
}
_INITIAL_PHASES = frozenset(
    {AuthoritySystemJournalPhase.WATERMARK_INSTALLED, AuthoritySystemJournalPhase.ADMITTED}
)


@dataclass(frozen=True, slots=True)
class AuthoritySystemFileHead:
    system_id: UUID
    sequence: int
    digest: str
    phase: AuthoritySystemJournalPhase | None


def _validate_directory(descriptor: int, owner_uid: int) -> None:
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        raise OSError("authority System journal root must be a directory")
    if status.st_uid != owner_uid:
        raise PermissionError("authority System journal root has foreign ownership")
    if stat.S_IMODE(status.st_mode) != 0o700:
        raise PermissionError("authority System journal root must have exact mode 0700")


class FileAuthoritySystemJournal:
    """Append and fully revalidate one fixed System journal."""

    def __init__(self, state_root: Path, system_id: UUID, *, owner_uid: int | None = None) -> None:
        self._owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self._system_id = system_id
        root_fd = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | _OPEN)
        try:
            _validate_directory(root_fd, self._owner_uid)
            journal_fd = os.open(
                "system-operations", os.O_RDONLY | os.O_DIRECTORY | _OPEN, dir_fd=root_fd
            )
        except BaseException:
            os.close(root_fd)
            raise
        try:
            _validate_directory(journal_fd, self._owner_uid)
        except BaseException:
            os.close(journal_fd)
            os.close(root_fd)
            raise
        self._root_fd = root_fd
        self._journal_fd = journal_fd
        self._name = f"{system_id}.jsonl"

    def close(self) -> None:
        journal_fd = getattr(self, "_journal_fd", -1)
        root_fd = getattr(self, "_root_fd", -1)
        self._journal_fd = self._root_fd = -1
        if journal_fd >= 0:
            os.close(journal_fd)
        if root_fd >= 0:
            os.close(root_fd)

    def __enter__(self) -> FileAuthoritySystemJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _open(self, *, create: bool = False) -> int | None:
        flags = os.O_RDONLY | _OPEN
        if create:
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _OPEN
        try:
            descriptor = os.open(self._name, flags, 0o600, dir_fd=self._journal_fd)
        except FileNotFoundError:
            return None
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            os.close(descriptor)
            raise OSError("authority System journal must be a regular file")
        if status.st_uid != self._owner_uid:
            os.close(descriptor)
            raise PermissionError("authority System journal has foreign ownership")
        if stat.S_IMODE(status.st_mode) != 0o600:
            os.close(descriptor)
            raise PermissionError("authority System journal must have exact mode 0600")
        return descriptor

    def read(self) -> tuple[AuthoritySystemJournalRecordV1, ...]:
        descriptor = self._open()
        if descriptor is None:
            return ()
        try:
            size = os.fstat(descriptor).st_size
            if size > MAX_JOURNAL_BYTES:
                raise ValueError("authority System journal exceeds 16 MiB")
            payload = b""
            while len(payload) <= MAX_JOURNAL_BYTES:
                block = os.read(descriptor, min(1024 * 1024, MAX_JOURNAL_BYTES + 1 - len(payload)))
                if not block:
                    break
                payload += block
        finally:
            os.close(descriptor)
        if len(payload) > MAX_JOURNAL_BYTES:
            raise ValueError("authority System journal exceeds 16 MiB")
        if payload and not payload.endswith(b"\n"):
            raise ValueError("authority System journal has an incomplete final record")
        lines = payload.splitlines()
        if len(lines) > MAX_RECORDS_PER_JOURNAL:
            raise ValueError("authority System journal exceeds 1024 records")
        records: list[AuthoritySystemJournalRecordV1] = []
        prior_digest = GENESIS_DIGEST
        prior_phase: AuthoritySystemJournalPhase | None = None
        prior_generation = 0
        operation_binding: tuple[object, ...] | None = None
        for sequence, line in enumerate(lines, start=1):
            if not line or len(line) > MAX_MESSAGE_BYTES:
                raise ValueError("authority System journal record size is invalid")
            record = AuthoritySystemJournalRecordV1.model_validate_json(line, strict=True)
            if canonical_system_authority_bytes(record) != line:
                raise ValueError("authority System journal record is not canonical")
            if record.system_id != self._system_id or record.sequence != sequence:
                raise ValueError("authority System journal record binding is invalid")
            if record.previous_digest != prior_digest:
                raise ValueError("authority System journal digest chain is invalid")
            legal = _INITIAL_PHASES if prior_phase is None else _NEXT_PHASES[prior_phase]
            if record.phase not in legal:
                raise ValueError("authority System journal phase transition is invalid")
            if record.generation < prior_generation:
                raise ValueError("authority System journal generation moved backward")
            binding = (
                record.authority_id,
                record.generation,
                record.attempt_id,
                record.operation,
                record.operation_identity,
                record.operation_digest,
                record.bootstrap_identity,
            )
            begins_operation = (
                prior_phase is None or prior_phase is AuthoritySystemJournalPhase.TERMINAL
            )
            if begins_operation:
                operation_binding = binding
            elif binding != operation_binding:
                raise ValueError("authority System journal operation binding changed")
            prior_digest = authority_system_record_digest(record)
            prior_phase = record.phase
            prior_generation = record.generation
            records.append(record)
        return tuple(records)

    def head(self) -> AuthoritySystemFileHead:
        records = self.read()
        if not records:
            return AuthoritySystemFileHead(self._system_id, 0, GENESIS_DIGEST, None)
        last = records[-1]
        return AuthoritySystemFileHead(
            self._system_id, last.sequence, authority_system_record_digest(last), last.phase
        )

    def append(self, record: AuthoritySystemJournalRecordV1) -> AuthoritySystemFileHead:
        record = AuthoritySystemJournalRecordV1.model_validate(
            record.model_dump(mode="python", by_alias=True)
        )
        records = self.read()
        if records:
            last = records[-1]
            head = AuthoritySystemFileHead(
                self._system_id,
                last.sequence,
                authority_system_record_digest(last),
                last.phase,
            )
        else:
            last = None
            head = AuthoritySystemFileHead(self._system_id, 0, GENESIS_DIGEST, None)
        if record.system_id != self._system_id or record.sequence != head.sequence + 1:
            raise ValueError("authority System journal append sequence is invalid")
        if record.previous_digest != head.digest:
            raise ValueError("authority System journal append previous digest is invalid")
        legal = _INITIAL_PHASES if head.phase is None else _NEXT_PHASES[head.phase]
        if record.phase not in legal:
            raise ValueError("authority System journal append phase is invalid")
        if last is not None and last.phase is not AuthoritySystemJournalPhase.TERMINAL:
            prior_binding = (
                last.authority_id,
                last.generation,
                last.attempt_id,
                last.operation,
                last.operation_identity,
                last.operation_digest,
                last.bootstrap_identity,
            )
            new_binding = (
                record.authority_id,
                record.generation,
                record.attempt_id,
                record.operation,
                record.operation_identity,
                record.operation_digest,
                record.bootstrap_identity,
            )
            if new_binding != prior_binding:
                raise ValueError("authority System journal append binding changed")
        encoded = canonical_system_authority_bytes(record) + b"\n"
        descriptor = self._open(create=True)
        assert descriptor is not None
        try:
            if os.fstat(descriptor).st_size + len(encoded) > MAX_JOURNAL_BYTES:
                raise ValueError("authority System journal append exceeds 16 MiB")
            if head.sequence >= MAX_RECORDS_PER_JOURNAL:
                raise ValueError("authority System journal append exceeds 1024 records")
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("authority System journal append made no progress")
                offset += written
            os.fsync(descriptor)
            os.fsync(self._journal_fd)
        finally:
            os.close(descriptor)
        return AuthoritySystemFileHead(
            self._system_id, record.sequence, authority_system_record_digest(record), record.phase
        )


def inventory_authority_system_journals(
    state_root: Path, *, owner_uid: int | None = None
) -> tuple[AuthoritySystemFileHead, ...]:
    """Validate the bounded fixed directory and return path-free heads."""
    expected_uid = os.geteuid() if owner_uid is None else owner_uid
    root_fd = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | _OPEN)
    try:
        _validate_directory(root_fd, expected_uid)
        directory_fd = os.open(
            "system-operations", os.O_RDONLY | os.O_DIRECTORY | _OPEN, dir_fd=root_fd
        )
        try:
            _validate_directory(directory_fd, expected_uid)
            names = os.listdir(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(root_fd)
    if len(names) > MAX_JOURNAL_FILES:
        raise ValueError("authority System journal inventory exceeds 4096 files")
    system_ids: list[UUID] = []
    for name in names:
        if not name.endswith(".jsonl"):
            raise ValueError("authority System journal inventory has an invalid filename")
        parsed = UUID(name.removesuffix(".jsonl"))
        if name != f"{parsed}.jsonl":
            raise ValueError("authority System journal filename is not canonical")
        system_ids.append(parsed)
    heads: list[AuthoritySystemFileHead] = []
    for system_id in sorted(system_ids, key=str):
        with FileAuthoritySystemJournal(state_root, system_id, owner_uid=expected_uid) as journal:
            heads.append(journal.head())
    return tuple(heads)
