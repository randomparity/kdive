"""Crash-safe append-only authority journal (ADR-0584)."""

from __future__ import annotations

import os
import stat
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from kdive.providers.external_boot_authority.protocol import (
    GENESIS_DIGEST,
    MAX_MESSAGE_BYTES,
    JournalPhase,
    JournalRecordV1,
    canonical_record_bytes,
    record_digest,
)

_OPEN_BASE = os.O_CLOEXEC | os.O_NOFOLLOW

_NEXT_OPERATION_PHASES = {
    JournalPhase.WATERMARK_INSTALLED: frozenset(
        {JournalPhase.TAKEOVER_SUPERSEDED, JournalPhase.TAKEOVER_ACKNOWLEDGED}
    ),
    JournalPhase.TAKEOVER_SUPERSEDED: frozenset({JournalPhase.WATERMARK_INSTALLED}),
    JournalPhase.ADMITTED: frozenset({JournalPhase.MUTATION_STARTED, JournalPhase.TERMINAL}),
    JournalPhase.MUTATION_STARTED: frozenset({JournalPhase.PROVIDER_RETURNED}),
    JournalPhase.PROVIDER_RETURNED: frozenset({JournalPhase.OBSERVED}),
    JournalPhase.OBSERVED: frozenset({JournalPhase.TERMINAL}),
}
_INITIAL_OPERATION_PHASES = frozenset(
    {JournalPhase.WATERMARK_INSTALLED, JournalPhase.TAKEOVER_SUPERSEDED, JournalPhase.ADMITTED}
)
DEFAULT_MAX_JOURNAL_BYTES = 64 * 1024 * 1024


@dataclass(slots=True)
class _ValidationState:
    previous_digest: str = GENESIS_DIGEST
    lane: tuple[str, str] | None = None
    ownership: dict[str, tuple[str, str]] = field(default_factory=dict)
    operation_phases: dict[str, JournalPhase] = field(default_factory=dict)
    operation_bindings: dict[str, tuple[object, ...]] = field(default_factory=dict)
    watermarks: dict[int, JournalRecordV1] = field(default_factory=dict)
    consumed_watermarks: set[tuple[int, str]] = field(default_factory=set)
    count: int = 0


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(slots=True)
class _JournalCache:
    records: tuple[JournalRecordV1, ...]
    state: _ValidationState
    identity: _FileIdentity | None
    tail_offset: int
    tail_bytes: bytes


class FileAuthorityJournal:
    """One private newline-delimited journal whose writes are fsynced before return."""

    def __init__(
        self,
        path: Path,
        *,
        owner_uid: int | None = None,
        max_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("authority journal maximum must be positive")
        self._path = path
        self._owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self._max_bytes = max_bytes
        self._cache: _JournalCache | None = None

    @staticmethod
    def _identity(status: os.stat_result) -> _FileIdentity:
        return _FileIdentity(
            status.st_dev,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        )

    def _validate_descriptor(self, descriptor: int) -> None:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise OSError("authority journal must be a regular file")
        if status.st_uid != self._owner_uid:
            raise PermissionError("authority journal is not owned by the service identity")
        if stat.S_IMODE(status.st_mode) & 0o022:
            raise PermissionError("authority journal must not be group- or other-writable")

    def _open_read(self) -> int:
        descriptor = os.open(self._path, os.O_RDONLY | _OPEN_BASE)
        try:
            self._validate_descriptor(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _open_append(self) -> tuple[int, bool]:
        created = False
        try:
            descriptor = os.open(
                self._path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | _OPEN_BASE,
                0o600,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(self._path, os.O_RDWR | os.O_APPEND | _OPEN_BASE)
        try:
            self._validate_descriptor(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, created

    @staticmethod
    def _validate_record(state: _ValidationState, record: JournalRecordV1) -> None:
        expected_sequence = state.count + 1
        if record.sequence != expected_sequence:
            raise ValueError("authority journal sequence is not contiguous")
        if record.previous_digest != state.previous_digest:
            raise ValueError("authority journal digest chain is invalid")
        current_lane = (record.authority_instance, str(record.system_id))
        if state.lane is None:
            state.lane = current_lane
        elif current_lane != state.lane:
            raise ValueError("authority journal contains a foreign lane")
        operation_binding = (
            record.authority_id,
            record.generation,
            record.activation_id,
            record.run_id,
            record.plan_identity,
            record.purpose,
            record.operation,
            record.provider_kind,
            record.operation_digest,
            record.attempt_id,
            record.expected_source_identity,
            record.intended_target_identity,
            record.recovery_objects,
        )
        if record.phase in {
            JournalPhase.TAKEOVER_SUPERSEDED,
            JournalPhase.TAKEOVER_ACKNOWLEDGED,
        }:
            linked_generation = (
                record.predecessor_generation
                if record.phase is JournalPhase.TAKEOVER_SUPERSEDED
                else record.generation
            )
            if linked_generation is None:
                raise ValueError("authority journal takeover watermark link is invalid")
            watermark = state.watermarks.get(linked_generation)
            if watermark is None or (
                record.watermark_sequence != watermark.sequence
                or record.watermark_digest != record_digest(watermark)
                or (
                    record.phase is JournalPhase.TAKEOVER_ACKNOWLEDGED
                    and record.operation_identity != watermark.operation_identity
                )
            ):
                raise ValueError("authority journal takeover watermark link is invalid")
            watermark_identity = (linked_generation, watermark.operation_identity)
            if watermark_identity in state.consumed_watermarks:
                raise ValueError("authority journal watermark is already superseded")
        prior_phase = state.operation_phases.get(record.operation_identity)
        allowed = (
            _INITIAL_OPERATION_PHASES
            if prior_phase is None
            else _NEXT_OPERATION_PHASES.get(prior_phase, frozenset())
        )
        if record.phase not in allowed:
            raise ValueError("authority journal phase ordering is invalid")
        prior_binding = state.operation_bindings.setdefault(
            record.operation_identity, operation_binding
        )
        if prior_binding != operation_binding:
            raise ValueError("authority journal operation binding changed")
        state.operation_phases[record.operation_identity] = record.phase
        if record.phase is JournalPhase.TAKEOVER_SUPERSEDED:
            state.consumed_watermarks.add(watermark_identity)
        if record.phase is JournalPhase.WATERMARK_INSTALLED:
            if record.generation in state.watermarks:
                raise ValueError("authority journal generation has multiple watermarks")
            state.watermarks[record.generation] = record
        for item in record.recovery_objects:
            current_owner = (str(item.system_id), str(item.activation_id))
            prior_owner = state.ownership.setdefault(item.reference, current_owner)
            if prior_owner != current_owner:
                raise ValueError("authority journal recovery ownership changed")
        state.previous_digest = record_digest(record)
        state.count += 1

    @classmethod
    def _validate_records(cls, records: tuple[JournalRecordV1, ...]) -> _ValidationState:
        state = _ValidationState()
        for record in records:
            cls._validate_record(state, record)
        return state

    def load(self) -> tuple[JournalRecordV1, ...]:
        """Load and verify exact canonical bytes, sequence, chain, lane, and ownership."""
        if not self._path.exists() and not self._path.is_symlink():
            self._cache = _JournalCache((), _ValidationState(), None, 0, b"")
            return ()
        descriptor = self._open_read()
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                status = os.fstat(descriptor)
                size = status.st_size
                if size > self._max_bytes:
                    raise ValueError("authority journal exceeds configured byte maximum")
                records: list[JournalRecordV1] = []
                state = _ValidationState()
                consumed = 0
                while line := stream.readline(MAX_MESSAGE_BYTES + 2):
                    consumed += len(line)
                    if consumed > self._max_bytes:
                        raise ValueError("authority journal exceeds configured byte maximum")
                    if not line.endswith(b"\n"):
                        raise ValueError("authority journal has a partial final record")
                    payload = line[:-1]
                    if not payload or len(payload) > MAX_MESSAGE_BYTES:
                        raise ValueError("authority journal record is empty or oversized")
                    record = JournalRecordV1.model_validate_json(payload)
                    if canonical_record_bytes(record) != payload:
                        raise ValueError("authority journal record is not canonical JSON")
                    self._validate_record(state, record)
                    records.append(record)
        finally:
            os.close(descriptor)
        result = tuple(records)
        tail_bytes = canonical_record_bytes(result[-1]) + b"\n" if result else b""
        self._cache = _JournalCache(
            result, state, self._identity(status), size - len(tail_bytes), tail_bytes
        )
        return result

    def append(self, record: JournalRecordV1) -> None:
        """Append one record, fsyncing file and newly created parent entry before return."""
        current = self.load() if self._cache is None else self._cache.records
        assert self._cache is not None
        state = deepcopy(self._cache.state)
        encoded = canonical_record_bytes(record) + b"\n"
        cached_size = self._cache.identity.size if self._cache.identity is not None else 0
        if cached_size + len(encoded) > self._max_bytes:
            raise ValueError("authority journal append exceeds configured byte maximum")
        self._validate_record(state, record)
        descriptor, created = self._open_append()
        try:
            before = os.fstat(descriptor)
            if self._cache.identity is None:
                if not created or before.st_size != 0:
                    raise ValueError("authority journal changed since validation")
            elif self._identity(before) != self._cache.identity:
                raise ValueError("authority journal changed since validation")
            if self._cache.tail_bytes and (
                os.pread(descriptor, len(self._cache.tail_bytes), self._cache.tail_offset)
                != self._cache.tail_bytes
            ):
                raise ValueError("authority journal tail changed since validation")
            path_status = os.stat(self._path, follow_symlinks=False)
            after_check = os.fstat(descriptor)
            if (
                self._identity(after_check) != self._identity(before)
                or path_status.st_dev != after_check.st_dev
                or path_status.st_ino != after_check.st_ino
            ):
                raise ValueError("authority journal changed since validation")
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
            final_status = os.fstat(descriptor)
            final_path_status = os.stat(self._path, follow_symlinks=False)
            if (
                final_path_status.st_dev != final_status.st_dev
                or final_path_status.st_ino != final_status.st_ino
            ):
                raise ValueError("authority journal changed during append")
        finally:
            os.close(descriptor)
        if created:
            parent = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY | _OPEN_BASE)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        final_identity = self._identity(final_status)
        self._cache = _JournalCache(
            (*current, record), state, final_identity, final_identity.size - len(encoded), encoded
        )
