"""Authority-owned initial System operations on one fixed local-libvirt host.

This adapter deliberately has no environment assembly path.  The authority process supplies a
private libvirt connection, staged-base resolver, and directories at construction; a worker never
gets to select any of them through an authority request.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

from kdive.providers.local_libvirt.lifecycle.rootfs.baseline_kernel import BaselineKernel
from kdive.providers.local_libvirt.lifecycle.rootfs.overlay_customize import (
    authorized_key_customizer,
)
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from kdive.providers.system_authority import (
    AuthoritySystemAbsenceFacts,
    AuthoritySystemCommitContextV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
    AuthoritySystemProvisionFacts,
    AuthoritySystemProvisionSnapshot,
)

_INTENT_SCHEMA = "local-authority-system-intent-v1"
_INTENT_PREFIX = b"kdive-local-authority-system-intent-v1\0"
_MAX_INTENT_BYTES = 1_048_576
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700
_OFFLOAD_CAPACITY = 4


class LocalAuthoritySystemError(RuntimeError):
    """A private-host ownership or durability check failed."""


class _Provisioner(Protocol):
    def provision(self, system_id: UUID, profile: Any, **kwargs: Any) -> str: ...


class _SystemTeardown(Protocol):
    def inspect(self) -> Any: ...

    def destroy(self) -> None: ...

    def undefine(self) -> None: ...

    def remove_overlay(self) -> None: ...

    def remove_baseline(self) -> None: ...

    def close(self) -> None: ...


type OpenTeardown = Callable[[UUID, str, str], _SystemTeardown]
type ReadinessProbe = Callable[[UUID], bool]
type PortAllocator = Callable[[], int]


@dataclass(frozen=True, slots=True)
class LocalAuthoritySystemTopology:
    """Fixed owner-only local artifact layout and staged bases for one authority instance."""

    intent_root: Path
    overlay_root: Path
    baseline_root: Path
    staged_bases: Mapping[str, Path]
    guest_egress: bool = False
    accel: str = "kvm"
    emulator: str | None = None

    def base_for(self, root_identity: str) -> Path:
        try:
            base = self.staged_bases[root_identity]
        except KeyError as error:
            raise LocalAuthoritySystemError(
                "root image is not staged for this authority"
            ) from error
        if not base.is_absolute():
            raise LocalAuthoritySystemError("authority staged base is not an absolute path")
        return base

    def overlay_for(self, system_id: UUID) -> Path:
        return self.overlay_root / f"{system_id}.qcow2"

    def baseline_for(self, system_id: UUID) -> Path:
        return self.baseline_root / f"{system_id}-baseline"


@dataclass(frozen=True, slots=True)
class _Intent:
    """Private durable identity required to resume a local System mutation safely."""

    system_id: UUID
    allocation_id: UUID
    resource_id: UUID
    authority_instance: str
    root_identity: str
    bootstrap_identity: str
    operation_digest: str
    deadline: datetime
    domain_name: str
    overlay: str
    baseline: str
    base: str
    gdb_port: int | None
    ssh_port: int
    xml_digest: str | None

    @property
    def identity(self) -> str:
        encoded = _canonical_intent_bytes(self)
        return "sha256:" + hashlib.sha256(_INTENT_PREFIX + encoded).hexdigest()

    def as_json(self) -> dict[str, object]:
        return {
            "schema": _INTENT_SCHEMA,
            "system_id": str(self.system_id),
            "allocation_id": str(self.allocation_id),
            "resource_id": str(self.resource_id),
            "authority_instance": self.authority_instance,
            "root_identity": self.root_identity,
            "bootstrap_identity": self.bootstrap_identity,
            "operation_digest": self.operation_digest,
            "deadline": self.deadline.isoformat(),
            "domain_name": self.domain_name,
            "overlay": self.overlay,
            "baseline": self.baseline,
            "base": self.base,
            "gdb_port": self.gdb_port,
            "ssh_port": self.ssh_port,
            "xml_digest": self.xml_digest,
        }

    @classmethod
    def from_json(cls, value: object) -> _Intent:
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise LocalAuthoritySystemError("local authority intent has an invalid shape")
        checked = cast("dict[str, object]", value)
        if set(checked) != {
            "schema",
            "system_id",
            "allocation_id",
            "resource_id",
            "authority_instance",
            "root_identity",
            "bootstrap_identity",
            "operation_digest",
            "deadline",
            "domain_name",
            "overlay",
            "baseline",
            "base",
            "gdb_port",
            "ssh_port",
            "xml_digest",
        }:
            raise LocalAuthoritySystemError("local authority intent has an invalid shape")
        if checked["schema"] != _INTENT_SCHEMA:
            raise LocalAuthoritySystemError("local authority intent has an unknown schema")
        try:
            deadline = datetime.fromisoformat(_require_str(checked, "deadline"))
            intent = cls(
                system_id=UUID(_require_str(checked, "system_id")),
                allocation_id=UUID(_require_str(checked, "allocation_id")),
                resource_id=UUID(_require_str(checked, "resource_id")),
                authority_instance=_require_str(checked, "authority_instance"),
                root_identity=_require_digest(checked, "root_identity"),
                bootstrap_identity=_require_digest(checked, "bootstrap_identity"),
                operation_digest=_require_digest(checked, "operation_digest"),
                deadline=deadline,
                domain_name=_require_str(checked, "domain_name"),
                overlay=_require_absolute_path(checked, "overlay"),
                baseline=_require_absolute_path(checked, "baseline"),
                base=_require_absolute_path(checked, "base"),
                gdb_port=_require_port_or_none(checked, "gdb_port"),
                ssh_port=_require_port(checked, "ssh_port"),
                xml_digest=_require_digest_or_none(checked, "xml_digest"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise LocalAuthoritySystemError("local authority intent has invalid values") from error
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise LocalAuthoritySystemError("local authority intent deadline is not UTC-aware")
        if deadline.utcoffset() != timedelta(0):
            raise LocalAuthoritySystemError("local authority intent deadline is not UTC")
        return intent


class LocalAuthoritySystemProvider:
    """Concrete authority provider for initial local-libvirt System lifecycle operations.

    ``provisioner`` is constructed by authority assembly from fixed primitives only.  In
    particular it must resolve rootfs materialization through ``topology.staged_bases`` and use
    the matching private overlay/baseline directories; this adapter validates the resulting
    identities before it asks that provisioner to mutate anything.
    """

    def __init__(
        self,
        *,
        provisioner: _Provisioner,
        topology: LocalAuthoritySystemTopology,
        readiness_probe: ReadinessProbe,
        open_teardown: OpenTeardown,
        allocate_port: PortAllocator,
        owner_uid: int | None = None,
        owner_gid: int | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        deadline: timedelta = timedelta(minutes=15),
    ) -> None:
        if deadline <= timedelta(0):
            raise ValueError("authority provision deadline must be positive")
        self._provisioner = provisioner
        self._topology = topology
        self._readiness_probe = readiness_probe
        self._open_teardown = open_teardown
        self._allocate_port = allocate_port
        self._owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self._owner_gid = os.getegid() if owner_gid is None else owner_gid
        self._now = now
        self._deadline = deadline
        self._executor = ThreadPoolExecutor(
            max_workers=_OFFLOAD_CAPACITY, thread_name_prefix="kdive-local-system-authority"
        )
        self._admission = threading.BoundedSemaphore(_OFFLOAD_CAPACITY)
        self._lock = threading.Lock()
        self._closed = False

    async def execute_system_provision(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> AuthoritySystemProvisionFacts:
        request, context, snapshot = _validated_provision_inputs(request, context, snapshot)
        return await self._offload(
            lambda: self._execute_system_provision(request, context, snapshot)
        )

    async def observe_system_provision(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> AuthoritySystemProvisionFacts:
        request, context, snapshot = _validated_provision_inputs(request, context, snapshot)
        return await self._offload(
            lambda: self._observe_system_provision(request, context, snapshot)
        )

    async def execute_preactivation_teardown(
        self, request: AuthoritySystemMutationRequestV1, context: AuthoritySystemCommitContextV1
    ) -> AuthoritySystemAbsenceFacts:
        request, context = _validated_teardown_inputs(request, context)
        return await self._offload(lambda: self._execute_teardown(request, context))

    async def observe_preactivation_teardown(
        self, request: AuthoritySystemMutationRequestV1, context: AuthoritySystemCommitContextV1
    ) -> AuthoritySystemAbsenceFacts:
        request, context = _validated_teardown_inputs(request, context)
        return await self._offload(lambda: self._observe_teardown(request, context))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)

    async def _offload[T](self, operation: Callable[[], T]) -> T:
        with self._lock:
            if self._closed or not self._admission.acquire(blocking=False):
                raise LocalAuthoritySystemError("local authority provider capacity is unavailable")
            try:
                future = self._executor.submit(operation)
            except RuntimeError as error:
                self._admission.release()
                raise LocalAuthoritySystemError(
                    "local authority provider capacity is unavailable"
                ) from error
        future.add_done_callback(lambda _future: self._admission.release())
        return await _await_completion(future)

    def _execute_system_provision(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> AuthoritySystemProvisionFacts:
        intent = self._load_intent(request.system_id)
        if intent is None:
            intent = self._candidate_intent(request, snapshot)
            # A retained baseline performs no extraction callback.  Persist before the overlay can
            # be touched on that retry path; otherwise the local provisioner's callback runs after
            # the guestfs read-only selection and before it resets any extraction staging.
            if Path(intent.baseline).is_dir():
                self._store_intent(self._intent_with_xml(intent, snapshot, None))
        self._require_matching_intent(intent, request, snapshot)
        if intent.deadline <= self._utc_now():
            raise LocalAuthoritySystemError("local authority provision deadline expired")
        self._provisioner.provision(
            request.system_id,
            snapshot.profile,
            bootstrap_pubkey=snapshot.bootstrap_public_key,
            overlay_customizers=(authorized_key_customizer(snapshot.bootstrap_public_key),),
            selected_gdb_port=intent.gdb_port,
            selected_ssh_port=intent.ssh_port,
            before_extract_baseline=lambda baseline: self._store_intent(
                self._intent_with_xml(intent, snapshot, baseline)
            ),
        )
        persisted = self._load_intent(request.system_id)
        if persisted is None:
            raise LocalAuthoritySystemError(
                "local authority provision failed to persist its intent"
            )
        intent = persisted
        return self._provision_facts(intent, ready=self._readiness_probe(request.system_id))

    def _observe_system_provision(
        self,
        request: AuthoritySystemMutationRequestV1,
        _context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> AuthoritySystemProvisionFacts:
        intent = self._load_intent(request.system_id)
        if intent is None:
            return AuthoritySystemProvisionFacts(
                intent_identity=request.operation_digest,
                domain_owned=False,
                root_storage_owned=False,
                boot_ready=False,
                bootstrap_ready=False,
                quarantine_retained=True,
                completed_at=None,
            )
        self._require_matching_intent(intent, request, snapshot)
        return self._provision_facts(intent, ready=self._readiness_probe(request.system_id))

    def _execute_teardown(
        self, request: AuthoritySystemMutationRequestV1, _context: AuthoritySystemCommitContextV1
    ) -> AuthoritySystemAbsenceFacts:
        intent = self._load_intent(request.system_id)
        if intent is None:
            inspection = self._inspect_without_intent(request.system_id)
            return self._absence_facts(
                request,
                domain_absent=inspection.domain_absent,
                storage_absent=inspection.overlay_absent and inspection.baseline_absent,
                intent_absent=True,
            )
        self._require_teardown_intent(intent, request)
        session = self._open_teardown(request.system_id, intent.overlay, intent.baseline)
        try:
            session.destroy()
            session.undefine()
            session.remove_overlay()
            session.remove_baseline()
            inspection = session.inspect()
            if not (
                inspection.domain_absent
                and inspection.overlay_absent
                and inspection.baseline_absent
            ):
                raise LocalAuthoritySystemError("local authority teardown did not reach absence")
        finally:
            session.close()
        self._remove_intent(intent)
        return self._absence_facts(
            request, domain_absent=True, storage_absent=True, intent_absent=True
        )

    def _observe_teardown(
        self, request: AuthoritySystemMutationRequestV1, _context: AuthoritySystemCommitContextV1
    ) -> AuthoritySystemAbsenceFacts:
        intent = self._load_intent(request.system_id)
        if intent is None:
            inspection = self._inspect_without_intent(request.system_id)
            return self._absence_facts(
                request,
                domain_absent=inspection.domain_absent,
                storage_absent=inspection.overlay_absent and inspection.baseline_absent,
                intent_absent=True,
            )
        self._require_teardown_intent(intent, request)
        session = self._open_teardown(request.system_id, intent.overlay, intent.baseline)
        try:
            inspection = session.inspect()
        finally:
            session.close()
        absent = (
            inspection.domain_absent and inspection.overlay_absent and inspection.baseline_absent
        )
        return self._absence_facts(
            request,
            domain_absent=inspection.domain_absent,
            storage_absent=inspection.overlay_absent and inspection.baseline_absent,
            intent_absent=False,
            retained=not absent,
        )

    def _inspect_without_intent(self, system_id: UUID) -> Any:
        session = self._open_teardown(
            system_id,
            str(self._topology.overlay_for(system_id)),
            str(self._topology.baseline_for(system_id)),
        )
        try:
            return session.inspect()
        finally:
            session.close()

    def _candidate_intent(
        self, request: AuthoritySystemMutationRequestV1, snapshot: AuthoritySystemProvisionSnapshot
    ) -> _Intent:
        base = self._topology.base_for(snapshot.root_identity)
        gdb_port = (
            self._allocate_port() if snapshot.profile.provider.local_libvirt.debug.gdbstub else None
        )
        return _Intent(
            system_id=request.system_id,
            allocation_id=request.allocation_id,
            resource_id=request.resource_id,
            authority_instance=request.authority_instance,
            root_identity=snapshot.root_identity,
            bootstrap_identity=snapshot.bootstrap_identity,
            operation_digest=request.operation_digest,
            deadline=self._utc_now() + self._deadline,
            domain_name=f"kdive-{request.system_id}",
            overlay=str(self._topology.overlay_for(request.system_id)),
            baseline=str(self._topology.baseline_for(request.system_id)),
            base=str(base),
            gdb_port=gdb_port,
            ssh_port=self._allocate_port(),
            xml_digest=None,
        )

    def _intent_with_xml(
        self,
        intent: _Intent,
        snapshot: AuthoritySystemProvisionSnapshot,
        baseline: BaselineKernel | None,
    ) -> _Intent:
        baseline_root = Path(intent.baseline)
        if baseline is None:
            kernel = baseline_root / "kernel"
            possible_initrd = baseline_root / "initrd"
            initrd = possible_initrd if possible_initrd.is_file() else None
        else:
            kernel = baseline.kernel
            initrd = baseline.initrd
        xml = render_domain_xml(
            intent.system_id,
            snapshot.profile,
            disk_path=intent.overlay,
            gdb_port=intent.gdb_port,
            ssh_port=intent.ssh_port,
            kernel_path=kernel,
            initrd_path=initrd,
            guest_egress=self._topology.guest_egress,
            accel=self._topology.accel,
            emulator=self._topology.emulator,
        )
        return replace(
            intent,
            xml_digest="sha256:" + hashlib.sha256(xml.encode("utf-8")).hexdigest(),
        )

    def _provision_facts(self, intent: _Intent, *, ready: bool) -> AuthoritySystemProvisionFacts:
        domain_owned = False
        try:
            teardown = self._open_teardown(intent.system_id, intent.overlay, intent.baseline)
            try:
                inspected = teardown.inspect()
                domain_owned = inspected.domain_validated
            finally:
                teardown.close()
        except Exception:
            domain_owned = False
        root_owned = Path(intent.overlay).is_file() and Path(intent.baseline).is_dir()
        complete = domain_owned and root_owned and ready
        return AuthoritySystemProvisionFacts(
            intent_identity=intent.identity,
            domain_owned=domain_owned,
            root_storage_owned=root_owned,
            boot_ready=ready if domain_owned else False,
            bootstrap_ready=complete,
            quarantine_retained=not complete,
            completed_at=self._utc_now() if complete else None,
        )

    @staticmethod
    def _absence_facts(
        request: AuthoritySystemMutationRequestV1,
        *,
        domain_absent: bool,
        storage_absent: bool,
        intent_absent: bool,
        retained: bool = False,
    ) -> AuthoritySystemAbsenceFacts:
        complete = domain_absent and storage_absent and intent_absent
        return AuthoritySystemAbsenceFacts(
            intent_identity=request.operation_digest,
            domain_absent=domain_absent,
            root_storage_absent=storage_absent,
            baseline_absent=storage_absent,
            private_intent_absent=intent_absent,
            quarantine_retained=retained or not complete,
            completed_at=datetime.now(UTC) if complete else None,
        )

    def _intent_path(self, system_id: UUID) -> Path:
        return self._topology.intent_root / f"{system_id}.json"

    def _load_intent(self, system_id: UUID) -> _Intent | None:
        root = self._open_private_root(create=False)
        if root is None:
            return None
        try:
            try:
                descriptor = os.open(
                    self._intent_path(system_id).name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=root,
                )
            except FileNotFoundError:
                return None
            except OSError as error:
                raise LocalAuthoritySystemError("local authority intent file is unsafe") from error
            try:
                info = os.fstat(descriptor)
                _require_private_file(info, self._owner_uid, self._owner_gid)
                payload = _read_bounded(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.close(root)
        try:
            return _Intent.from_json(json.loads(payload))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LocalAuthoritySystemError("local authority intent is unreadable") from error

    def _store_intent(self, intent: _Intent) -> None:
        root = self._open_private_root(create=True)
        assert root is not None
        name = self._intent_path(intent.system_id).name
        payload = _canonical_intent_bytes(intent)
        try:
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    _PRIVATE_FILE_MODE,
                    dir_fd=root,
                )
            except FileExistsError:
                existing = self._load_intent(intent.system_id)
                if existing != intent:
                    raise LocalAuthoritySystemError("local authority intent was replaced") from None
                return
            try:
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(root)
        finally:
            os.close(root)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise LocalAuthoritySystemError("authority clock must return an aware UTC time")
        return value

    def _remove_intent(self, intent: _Intent) -> None:
        root = self._open_private_root(create=False)
        if root is None:
            raise LocalAuthoritySystemError("local authority intent disappeared during teardown")
        try:
            current = self._load_intent(intent.system_id)
            if current != intent:
                raise LocalAuthoritySystemError("local authority intent was replaced")
            os.unlink(self._intent_path(intent.system_id).name, dir_fd=root)
            os.fsync(root)
        finally:
            os.close(root)

    def _open_private_root(self, *, create: bool) -> int | None:
        root = self._topology.intent_root
        if create:
            root.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            _require_private_directory(os.fstat(descriptor), self._owner_uid, self._owner_gid)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _require_matching_intent(
        intent: _Intent,
        request: AuthoritySystemMutationRequestV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> None:
        if (
            intent.system_id != request.system_id
            or intent.allocation_id != request.allocation_id
            or intent.resource_id != request.resource_id
            or intent.authority_instance != request.authority_instance
            or intent.root_identity != snapshot.root_identity
            or intent.bootstrap_identity != snapshot.bootstrap_identity
            or intent.operation_digest != request.operation_digest
        ):
            raise LocalAuthoritySystemError("local authority intent does not match the request")

    @staticmethod
    def _require_teardown_intent(
        intent: _Intent, request: AuthoritySystemMutationRequestV1
    ) -> None:
        if (
            intent.system_id != request.system_id
            or intent.allocation_id != request.allocation_id
            or intent.resource_id != request.resource_id
            or intent.authority_instance != request.authority_instance
        ):
            raise LocalAuthoritySystemError("local authority intent does not match the request")


async def _await_completion[T](future: Future[T]) -> T:
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    future.add_done_callback(lambda _future: loop.call_soon_threadsafe(completed.set_result, None))
    try:
        await asyncio.shield(completed)
    except asyncio.CancelledError as cancelled:
        while not completed.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(completed)
        if not future.cancelled():
            future.exception()
        raise cancelled from None
    return future.result()


def _validated_provision_inputs(
    request: AuthoritySystemMutationRequestV1,
    context: AuthoritySystemCommitContextV1,
    snapshot: AuthoritySystemProvisionSnapshot,
) -> tuple[
    AuthoritySystemMutationRequestV1,
    AuthoritySystemCommitContextV1,
    AuthoritySystemProvisionSnapshot,
]:
    try:
        checked_request = AuthoritySystemMutationRequestV1.model_validate(
            request.model_dump(by_alias=True)
        )
        checked_context = AuthoritySystemCommitContextV1.model_validate(
            context.model_dump(by_alias=True)
        )
        checked_snapshot = AuthoritySystemProvisionSnapshot(
            system_id=snapshot.system_id,
            allocation_id=snapshot.allocation_id,
            resource_id=snapshot.resource_id,
            project=snapshot.project,
            provider_kind=snapshot.provider_kind,
            resource_name=snapshot.resource_name,
            authority_instance=snapshot.authority_instance,
            profile=snapshot.profile,
            profile_identity=snapshot.profile_identity,
            source_image_id=snapshot.source_image_id,
            root_identity=snapshot.root_identity,
            root_spec=snapshot.root_spec,
            bootstrap_public_key=snapshot.bootstrap_public_key,
            bootstrap_identity=snapshot.bootstrap_identity,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise LocalAuthoritySystemError("authority provision values are invalid") from error
    if (
        checked_request.operation is not AuthoritySystemOperation.PROVISION
        or checked_context.operation is not AuthoritySystemOperation.PROVISION
        or checked_context.attempt_id != checked_request.attempt_id
        or checked_snapshot.system_id != checked_request.system_id
        or checked_snapshot.allocation_id != checked_request.allocation_id
        or checked_snapshot.resource_id != checked_request.resource_id
        or checked_snapshot.provider_kind != checked_request.provider_kind
        or checked_snapshot.resource_name != checked_request.resource_name
        or checked_snapshot.authority_instance != checked_request.authority_instance
        or checked_snapshot.profile_identity != checked_request.profile_identity
        or checked_snapshot.root_identity != checked_request.root_identity
        or checked_snapshot.bootstrap_identity != checked_request.bootstrap_identity
    ):
        raise LocalAuthoritySystemError("authority provision operation does not match its context")
    return checked_request, checked_context, checked_snapshot


def _validated_teardown_inputs(
    request: AuthoritySystemMutationRequestV1, context: AuthoritySystemCommitContextV1
) -> tuple[AuthoritySystemMutationRequestV1, AuthoritySystemCommitContextV1]:
    try:
        checked_request = AuthoritySystemMutationRequestV1.model_validate(
            request.model_dump(by_alias=True)
        )
        checked_context = AuthoritySystemCommitContextV1.model_validate(
            context.model_dump(by_alias=True)
        )
    except (AttributeError, ValueError) as error:
        raise LocalAuthoritySystemError("authority teardown values are invalid") from error
    if (
        checked_request.operation is not AuthoritySystemOperation.PREACTIVATION_TEARDOWN
        or checked_context.operation is not AuthoritySystemOperation.PREACTIVATION_TEARDOWN
        or checked_context.attempt_id != checked_request.attempt_id
    ):
        raise LocalAuthoritySystemError("authority teardown operation does not match its context")
    return checked_request, checked_context


def _canonical_intent_bytes(intent: _Intent) -> bytes:
    payload = json.dumps(intent.as_json(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_INTENT_BYTES:
        raise LocalAuthoritySystemError("local authority intent exceeds its byte bound")
    return payload


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    remaining = _MAX_INTENT_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > _MAX_INTENT_BYTES:
        raise LocalAuthoritySystemError("local authority intent exceeds its byte bound")
    return payload


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise LocalAuthoritySystemError("failed to write the full local authority intent")
        view = view[written:]


def _require_private_file(info: os.stat_result, uid: int, gid: int) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_gid != gid:
        raise LocalAuthoritySystemError("local authority intent file has unsafe ownership")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_FILE_MODE or info.st_nlink != 1:
        raise LocalAuthoritySystemError("local authority intent file has unsafe mode")


def _require_private_directory(info: os.stat_result, uid: int, gid: int) -> None:
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_gid != gid:
        raise LocalAuthoritySystemError("local authority intent directory has unsafe ownership")
    if stat.S_IMODE(info.st_mode) != _PRIVATE_DIR_MODE:
        raise LocalAuthoritySystemError("local authority intent directory has unsafe mode")


def _require_str(value: dict[str, object], name: str) -> str:
    result = value[name]
    if not isinstance(result, str) or not result:
        raise ValueError(name)
    return result


def _require_digest(value: dict[str, object], name: str) -> str:
    result = _require_str(value, name)
    if not result.startswith("sha256:") or len(result) != 71:
        raise ValueError(name)
    return result


def _require_digest_or_none(value: dict[str, object], name: str) -> str | None:
    if value[name] is None:
        return None
    return _require_digest(value, name)


def _require_absolute_path(value: dict[str, object], name: str) -> str:
    result = _require_str(value, name)
    if not Path(result).is_absolute():
        raise ValueError(name)
    return result


def _require_port(value: dict[str, object], name: str) -> int:
    result = value[name]
    if type(result) is not int or not 1 <= result <= 65535:
        raise ValueError(name)
    return result


def _require_port_or_none(value: dict[str, object], name: str) -> int | None:
    if value[name] is None:
        return None
    return _require_port(value, name)
