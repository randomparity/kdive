"""Crash-safe, fixed-path persistence for one systemd worker slot (ADR-0574)."""

from __future__ import annotations

import hashlib
import os
import pwd
import secrets
import shlex
import socket
import stat
import warnings
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from kdive.processes.lifecycle.systemd.systemd_worker_contract import (
    SlotPhase,
    WorkerSettings,
    validate_utf8_bytes,
)
from kdive.services.runs.worker_incarnations import LocalAuthorityBinding
from kdive.worker_lifecycle.contracts import TerminationOutcome

_HEX_32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
_HEX_64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_STATE_MODE = 0o600
_ENVIRONMENT_MODE = 0o600
_CREDENTIAL_MODE = 0o400
_RELEASE_MODE = 0o440
_SLOTS_MODE = 0o711
_MAX_STATE_BYTES = 65_536


class StateConflict(RuntimeError):
    """A retained slot state cannot safely be replaced or cleaned."""


with warnings.catch_warnings():
    # ``schema`` is the durable on-disk field mandated by the lifecycle contract. Pydantic also
    # retains a deprecated BaseModel.schema method, so it warns while constructing this model.
    warnings.filterwarnings("ignore", message='Field name "schema"', category=UserWarning)

    class SlotState(BaseModel):
        """Immutable non-secret facts retained for one fixed worker slot."""

        model_config = ConfigDict(extra="forbid", frozen=True)

        schema: Literal[1]
        slot: int = Field(ge=1, le=8)
        unit: Annotated[str, StringConstraints(max_length=128)]
        generation: _HEX_32
        incarnation: Annotated[str, StringConstraints(max_length=512)]
        credential_hash: _HEX_64
        phase: SlotPhase
        boot_id: Annotated[str, StringConstraints(max_length=128)] | None = None
        invocation_id: Annotated[str, StringConstraints(max_length=128)] | None = None
        outcome: TerminationOutcome | None = None

        @model_validator(mode="after")
        def validate_identity(self) -> Self:
            """Keep each persisted state bound to its fixed slot and derived holder."""
            unit = f"kdive-live-worker@{self.slot}.service"
            incarnation = f"local-systemd:{unit}:{self.generation}"
            if self.unit != unit or self.incarnation != incarnation:
                raise ValueError("slot state must use its derived systemd identity")
            needs_binding = self.phase is not SlotPhase.PREPARED
            if needs_binding and (not self.boot_id or not self.invocation_id):
                raise ValueError("gated and later state requires boot and invocation identifiers")
            if not needs_binding and (self.boot_id is not None or self.invocation_id is not None):
                raise ValueError("prepared state has no systemd binding")
            if (self.phase is SlotPhase.TERMINATED) != (self.outcome is not None):
                raise ValueError("only terminated state has a terminal outcome")
            return self

        @field_validator("unit", "incarnation", "boot_id", "invocation_id")
        @classmethod
        def validate_string_bytes(cls, value: str | None, info: ValidationInfo) -> str | None:
            """Keep persisted identity strings inside their protocol byte limits."""
            if value is not None:
                limits = {"unit": 128, "incarnation": 512, "boot_id": 128, "invocation_id": 128}
                field = info.field_name
                if field is None:
                    raise RuntimeError("slot state byte validator has no field name")
                validate_utf8_bytes(value, limits[field])
            return value

        def authority_binding(self) -> LocalAuthorityBinding:
            """Return the exact non-secret binding required by the witness authority."""
            if self.boot_id is None or self.invocation_id is None:
                raise StateConflict("registered state requires boot and invocation identifiers")
            return {
                "unit": self.unit,
                "generation": self.generation,
                "boot_id": self.boot_id,
                "invocation_id": self.invocation_id,
                "host": socket.gethostname(),
            }


class SlotStore:
    """Persist one derived systemd worker slot without caller-selected descendants."""

    def __init__(self, *, root: Path, slot: int) -> None:
        if slot not in range(1, 9):
            raise ValueError("slot must be in 1..8")
        self.root = root
        self.slot = slot
        self.unit = f"kdive-live-worker@{slot}.service"
        self.slots_path = root / "slots"
        self.slot_path = self.slots_path / str(slot)
        self.state_path = self.slot_path / "state.json"
        self.environment_path = self.slot_path / "worker.env"
        self.credential_path = self.slot_path / "worker-incarnation.credential"
        self.release_path = self.slot_path / "release"

    def prepare(self, settings: WorkerSettings | None) -> SlotState:
        """Mint and durably publish a new prepared generation for this empty slot."""
        if settings is None:
            raise ValueError("prepared slot requires validated worker settings")
        self._require_root()
        descriptor = self._slot_descriptor(create=True)
        if descriptor is None:
            raise RuntimeError("failed to create fixed systemd worker slot directory")
        try:
            if self._exists(descriptor, "state.json"):
                raise StateConflict(f"slot {self.slot} has retained state")
            if self._exists(descriptor, "release"):
                raise StateConflict(f"slot {self.slot} has a retained release marker")
            generation = secrets.token_hex(16)
            credential = secrets.token_urlsafe(32)
            state = SlotState(
                schema=1,
                slot=self.slot,
                unit=self.unit,
                generation=generation,
                incarnation=f"local-systemd:{self.unit}:{generation}",
                credential_hash=hashlib.sha256(credential.encode("utf-8")).hexdigest(),
                phase=SlotPhase.PREPARED,
            )
            self._write(
                descriptor,
                "worker.env",
                self._environment(settings, state).encode(),
                _ENVIRONMENT_MODE,
            )
            self._write(
                descriptor, "worker-incarnation.credential", credential.encode(), _CREDENTIAL_MODE
            )
            self._write(descriptor, "state.json", state.model_dump_json().encode(), _STATE_MODE)
            return state
        finally:
            os.close(descriptor)

    def load(self) -> SlotState | None:
        """Load the current fixed state document, or ``None`` for an empty slot."""
        descriptor = self._slot_descriptor(create=False)
        if descriptor is None:
            return None
        try:
            try:
                data = self._read(descriptor, "state.json")
            except FileNotFoundError:
                return None
            return SlotState.model_validate_json(data)
        except ValueError as exc:
            raise StateConflict(f"slot {self.slot} state is malformed") from exc
        finally:
            os.close(descriptor)

    def persist(self, state: SlotState) -> None:
        """Durably replace the state document for this exact fixed slot."""
        self._require_root()
        state = self._validated_state(state)
        self._validate_transition(self.load(), state)
        descriptor = self._slot_descriptor(create=True)
        if descriptor is None:
            raise RuntimeError("failed to create fixed systemd worker slot directory")
        try:
            self._write(descriptor, "state.json", state.model_dump_json().encode(), _STATE_MODE)
        finally:
            os.close(descriptor)

    def publish_release(self, state: SlotState) -> None:
        """Atomically release an already registered exact systemd invocation."""
        self._require_root()
        state = self._validated_state(state)
        if state.phase is not SlotPhase.REGISTERED:
            raise StateConflict("only registered state with an invocation may release a worker")
        state.authority_binding()
        if self.load() != state:
            raise StateConflict("release requires the retained registered state")
        descriptor = self._slot_descriptor(create=True)
        if descriptor is None:
            raise RuntimeError("failed to create fixed systemd worker slot directory")
        try:
            release = f"{state.generation}\n{state.invocation_id}\n".encode()
            self._write(descriptor, "release", release, _RELEASE_MODE)
        finally:
            os.close(descriptor)

    def discard_prepared(self, state: SlotState) -> None:
        """Remove one exact prepared generation proven to have no unit invocation."""
        self._require_root()
        state = self._validated_state(state)
        if state.phase is not SlotPhase.PREPARED:
            raise StateConflict("discard requires prepared state")
        if self.load() != state:
            raise StateConflict("discard requires the retained prepared state")
        descriptor = self._slot_descriptor(create=False)
        if descriptor is None:
            raise StateConflict("discard requires the retained prepared slot")
        try:
            if self._exists(descriptor, "release"):
                raise StateConflict("prepared state cannot have a release marker")
            for name in ("worker.env", "worker-incarnation.credential"):
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
            os.unlink("state.json", dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def cleanup_terminated(self, state: SlotState) -> None:
        """Remove the retained handoff only after exact terminal evidence is persisted."""
        self._require_root()
        state = self._validated_state(state)
        if state.phase is not SlotPhase.TERMINATED:
            raise StateConflict("cleanup requires terminated state")
        retained = self.load()
        if retained != state:
            raise StateConflict("cleanup requires the retained terminated state")
        descriptor = self._slot_descriptor(create=True)
        if descriptor is None:
            raise RuntimeError("failed to create fixed systemd worker slot directory")
        try:
            for name in ("worker.env", "worker-incarnation.credential", "release"):
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
            with suppress(FileNotFoundError):
                os.unlink("state.json", dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _slot_descriptor(self, *, create: bool) -> int | None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        root = os.open(self.root, flags)
        try:
            slots = self._open_directory(root, "slots", create, _SLOTS_MODE)
            if slots is None:
                return None
            try:
                if create:
                    self._set_slots_permissions(slots)
                else:
                    self._validate_slots_permissions(slots)
                descriptor = self._open_directory(slots, str(self.slot), create, 0o750)
                if descriptor is None:
                    return None
                if create:
                    self._set_slot_permissions(descriptor)
                else:
                    self._validate_slot_permissions(descriptor)
                return descriptor
            finally:
                os.close(slots)
        finally:
            os.close(root)

    def _open_directory(self, parent: int, name: str, create: bool, mode: int) -> int | None:
        if create:
            try:
                os.mkdir(name, mode, dir_fd=parent)
            except FileExistsError:
                pass
            else:
                os.fsync(parent)
        try:
            return os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        except FileNotFoundError:
            return None

    def _set_slot_permissions(self, descriptor: int) -> None:
        account = pwd.getpwnam(f"kdive-worker-{self.slot}")
        os.fchmod(descriptor, 0o750)
        os.fchown(descriptor, 0, account.pw_gid)

    @staticmethod
    def _set_slots_permissions(descriptor: int) -> None:
        os.fchmod(descriptor, _SLOTS_MODE)
        os.fchown(descriptor, 0, 0)

    @staticmethod
    def _validate_slots_permissions(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != _SLOTS_MODE
            or metadata.st_uid != 0
            or metadata.st_gid != 0
        ):
            raise StateConflict("slots directory has untrusted traversal metadata")

    def _validate_slot_permissions(self, descriptor: int) -> None:
        account = pwd.getpwnam(f"kdive-worker-{self.slot}")
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o750
            or metadata.st_uid != 0
            or metadata.st_gid != account.pw_gid
        ):
            raise StateConflict(f"slot {self.slot} directory has untrusted metadata")

    def _write(self, parent: int, name: str, data: bytes, mode: int) -> None:
        temporary = f".{name}.{secrets.token_hex(8)}"
        created = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                mode,
                dir_fd=parent,
            )
            created = True
            account = pwd.getpwnam(f"kdive-worker-{self.slot}")
            try:
                os.fchmod(descriptor, mode)
                os.fchown(descriptor, 0, account.pw_gid)
                self._write_all(descriptor, data)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        except BaseException:
            if created:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)
                    os.fsync(parent)
            raise

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            count = os.write(descriptor, view)
            view = view[count:]

    def _read(self, parent: int, name: str) -> bytes:
        descriptor = self._open_trusted_file(parent, name, _STATE_MODE, _MAX_STATE_BYTES)
        try:
            return os.read(descriptor, _MAX_STATE_BYTES + 1)
        finally:
            os.close(descriptor)

    def _exists(self, parent: int, name: str) -> bool:
        mode = _STATE_MODE if name == "state.json" else _RELEASE_MODE
        try:
            descriptor = self._open_trusted_file(parent, name, mode, _MAX_STATE_BYTES)
        except FileNotFoundError:
            return False
        else:
            os.close(descriptor)
            return True

    def _open_trusted_file(self, parent: int, name: str, mode: int, maximum: int) -> int:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise StateConflict(f"slot {self.slot} {name} is not a safe fixed file") from exc
        metadata = os.fstat(descriptor)
        account = pwd.getpwnam(f"kdive-worker-{self.slot}")
        trusted = (
            stat.S_ISREG(metadata.st_mode)
            and stat.S_IMODE(metadata.st_mode) == mode
            and metadata.st_uid == 0
            and metadata.st_gid == account.pw_gid
            and metadata.st_nlink == 1
            and metadata.st_size <= maximum
        )
        if trusted:
            return descriptor
        os.close(descriptor)
        raise StateConflict(f"slot {self.slot} {name} has untrusted metadata")

    def _environment(self, settings: WorkerSettings, state: SlotState) -> str:
        values = {
            "AWS_ACCESS_KEY_ID": settings.aws_access_key_id.get_secret_value(),
            "AWS_SECRET_ACCESS_KEY": settings.aws_secret_access_key.get_secret_value(),
            "KDIVE_BUILD_COMPONENT_ROOTS": settings.build_component_roots,
            "KDIVE_BUILD_USER": settings.build_user,
            "KDIVE_BUILD_WORKSPACE": settings.build_workspace,
            "KDIVE_DATABASE_URL": settings.worker_database_url.get_secret_value(),
            "KDIVE_FIXTURE_CATALOG_PATH": settings.fixture_catalog_path,
            "KDIVE_INSTALL_STAGING": settings.install_staging,
            "KDIVE_KERNEL_SRC": settings.source_root,
            "KDIVE_LIBVIRT_URI": settings.libvirt_uri,
            "KDIVE_LOG_LEVEL": settings.log_level,
            "KDIVE_ROOTFS_DIR": settings.rootfs_dir,
            "KDIVE_S3_BUCKET": settings.s3_bucket,
            "KDIVE_S3_ENDPOINT_URL": settings.s3_endpoint_url,
            "KDIVE_S3_REGION": settings.s3_region,
            "KDIVE_WORKER_ACCEPTED_LANES": ",".join(settings.accepted_lanes),
            "KDIVE_WORKER_INCARNATION_ID": state.incarnation,
            "KDIVE_WORKER_INCARNATION_KIND": "local",
            "KDIVE_WORKER_PYTHON": settings.python,
            "KDIVE_WORKER_SOURCE_ROOT": settings.source_root,
        }
        if health_bind := settings.health_binds.get(self.slot):
            values["KDIVE_HEALTH_BIND_ADDR"] = health_bind
        if any("\n" in value or "\x00" in value for value in values.values()):
            raise ValueError("worker environment values cannot contain newlines or NUL bytes")
        return "".join(f"{name}={shlex.quote(value)}\n" for name, value in sorted(values.items()))

    def _validate_state_slot(self, state: SlotState) -> None:
        if state.slot != self.slot or state.unit != self.unit:
            raise StateConflict("state does not belong to this fixed slot")

    def _validated_state(self, state: SlotState) -> SlotState:
        try:
            validated = SlotState.model_validate(state.model_dump())
        except ValueError as exc:
            raise StateConflict("state violates the durable slot contract") from exc
        self._validate_state_slot(validated)
        return validated

    @staticmethod
    def _validate_transition(retained: SlotState | None, candidate: SlotState) -> None:
        if retained is None:
            if candidate.phase is not SlotPhase.PREPARED:
                raise StateConflict("first persisted state must be prepared")
            return
        immutable = ("schema", "slot", "unit", "generation", "incarnation", "credential_hash")
        if any(getattr(retained, field) != getattr(candidate, field) for field in immutable):
            raise StateConflict("persist cannot change immutable incarnation facts")
        if retained == candidate:
            return
        transitions = {
            (SlotPhase.PREPARED, SlotPhase.GATED),
            (SlotPhase.GATED, SlotPhase.REGISTERED),
            (SlotPhase.REGISTERED, SlotPhase.STARTED),
            (SlotPhase.REGISTERED, SlotPhase.TERMINATED),
            (SlotPhase.STARTED, SlotPhase.TERMINATED),
        }
        if (retained.phase, candidate.phase) not in transitions:
            raise StateConflict("persist requires a legal forward phase transition")
        if retained.boot_id is not None and candidate.boot_id != retained.boot_id:
            raise StateConflict("persist cannot change the retained boot identifier")
        if retained.invocation_id is not None and candidate.invocation_id != retained.invocation_id:
            raise StateConflict("persist cannot change the retained invocation identifier")

    @staticmethod
    def _require_root() -> None:
        if os.geteuid() != 0:
            raise PermissionError("only root may mutate systemd worker slot state")
