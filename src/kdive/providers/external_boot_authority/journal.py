"""Crash-safe append-only authority journal (ADR-0584)."""

from __future__ import annotations

import os
import stat
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
class _ValidationDelta:
    lane: tuple[str, str] | None
    operation_identity: str
    operation_binding: tuple[object, ...] | None
    phase: JournalPhase
    watermark: tuple[int, JournalRecordV1] | None
    consumed_watermark: tuple[int, str] | None
    ownership: tuple[tuple[str, tuple[str, str]], ...]
    previous_digest: str


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(slots=True)
class _JournalCache:
    records: list[JournalRecordV1]
    state: _ValidationState
    identity: _FileIdentity | None
    tail_offset: int
    tail_bytes: bytes


class FileAuthorityJournal:
    """One private newline-delimited journal whose writes are fsynced before return."""

    def __init__(
        self,
        trusted_root: Path,
        lane_path: str | Path,
        *,
        owner_uid: int | None = None,
        max_bytes: int = DEFAULT_MAX_JOURNAL_BYTES,
    ) -> None:
        self._directory_fds: tuple[int, ...] = ()
        if max_bytes < 1:
            raise ValueError("authority journal maximum must be positive")
        raw_path = os.fspath(lane_path)
        parts = raw_path.split("/")
        if raw_path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("authority journal requires a confined relative lane path")
        self._trusted_root = trusted_root
        self._parts = tuple(parts)
        self._name = parts[-1]
        self._owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self._max_bytes = max_bytes
        self._cache: _JournalCache | None = None
        descriptors: list[int] = []
        try:
            root_fd = os.open(trusted_root, os.O_RDONLY | os.O_DIRECTORY | _OPEN_BASE)
            descriptors.append(root_fd)
            self._validate_directory_descriptor(root_fd)
            current = root_fd
            for component in parts[:-1]:
                current = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | _OPEN_BASE,
                    dir_fd=current,
                )
                descriptors.append(current)
                self._validate_directory_descriptor(current)
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise
        self._directory_fds = tuple(descriptors)

    def close(self) -> None:
        """Release retained trusted-directory descriptors."""
        descriptors, self._directory_fds = self._directory_fds, ()
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    def __del__(self) -> None:
        if hasattr(self, "_directory_fds"):
            self.close()

    @property
    def _parent_fd(self) -> int:
        if not self._directory_fds:
            raise ValueError("authority journal is closed")
        return self._directory_fds[-1]

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
        if stat.S_IMODE(status.st_mode) != 0o600:
            raise PermissionError("authority journal must have exact mode 0600")

    def _validate_directory_descriptor(self, descriptor: int) -> None:
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode):
            raise OSError("authority journal trusted directory must be a directory")
        if status.st_uid != self._owner_uid:
            raise PermissionError("authority journal trusted directory has foreign ownership")
        if stat.S_IMODE(status.st_mode) & 0o022:
            raise PermissionError("authority journal trusted directory must not be writable")

    def _validate_directory_chain(self) -> None:
        if not self._directory_fds:
            raise ValueError("authority journal is closed")
        try:
            root_status = os.stat(self._trusted_root, follow_symlinks=False)
        except OSError:
            raise ValueError("authority journal trusted directory chain changed") from None
        retained_root = os.fstat(self._directory_fds[0])
        if (root_status.st_dev, root_status.st_ino) != (
            retained_root.st_dev,
            retained_root.st_ino,
        ):
            raise ValueError("authority journal trusted directory chain changed")
        for index, component in enumerate(self._parts[:-1]):
            try:
                linked = os.stat(
                    component, dir_fd=self._directory_fds[index], follow_symlinks=False
                )
            except OSError:
                raise ValueError("authority journal trusted directory chain changed") from None
            retained = os.fstat(self._directory_fds[index + 1])
            if not stat.S_ISDIR(linked.st_mode) or (linked.st_dev, linked.st_ino) != (
                retained.st_dev,
                retained.st_ino,
            ):
                raise ValueError("authority journal trusted directory chain changed")
        for descriptor in self._directory_fds:
            self._validate_directory_descriptor(descriptor)

    def _entry_status(self) -> os.stat_result | None:
        self._validate_directory_chain()
        try:
            return os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _open_read(self) -> int:
        self._validate_directory_chain()
        descriptor = os.open(self._name, os.O_RDONLY | _OPEN_BASE, dir_fd=self._parent_fd)
        try:
            self._validate_descriptor(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _open_append(self) -> tuple[int, bool]:
        self._validate_directory_chain()
        created = False
        try:
            descriptor = os.open(
                self._name,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | _OPEN_BASE,
                0o600,
                dir_fd=self._parent_fd,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(
                self._name, os.O_RDWR | os.O_APPEND | _OPEN_BASE, dir_fd=self._parent_fd
            )
        try:
            self._validate_descriptor(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, created

    @staticmethod
    def _prepare_record(state: _ValidationState, record: JournalRecordV1) -> _ValidationDelta:
        expected_sequence = state.count + 1
        if record.sequence != expected_sequence:
            raise ValueError("authority journal sequence is not contiguous")
        if record.previous_digest != state.previous_digest:
            raise ValueError("authority journal digest chain is invalid")
        current_lane = (record.authority_instance, str(record.system_id))
        if state.lane is None:
            new_lane = current_lane
        elif current_lane != state.lane:
            raise ValueError("authority journal contains a foreign lane")
        else:
            new_lane = None
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
        prior_binding = state.operation_bindings.get(record.operation_identity)
        if prior_binding != operation_binding:
            if prior_binding is not None:
                raise ValueError("authority journal operation binding changed")
            new_binding: tuple[object, ...] | None = operation_binding
        else:
            new_binding = None
        consumed = watermark_identity if record.phase is JournalPhase.TAKEOVER_SUPERSEDED else None
        new_watermark = None
        if record.phase is JournalPhase.WATERMARK_INSTALLED:
            if record.generation in state.watermarks:
                raise ValueError("authority journal generation has multiple watermarks")
            new_watermark = (record.generation, record)
        new_ownership: list[tuple[str, tuple[str, str]]] = []
        for item in record.recovery_objects:
            current_owner = (str(item.system_id), str(item.activation_id))
            prior_owner = state.ownership.get(item.reference)
            if prior_owner is not None and prior_owner != current_owner:
                raise ValueError("authority journal recovery ownership changed")
            if prior_owner is None:
                new_ownership.append((item.reference, current_owner))
        return _ValidationDelta(
            new_lane,
            record.operation_identity,
            new_binding,
            record.phase,
            new_watermark,
            consumed,
            tuple(new_ownership),
            record_digest(record),
        )

    @staticmethod
    def _apply_delta(state: _ValidationState, delta: _ValidationDelta) -> None:
        if delta.lane is not None:
            state.lane = delta.lane
        if delta.operation_binding is not None:
            state.operation_bindings[delta.operation_identity] = delta.operation_binding
        state.operation_phases[delta.operation_identity] = delta.phase
        if delta.watermark is not None:
            state.watermarks[delta.watermark[0]] = delta.watermark[1]
        if delta.consumed_watermark is not None:
            state.consumed_watermarks.add(delta.consumed_watermark)
        state.ownership.update(delta.ownership)
        state.previous_digest = delta.previous_digest
        state.count += 1

    @classmethod
    def _validate_record(cls, state: _ValidationState, record: JournalRecordV1) -> None:
        cls._apply_delta(state, cls._prepare_record(state, record))

    @classmethod
    def _validate_records(cls, records: tuple[JournalRecordV1, ...]) -> _ValidationState:
        state = _ValidationState()
        for record in records:
            cls._validate_record(state, record)
        return state

    def load(self) -> tuple[JournalRecordV1, ...]:
        """Load and verify exact canonical bytes, sequence, chain, lane, and ownership."""
        if self._entry_status() is None:
            self._cache = _JournalCache([], _ValidationState(), None, 0, b"")
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
            records, state, self._identity(status), size - len(tail_bytes), tail_bytes
        )
        return result

    def append(self, record: JournalRecordV1) -> None:
        """Append one record, fsyncing file and newly created parent entry before return."""
        if self._cache is None:
            self.load()
        assert self._cache is not None
        state = self._cache.state
        encoded = canonical_record_bytes(record) + b"\n"
        cached_size = self._cache.identity.size if self._cache.identity is not None else 0
        if cached_size + len(encoded) > self._max_bytes:
            raise ValueError("authority journal append exceeds configured byte maximum")
        delta = self._prepare_record(state, record)
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
            self._validate_directory_chain()
            path_status = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
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
            self._validate_directory_chain()
            final_path_status = os.stat(self._name, dir_fd=self._parent_fd, follow_symlinks=False)
            if (
                final_path_status.st_dev != final_status.st_dev
                or final_path_status.st_ino != final_status.st_ino
            ):
                raise ValueError("authority journal changed during append")
        finally:
            os.close(descriptor)
        if created:
            os.fsync(self._parent_fd)
        final_identity = self._identity(final_status)
        self._apply_delta(state, delta)
        self._cache.records.append(record)
        self._cache.identity = final_identity
        self._cache.tail_offset = final_identity.size - len(encoded)
        self._cache.tail_bytes = encoded
