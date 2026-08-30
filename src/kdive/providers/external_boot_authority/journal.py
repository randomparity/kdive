"""Crash-safe append-only authority journal (ADR-0584)."""

from __future__ import annotations

import os
import stat
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
    JournalPhase.ADMITTED: frozenset({JournalPhase.MUTATION_STARTED, JournalPhase.TERMINAL}),
    JournalPhase.MUTATION_STARTED: frozenset({JournalPhase.PROVIDER_RETURNED}),
    JournalPhase.PROVIDER_RETURNED: frozenset({JournalPhase.OBSERVED}),
    JournalPhase.OBSERVED: frozenset({JournalPhase.TERMINAL}),
}
_INITIAL_OPERATION_PHASES = frozenset(
    {JournalPhase.WATERMARK_INSTALLED, JournalPhase.TAKEOVER_SUPERSEDED, JournalPhase.ADMITTED}
)


class FileAuthorityJournal:
    """One private newline-delimited journal whose writes are fsynced before return."""

    def __init__(self, path: Path, *, owner_uid: int | None = None) -> None:
        self._path = path
        self._owner_uid = os.geteuid() if owner_uid is None else owner_uid

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
            descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | _OPEN_BASE)
        try:
            self._validate_descriptor(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, created

    def load(self) -> tuple[JournalRecordV1, ...]:
        """Load and verify exact canonical bytes, sequence, chain, lane, and ownership."""
        if not self._path.exists() and not self._path.is_symlink():
            return ()
        descriptor = self._open_read()
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read()
        finally:
            os.close(descriptor)
        if not data:
            return ()
        if not data.endswith(b"\n"):
            raise ValueError("authority journal has a partial final record")
        records: list[JournalRecordV1] = []
        previous_digest = GENESIS_DIGEST
        lane: tuple[str, str] | None = None
        ownership: dict[str, tuple[str, str]] = {}
        operation_phases: dict[str, JournalPhase] = {}
        operation_bindings: dict[str, tuple[object, ...]] = {}
        for expected_sequence, line in enumerate(data[:-1].split(b"\n"), start=1):
            if not line or len(line) > MAX_MESSAGE_BYTES:
                raise ValueError("authority journal record is empty or oversized")
            record = JournalRecordV1.model_validate_json(line)
            if canonical_record_bytes(record) != line:
                raise ValueError("authority journal record is not canonical JSON")
            if record.sequence != expected_sequence:
                raise ValueError("authority journal sequence is not contiguous")
            if record.previous_digest != previous_digest:
                raise ValueError("authority journal digest chain is invalid")
            current_lane = (record.authority_instance, str(record.system_id))
            if lane is None:
                lane = current_lane
            elif current_lane != lane:
                raise ValueError("authority journal contains a foreign lane")
            operation_binding = (
                record.authority_id,
                record.generation,
                record.activation_id,
                record.run_id,
                record.plan_identity,
                record.purpose,
                record.provider_kind,
                record.operation_digest,
                record.attempt_id,
            )
            prior_binding = operation_bindings.setdefault(
                record.operation_identity, operation_binding
            )
            if prior_binding != operation_binding:
                raise ValueError("authority journal operation binding changed")
            prior_phase = operation_phases.get(record.operation_identity)
            allowed = (
                _INITIAL_OPERATION_PHASES
                if prior_phase is None
                else _NEXT_OPERATION_PHASES.get(prior_phase, frozenset())
            )
            if record.phase not in allowed:
                raise ValueError("authority journal phase ordering is invalid")
            operation_phases[record.operation_identity] = record.phase
            for item in record.recovery_objects:
                current_owner = (str(item.system_id), str(item.activation_id))
                prior_owner = ownership.setdefault(item.reference, current_owner)
                if prior_owner != current_owner:
                    raise ValueError("authority journal recovery ownership changed")
            previous_digest = record_digest(record)
            records.append(record)
        return tuple(records)

    def append(self, record: JournalRecordV1) -> None:
        """Append one record, fsyncing file and newly created parent entry before return."""
        current = self.load()
        expected_sequence = len(current) + 1
        expected_previous = GENESIS_DIGEST if not current else record_digest(current[-1])
        if record.sequence != expected_sequence or record.previous_digest != expected_previous:
            raise ValueError("authority journal append does not continue the exact head")
        if current and (
            record.authority_instance != current[0].authority_instance
            or record.system_id != current[0].system_id
        ):
            raise ValueError("authority journal append targets a foreign lane")
        encoded = canonical_record_bytes(record) + b"\n"
        descriptor, created = self._open_append()
        try:
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if created:
            parent = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY | _OPEN_BASE)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
