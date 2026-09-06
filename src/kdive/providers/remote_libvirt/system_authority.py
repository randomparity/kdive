"""Fixed-host authority-owned remote System provisioning (ADR-0623)."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, Self, cast
from uuid import UUID, uuid4

import libvirt
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.external_boot_activation import UtcDateTime
from kdive.profiles.provisioning import require_concrete_sizing
from kdive.providers.remote_libvirt.guest.bootstrap_key import RemoteBootstrapKeyInjector
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    parse_domain_xml,
    preserved_definition_identity,
    require_disk_grub_source,
)
from kdive.providers.remote_libvirt.lifecycle.port_allocation import (
    allocate_port,
    used_gdb_ports,
    used_ssh_ports,
)
from kdive.providers.remote_libvirt.lifecycle.readiness import wait_for_agent
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    HostStatDeviceIdentity,
    RemoteDeviceIdentityPort,
    prove_no_foreign_storage_references,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.storage import ensure_named_overlay
from kdive.providers.remote_libvirt.lifecycle.xml import (
    QEMU_NS,
    agent_channel_connected_strict,
    overlay_volume_name,
    render_domain_xml,
    supplied_base_volume_name,
    volume_backing_path,
)
from kdive.providers.shared.libvirt_xml import (
    remote_metadata_storage_identity,
    remote_metadata_system_id,
)
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.providers.system_authority import (
    AuthoritySystemAbsenceFacts,
    AuthoritySystemCommitContextV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
    AuthoritySystemProvisionFacts,
    AuthoritySystemProvisionSnapshot,
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}\Z")
_STATE_NAME = re.compile(
    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    r"\.(?:provision|teardown|absent)\.json\Z"
)
_MAX_STATE_ENTRIES = 4096
_MAX_STATE_BYTES = 1_048_576
_MAX_DOMAIN_DOCUMENTS = 4096
_MAX_STORAGE_PATH_BYTES = 4096
_PHASES = {
    "planned": 0,
    "overlay-ready": 1,
    "domain-defined": 2,
    "boot-ready": 3,
    "bootstrap-ready": 4,
    "complete": 5,
}
type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


def _bounded_name(value: str) -> str:
    if _NAME.fullmatch(value) is None:
        raise ValueError("authority host identifier must be 1 through 255 safe bytes")
    return value


def _bounded_binding(value: str) -> str:
    if not value or not value.strip() or len(value.encode()) > 255:
        raise ValueError("authority binding must contain 1 through 255 nonblank UTF-8 bytes")
    return value


def _bounded_address(value: str) -> str:
    if (
        not value
        or len(value.encode()) > 255
        or not value.isascii()
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        raise ValueError("authority host address must be 1 through 255 nonblank ASCII bytes")
    return value


def _concrete_machine(architecture: str, machine: str) -> str:
    prefixes = {
        "x86_64": ("pc-i440fx-", "pc-q35-"),
        "ppc64le": ("pseries-",),
    }
    accepted = prefixes.get(architecture)
    if accepted is None or not machine.startswith(accepted):
        raise ValueError("authority host machine must be a concrete architecture-matched type")
    return machine


@dataclass(frozen=True, slots=True)
class RemoteAuthoritySystemManifestEntry:
    """One owner-supplied root-digest to private remote-host topology binding."""

    resource_name: str
    authority_instance: str
    root_identity: str
    architecture: Literal["x86_64", "ppc64le"]
    base_volume: str
    network: str
    machine: str
    gdb_addr: str
    gdb_port_min: int
    gdb_port_max: int
    ssh_addr: str
    ssh_port_min: int
    ssh_port_max: int

    def __post_init__(self) -> None:
        for value in (self.resource_name, self.authority_instance):
            _bounded_binding(value)
        for value in (self.base_volume, self.network, self.machine):
            _bounded_name(value)
        _concrete_machine(self.architecture, self.machine)
        if _DIGEST.fullmatch(self.root_identity) is None:
            raise ValueError("authority base volume identity must be a lowercase SHA-256 digest")
        _bounded_address(self.gdb_addr)
        _bounded_address(self.ssh_addr)
        for low, high, label in (
            (self.gdb_port_min, self.gdb_port_max, "gdb"),
            (self.ssh_port_min, self.ssh_port_max, "ssh"),
        ):
            if type(low) is not int or type(high) is not int or not 1 <= low <= high <= 65535:
                raise ValueError(f"authority {label} port range is invalid")
        if self.gdb_port_min == self.gdb_port_max:
            raise ValueError("authority gdb range must reserve one probe port and one guest port")
        if (
            self.gdb_addr == self.ssh_addr
            and self.gdb_port_min <= self.ssh_port_max
            and self.ssh_port_min <= self.gdb_port_max
        ):
            raise ValueError("authority gdb and ssh ranges overlap on one address")


class _ClosedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_by_alias=True)


class _AttemptBinding(_ClosedValue):
    authority_id: UUID
    generation: Annotated[int, Field(ge=1)]
    attempt_id: UUID
    operation_digest: Digest
    journal_sequence: Annotated[int, Field(ge=1)]
    journal_digest: Digest


class _ProvisionIntentV1(_ClosedValue):
    schema_: Literal["remote-authority-system-provision-intent-v1"] = Field(
        "remote-authority-system-provision-intent-v1", alias="schema"
    )
    system_id: UUID
    allocation_id: UUID
    resource_id: UUID
    resource_name: str
    authority_instance: str
    profile_identity: Digest
    root_identity: Digest
    bootstrap_identity: Digest
    operation_identity: str
    current_attempt: _AttemptBinding
    architecture: Literal["x86_64", "ppc64le"]
    pool: str
    base_volume: str
    base_path: str
    overlay_volume: str
    overlay_path: str
    network: str
    machine: str
    gdb_addr: str
    gdb_port: Annotated[int, Field(ge=1, le=65535)]
    ssh_addr: str
    ssh_port: Annotated[int, Field(ge=1, le=65535)]
    domain_xml: Annotated[str, Field(min_length=1, max_length=65_536)]
    domain_projection: Digest
    normalized_definition: Digest | None = None
    deadline: UtcDateTime
    phase: Literal[
        "planned",
        "overlay-ready",
        "domain-defined",
        "boot-ready",
        "bootstrap-ready",
        "complete",
    ]
    completed_at: UtcDateTime | None = None
    intent_identity: Digest

    @field_validator("resource_name", "authority_instance", "operation_identity")
    @classmethod
    def _bindings_are_bounded(cls, value: str) -> str:
        return _bounded_binding(value)

    @field_validator("pool", "base_volume", "network", "machine")
    @classmethod
    def _topology_names_are_safe(cls, value: str) -> str:
        return _bounded_name(value)

    @model_validator(mode="after")
    def _identity_and_phase_are_valid(self) -> Self:
        if self.domain_projection != _projection_identity(self.domain_xml):
            raise ValueError("private provision intent domain projection changed")
        if self.intent_identity != _intent_identity(self):
            raise ValueError("private provision intent identity changed")
        if (self.phase == "complete") != (self.completed_at is not None):
            raise ValueError("private provision intent completion shape is invalid")
        return self


class _TeardownIntentV1(_ClosedValue):
    schema_: Literal["remote-authority-system-teardown-intent-v1"] = Field(
        "remote-authority-system-teardown-intent-v1", alias="schema"
    )
    system_id: UUID
    allocation_id: UUID
    resource_id: UUID
    resource_name: str
    authority_instance: str
    profile_identity: Digest
    root_identity: Digest
    bootstrap_identity: Digest
    operation_identity: str
    current_attempt: _AttemptBinding
    provision_intent_identity: Digest
    deadline: UtcDateTime

    @model_validator(mode="after")
    def _binding_is_bounded(self) -> Self:
        for value in (self.resource_name, self.authority_instance, self.operation_identity):
            _bounded_binding(value)
        return self


class _AbsenceReceiptV1(_ClosedValue):
    schema_: Literal["remote-authority-system-absence-v1"] = Field(
        "remote-authority-system-absence-v1", alias="schema"
    )
    system_id: UUID
    allocation_id: UUID
    resource_id: UUID
    resource_name: str
    authority_instance: str
    profile_identity: Digest
    root_identity: Digest
    bootstrap_identity: Digest
    operation_identity: str
    provision_intent_identity: Digest
    completed_at: UtcDateTime
    receipt_identity: Digest

    @model_validator(mode="after")
    def _identity_is_valid(self) -> Self:
        for value in (self.resource_name, self.authority_instance, self.operation_identity):
            _bounded_binding(value)
        if self.receipt_identity != _absence_identity(self):
            raise ValueError("private remote System absence receipt identity changed")
        return self


class _Domain(Protocol):
    def name(self) -> str: ...
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def undefineFlags(self, flags: int = 0) -> int: ...  # noqa: N802
    def isActive(self) -> int: ...  # noqa: N802
    def isPersistent(self) -> int: ...  # noqa: N802
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802


class _Volume(Protocol):
    def name(self) -> str: ...
    def path(self) -> str: ...
    def info(self) -> list[int]: ...
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802
    def delete(self, flags: int = 0) -> int: ...


class _Pool(Protocol):
    def refresh(self, flags: int = 0) -> int: ...
    def storageVolLookupByName(self, name: str) -> _Volume: ...  # noqa: N802
    def createXML(self, xml: str, flags: int = 0) -> _Volume: ...  # noqa: N802


class _Connection(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...  # noqa: N802
    def listAllDomains(self, flags: int = 0) -> list[_Domain]: ...  # noqa: N802
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802
    def defineXML(self, xml: str) -> _Domain: ...  # noqa: N802


class _BootstrapInjector(Protocol):
    def inject(self, domain: Any, pubkey: str) -> None: ...


type ConnectionFactory = Callable[[], AbstractContextManager[_Connection]]


def _canonical(value: BaseModel) -> bytes:
    data = json.dumps(
        value.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    if len(data) > _MAX_STATE_BYTES:
        raise ValueError("private authority System record exceeds 1048576 bytes")
    return data + b"\n"


def _digest(prefix: bytes, value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(prefix + b"\0" + encoded).hexdigest()


def _intent_identity(intent: _ProvisionIntentV1) -> str:
    return _digest(
        b"kdive-remote-authority-system-intent-v1",
        {
            "system_id": str(intent.system_id),
            "allocation_id": str(intent.allocation_id),
            "resource_id": str(intent.resource_id),
            "resource_name": intent.resource_name,
            "authority_instance": intent.authority_instance,
            "profile_identity": intent.profile_identity,
            "root_identity": intent.root_identity,
            "bootstrap_identity": intent.bootstrap_identity,
            "operation_identity": intent.operation_identity,
            "architecture": intent.architecture,
            "pool": intent.pool,
            "base_volume": intent.base_volume,
            "base_path": intent.base_path,
            "overlay_volume": intent.overlay_volume,
            "overlay_path": intent.overlay_path,
            "network": intent.network,
            "machine": intent.machine,
            "gdb_addr": intent.gdb_addr,
            "gdb_port": intent.gdb_port,
            "ssh_addr": intent.ssh_addr,
            "ssh_port": intent.ssh_port,
            "domain_projection": intent.domain_projection,
            "deadline": intent.deadline.isoformat(),
        },
    )


def _absence_identity(receipt: _AbsenceReceiptV1) -> str:
    return _digest(
        b"kdive-remote-authority-system-absence-v1",
        {
            "system_id": str(receipt.system_id),
            "allocation_id": str(receipt.allocation_id),
            "resource_id": str(receipt.resource_id),
            "resource_name": receipt.resource_name,
            "authority_instance": receipt.authority_instance,
            "profile_identity": receipt.profile_identity,
            "root_identity": receipt.root_identity,
            "bootstrap_identity": receipt.bootstrap_identity,
            "operation_identity": receipt.operation_identity,
            "provision_intent_identity": receipt.provision_intent_identity,
            "completed_at": receipt.completed_at.isoformat(),
        },
    )


def _never_began_identity(request: AuthoritySystemMutationRequestV1) -> str:
    return _digest(
        b"kdive-remote-authority-system-never-began-v1",
        {
            "system_id": str(request.system_id),
            "allocation_id": str(request.allocation_id),
            "resource_id": str(request.resource_id),
            "resource_name": request.resource_name,
            "authority_instance": request.authority_instance,
            "profile_identity": request.profile_identity,
            "root_identity": request.root_identity,
            "bootstrap_identity": request.bootstrap_identity,
        },
    )


def _memory_bytes(element: ET.Element) -> int:
    raw = (element.text or "").strip()
    if not raw.isdigit():
        raise ValueError("remote System memory is invalid")
    multipliers = {None: 1024, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    try:
        multiplier = multipliers[element.get("unit")]
    except KeyError:
        raise ValueError("remote System memory unit is invalid") from None
    return int(raw) * multiplier


def _one(root: ET.Element, path: str, what: str) -> ET.Element:
    values = root.findall(path)
    if len(values) != 1:
        raise ValueError(f"remote System {what} is not singular")
    return values[0]


def _target_devices(root: ET.Element, tag: str) -> list[tuple[str | None, str | None, str | None]]:
    values = []
    for device in root.findall(f"./devices/{tag}"):
        target = device.find("./target")
        values.append(
            (
                device.get("type"),
                target.get("type") if target is not None else None,
                target.get("name")
                if tag == "channel" and target is not None
                else (target.get("port") if target is not None else None),
            )
        )
    return values


def _domain_projection(domain_xml: str) -> dict[str, object]:
    root = parse_domain_xml(domain_xml)
    os_element = _one(root, "./os", "operating system")
    os_type = _one(os_element, "./type", "machine type")
    memory = _one(root, "./memory", "memory")
    vcpu = _one(root, "./vcpu", "vCPU count")
    disk = _one(root, "./devices/disk", "root disk")
    source = _one(disk, "./source", "root disk source")
    driver = _one(disk, "./driver", "root disk driver")
    target = _one(disk, "./target", "root disk target")
    backing = _one(disk, "./backingStore", "root disk backing")
    backing_source = _one(backing, "./source", "root backing source")
    backing_format = _one(backing, "./format", "root backing format")
    interfaces = root.findall("./devices/interface")
    devices = _one(root, "./devices", "device collection")
    qemu_args = [
        argument.get("value")
        for argument in root.findall(f"./{{{QEMU_NS}}}commandline/{{{QEMU_NS}}}arg")
    ]
    networks = []
    for interface in interfaces:
        network_source = interface.find("./source")
        model = interface.find("./model")
        networks.append(
            (
                interface.get("type"),
                network_source.get("network") if network_source is not None else None,
                network_source.get("bridge") if network_source is not None else None,
                network_source.get("dev") if network_source is not None else None,
                model.get("type") if model is not None else None,
            )
        )
    storage = remote_metadata_storage_identity(root)
    return {
        "name": root.findtext("./name"),
        "uuid": root.findtext("./uuid"),
        "memory_bytes": _memory_bytes(memory),
        "vcpus": (vcpu.text or "").strip(),
        "cpu_mode": _one(root, "./cpu", "CPU").get("mode"),
        "architecture": os_type.get("arch"),
        "machine": os_type.get("machine"),
        "boots": [element.get("dev") for element in os_element.findall("./boot")],
        "kernel": os_element.findtext("./kernel"),
        "initrd": os_element.findtext("./initrd"),
        "cmdline": os_element.findtext("./cmdline"),
        "acpi": len(root.findall("./features/acpi")),
        "vmcoreinfo": [item.get("state") for item in root.findall("./features/vmcoreinfo")],
        "disk": {
            "type": disk.get("type"),
            "driver": driver.get("type"),
            "source": source.get("file"),
            "target": (target.get("dev"), target.get("bus")),
            "backing": (backing_source.get("file"), backing_format.get("type")),
        },
        "networks": networks,
        "channels": _target_devices(root, "channel"),
        "serials": _target_devices(root, "serial"),
        "consoles": _target_devices(root, "console"),
        "filesystems": len(root.findall("./devices/filesystem")),
        "hostdevs": len(root.findall("./devices/hostdev")),
        "unexpected_devices": sorted(
            child.tag
            for child in devices
            if child.tag
            not in {
                "audio",
                "channel",
                "console",
                "controller",
                "disk",
                "emulator",
                "input",
                "interface",
                "memballoon",
                "serial",
                "watchdog",
            }
        ),
        "system_id": remote_metadata_system_id(root),
        "storage": storage,
        "qemu_args": qemu_args,
    }


def _projection_identity(domain_xml: str) -> str:
    return _digest(b"kdive-remote-authority-system-domain-v1", _domain_projection(domain_xml))


class _PrivateStore:
    def __init__(self, root: Path, *, owner_uid: int, owner_gid: int) -> None:
        self._root = root
        self._owner_uid = owner_uid
        self._owner_gid = owner_gid
        self._lock = threading.Lock()

    @staticmethod
    def provision_name(system_id: UUID) -> str:
        return f"{system_id}.provision.json"

    @staticmethod
    def teardown_name(system_id: UUID) -> str:
        return f"{system_id}.teardown.json"

    @staticmethod
    def absent_name(system_id: UUID) -> str:
        return f"{system_id}.absent.json"

    def _open(self) -> int:
        try:
            descriptor = os.open(
                self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
        except OSError as exc:
            raise CategorizedError(
                "remote authority System state root is unavailable",
                category=ErrorCategory.CONFIGURATION_ERROR,
            ) from exc
        status = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o700
            or status.st_uid != self._owner_uid
            or status.st_gid != self._owner_gid
        ):
            os.close(descriptor)
            raise CategorizedError(
                "remote authority System state root has unsafe ownership or mode",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        try:
            self._scan(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _scan(self, descriptor: int) -> None:
        names = os.listdir(descriptor)
        if len(names) > _MAX_STATE_ENTRIES:
            raise ValueError("remote authority System state exceeds 4096 entries")
        for name in names:
            if _STATE_NAME.fullmatch(name) is None:
                raise ValueError("remote authority System state contains an unexpected entry")
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(status.st_mode)
                or stat.S_IMODE(status.st_mode) != 0o600
                or status.st_uid != self._owner_uid
                or status.st_gid != self._owner_gid
                or status.st_size > _MAX_STATE_BYTES + 1
            ):
                raise ValueError("remote authority System state entry is unsafe")

    def read[ValueT: BaseModel](self, name: str, model: type[ValueT]) -> ValueT | None:
        with self._lock:
            descriptor = self._open()
            try:
                try:
                    item = os.open(
                        name,
                        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    return None
                try:
                    chunks = bytearray()
                    while chunk := os.read(item, min(65_536, _MAX_STATE_BYTES + 2 - len(chunks))):
                        chunks.extend(chunk)
                        if len(chunks) > _MAX_STATE_BYTES + 1:
                            raise ValueError(
                                "private authority System record exceeds 1048576 bytes"
                            )
                finally:
                    os.close(item)
            finally:
                os.close(descriptor)
        data = bytes(chunks)
        if not data.endswith(b"\n") or data.endswith(b"\n\n"):
            raise ValueError("private authority System record is not newline framed")
        value = model.model_validate_json(data[:-1])
        if _canonical(value) != data:
            raise ValueError("private authority System record is not canonical JSON")
        return value

    def write(self, name: str, value: BaseModel) -> None:
        data = _canonical(value)
        temporary = f".{name}.{uuid4().hex}.tmp"
        with self._lock:
            descriptor = self._open()
            created = False
            try:
                item = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=descriptor,
                )
                created = True
                try:
                    view = memoryview(data)
                    while view:
                        count = os.write(item, view)
                        if count < 1:
                            raise OSError("short private authority System record write")
                        view = view[count:]
                    os.fsync(item)
                finally:
                    os.close(item)
                os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                created = False
                os.fsync(descriptor)
            finally:
                if created:
                    with suppress(FileNotFoundError):
                        os.unlink(temporary, dir_fd=descriptor)
                os.close(descriptor)

    def delete(self, name: str) -> None:
        with self._lock:
            descriptor = self._open()
            try:
                try:
                    os.unlink(name, dir_fd=descriptor)
                except FileNotFoundError:
                    return
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


class RemoteAuthoritySystemProvider:
    """Provision and remove remote Systems only through fixed authority-host resources."""

    def __init__(
        self,
        *,
        connection: ConnectionFactory,
        pool_name: str,
        manifest: tuple[RemoteAuthoritySystemManifestEntry, ...],
        state_dir: Path,
        executor: RemoteModulePreparationExecutor,
        identity_port: RemoteDeviceIdentityPort | None = None,
        bootstrap_injector: _BootstrapInjector | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        provision_timeout_s: float = 300.0,
        poll_interval_s: float = 2.0,
        owner_uid: int | None = None,
        owner_gid: int | None = None,
    ) -> None:
        self._connection = connection
        self._pool_name = _bounded_name(pool_name)
        self._manifest = self._index_manifest(manifest)
        self._store = _PrivateStore(
            state_dir,
            owner_uid=os.getuid() if owner_uid is None else owner_uid,
            owner_gid=os.getgid() if owner_gid is None else owner_gid,
        )
        self._executor = executor
        self._identity_port = identity_port or HostStatDeviceIdentity()
        self._bootstrap = bootstrap_injector or RemoteBootstrapKeyInjector()
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        if not 0 < provision_timeout_s <= 3600:
            raise ValueError("remote authority System timeout must be within 1 hour")
        if not 0 < poll_interval_s <= provision_timeout_s:
            raise ValueError("remote authority System poll interval is invalid")
        self._timeout = provision_timeout_s
        self._poll = poll_interval_s

    @staticmethod
    def _index_manifest(
        entries: tuple[RemoteAuthoritySystemManifestEntry, ...],
    ) -> dict[tuple[str, str, str], RemoteAuthoritySystemManifestEntry]:
        if not 0 < len(entries) <= _MAX_STATE_ENTRIES:
            raise ValueError("remote authority System manifest must have 1 through 4096 entries")
        indexed: dict[tuple[str, str, str], RemoteAuthoritySystemManifestEntry] = {}
        for entry in entries:
            key = (entry.resource_name, entry.authority_instance, entry.root_identity)
            if key in indexed:
                raise ValueError("remote authority System manifest has a duplicate binding")
            indexed[key] = entry
        return indexed

    def _entry(
        self, request: AuthoritySystemMutationRequestV1
    ) -> RemoteAuthoritySystemManifestEntry:
        key = (request.resource_name, request.authority_instance, request.root_identity)
        try:
            return self._manifest[key]
        except KeyError:
            raise CategorizedError(
                "remote authority System binding is not installed",
                category=ErrorCategory.CONFIGURATION_ERROR,
            ) from None

    @staticmethod
    def _validate_context(
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        operation: AuthoritySystemOperation,
    ) -> None:
        if (
            request.provider_kind != "remote-libvirt"
            or request.operation is not operation
            or context.operation is not operation
            or context.attempt_id != request.attempt_id
        ):
            raise ValueError("remote authority System request and context differ")

    @staticmethod
    def _validate_snapshot(
        request: AuthoritySystemMutationRequestV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> None:
        if (
            snapshot.system_id != request.system_id
            or snapshot.allocation_id != request.allocation_id
            or snapshot.resource_id != request.resource_id
            or snapshot.provider_kind != request.provider_kind
            or snapshot.resource_name != request.resource_name
            or snapshot.authority_instance != request.authority_instance
            or snapshot.profile_identity != request.profile_identity
            or snapshot.root_identity != request.root_identity
            or snapshot.bootstrap_identity != request.bootstrap_identity
        ):
            raise ValueError("remote authority System snapshot differs from request")
        require_concrete_sizing(snapshot.profile)
        section = snapshot.profile.provider.remote_libvirt_section
        if section is None:
            raise ValueError("remote authority System snapshot has no remote-libvirt profile")
        if section.base_image_source is not None:
            raise CategorizedError(
                "authority-owned remote Systems require an operator-staged base mapping",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )

    @staticmethod
    def _attempt(
        request: AuthoritySystemMutationRequestV1, context: AuthoritySystemCommitContextV1
    ) -> _AttemptBinding:
        return _AttemptBinding(
            authority_id=request.authority_id,
            generation=request.generation,
            attempt_id=request.attempt_id,
            operation_digest=request.operation_digest,
            journal_sequence=context.journal_sequence,
            journal_digest=context.journal_digest,
        )

    def _deadline(self) -> datetime:
        return self._utc_now() + timedelta(seconds=self._timeout)

    def _utc_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("remote authority System clock must return an aware timestamp")
        return now.astimezone(UTC)

    def _local_deadline(self, retained: datetime) -> float:
        remaining = (retained - self._utc_now()).total_seconds()
        if remaining <= 0:
            raise TimeoutError("remote authority System deadline expired")
        return self._monotonic() + remaining

    def _require_deadline(self, deadline: float) -> None:
        if self._monotonic() >= deadline:
            raise TimeoutError("remote authority System deadline expired")

    def _base(
        self, connection: _Connection, entry: RemoteAuthoritySystemManifestEntry
    ) -> tuple[_Pool, _Volume, str]:
        try:
            pool = connection.storagePoolLookupByName(self._pool_name)
        except libvirt.libvirtError as exc:
            category = (
                ErrorCategory.CONFIGURATION_ERROR
                if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL
                else ErrorCategory.INFRASTRUCTURE_FAILURE
            )
            raise CategorizedError(
                "remote authority System pool is unavailable",
                category=category,
            ) from exc
        try:
            pool.refresh(0)
            volume = pool.storageVolLookupByName(entry.base_volume)
        except libvirt.libvirtError as exc:
            category = (
                ErrorCategory.CONFIGURATION_ERROR
                if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL
                else ErrorCategory.INFRASTRUCTURE_FAILURE
            )
            raise CategorizedError(
                "remote authority System base volume is unavailable",
                category=category,
            ) from exc
        try:
            path = volume.path()
            xml = volume.XMLDesc(0)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote authority System base volume inspection failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        try:
            backing_path = volume_backing_path(xml)
        except ValueError as exc:
            raise CategorizedError(
                "remote authority System base volume XML is invalid",
                category=ErrorCategory.CONFIGURATION_ERROR,
            ) from exc
        if volume.name() != entry.base_volume or backing_path is not None:
            raise CategorizedError(
                "remote authority System base volume identity is invalid",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return pool, volume, _storage_path(path)

    @staticmethod
    def _domain_optional(connection: _Connection, system_id: UUID) -> _Domain | None:
        try:
            return connection.lookupByName(domain_name_for(system_id))
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise CategorizedError(
                "remote authority System domain lookup failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc

    def _guard_graph(self, connection: _Connection, system_id: UUID, overlay: str) -> None:
        try:
            domains = connection.listAllDomains(0)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote authority System domain enumeration failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        if len(domains) > _MAX_DOMAIN_DOCUMENTS:
            raise CategorizedError(
                "remote authority System domain enumeration exceeds 4096 entries",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            )
        prove_no_foreign_storage_references(
            cast(Any, connection),
            self._identity_port,
            str(system_id),
            frozenset({(self._pool_name, overlay)}),
        )

    def _new_intent(
        self,
        connection: _Connection,
        entry: RemoteAuthoritySystemManifestEntry,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> _ProvisionIntentV1:
        _pool, _base, base_path = self._base(connection, entry)
        overlay = overlay_volume_name(request.system_id)
        overlay_path = _storage_path(posixpath.join(posixpath.dirname(base_path), overlay))
        self._guard_graph(connection, request.system_id, overlay)
        domain_name = domain_name_for(request.system_id)
        gdb = allocate_port(
            used_gdb_ports(cast(Any, connection)),
            own_name=domain_name,
            port_min=entry.gdb_port_min + 1,
            port_max=entry.gdb_port_max,
        )
        ssh = allocate_port(
            used_ssh_ports(cast(Any, connection)),
            own_name=domain_name,
            port_min=entry.ssh_port_min,
            port_max=entry.ssh_port_max,
        )
        xml = render_domain_xml(
            request.system_id,
            snapshot.profile,
            pool=self._pool_name,
            volume=overlay,
            overlay_path=overlay_path,
            backing_path=base_path,
            gdb_addr=entry.gdb_addr,
            gdb_port=gdb,
            network=entry.network,
            machine=entry.machine,
            ssh_addr=entry.ssh_addr,
            ssh_port=ssh,
        )
        values: dict[str, Any] = {
            "system_id": request.system_id,
            "allocation_id": request.allocation_id,
            "resource_id": request.resource_id,
            "resource_name": request.resource_name,
            "authority_instance": request.authority_instance,
            "profile_identity": request.profile_identity,
            "root_identity": request.root_identity,
            "bootstrap_identity": request.bootstrap_identity,
            "operation_identity": request.operation_identity,
            "current_attempt": self._attempt(request, context),
            "architecture": entry.architecture,
            "pool": self._pool_name,
            "base_volume": entry.base_volume,
            "base_path": base_path,
            "overlay_volume": overlay,
            "overlay_path": overlay_path,
            "network": entry.network,
            "machine": entry.machine,
            "gdb_addr": entry.gdb_addr,
            "gdb_port": gdb,
            "ssh_addr": entry.ssh_addr,
            "ssh_port": ssh,
            "domain_xml": xml,
            "domain_projection": _projection_identity(xml),
            "deadline": self._deadline(),
            "phase": "planned",
            "intent_identity": "sha256:" + "0" * 64,
        }
        unchecked = _ProvisionIntentV1.model_construct(**values)
        values["intent_identity"] = _intent_identity(unchecked)
        return _ProvisionIntentV1.model_validate(values)

    def _same_subject(
        self,
        intent: _ProvisionIntentV1,
        request: AuthoritySystemMutationRequestV1,
        entry: RemoteAuthoritySystemManifestEntry,
    ) -> bool:
        return (
            intent.system_id == request.system_id
            and intent.allocation_id == request.allocation_id
            and intent.resource_id == request.resource_id
            and intent.resource_name == request.resource_name == entry.resource_name
            and intent.authority_instance == request.authority_instance == entry.authority_instance
            and intent.profile_identity == request.profile_identity
            and intent.root_identity == request.root_identity == entry.root_identity
            and intent.bootstrap_identity == request.bootstrap_identity
            and intent.operation_identity == request.operation_identity
            and intent.architecture == entry.architecture
            and intent.pool == self._pool_name
            and intent.base_volume == entry.base_volume
            and intent.network == entry.network
            and intent.machine == entry.machine
            and intent.gdb_addr == entry.gdb_addr
            and entry.gdb_port_min < intent.gdb_port <= entry.gdb_port_max
            and intent.ssh_addr == entry.ssh_addr
            and entry.ssh_port_min <= intent.ssh_port <= entry.ssh_port_max
        )

    def _adopt(
        self,
        intent: _ProvisionIntentV1,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        entry: RemoteAuthoritySystemManifestEntry,
    ) -> _ProvisionIntentV1:
        if not self._same_subject(intent, request, entry):
            raise CategorizedError(
                "remote authority System private intent has another binding",
                category=ErrorCategory.CONFLICT,
            )
        attempt = self._attempt(request, context)
        if intent.current_attempt == attempt:
            return intent
        adopted = _ProvisionIntentV1.model_validate(
            {**intent.model_dump(mode="python", by_alias=True), "current_attempt": attempt}
        )
        self._store.write(self._store.provision_name(request.system_id), adopted)
        return adopted

    def _checkpoint(
        self,
        intent: _ProvisionIntentV1,
        phase: Literal[
            "overlay-ready",
            "domain-defined",
            "boot-ready",
            "bootstrap-ready",
            "complete",
        ],
        *,
        normalized_definition: str | None = None,
    ) -> _ProvisionIntentV1:
        if _PHASES[phase] < _PHASES[intent.phase]:
            return intent
        values = intent.model_dump(mode="python", by_alias=True)
        values["phase"] = phase
        if normalized_definition is not None:
            values["normalized_definition"] = normalized_definition
        if phase == "complete" and intent.completed_at is None:
            values["completed_at"] = self._utc_now()
        updated = _ProvisionIntentV1.model_validate(values)
        self._store.write(self._store.provision_name(intent.system_id), updated)
        return updated

    @staticmethod
    def _validate_domain(domain: _Domain, intent: _ProvisionIntentV1) -> bool:
        expected = _domain_projection(intent.domain_xml)
        try:
            documents = [domain.XMLDesc(0), domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)]
            active = bool(domain.isActive())
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote authority System domain inspection failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        try:
            for document in documents:
                if _domain_projection(document) != expected:
                    raise CategorizedError(
                        "remote authority System domain differs from private intent",
                        category=ErrorCategory.CONFLICT,
                    )
                require_disk_grub_source(
                    document,
                    system_id=intent.system_id,
                    pool=intent.pool,
                    overlay_path=intent.overlay_path,
                )
        except ValueError as exc:
            raise CategorizedError(
                "remote authority System domain projection is invalid",
                category=ErrorCategory.CONFLICT,
            ) from exc
        inactive = documents[1]
        normalized = preserved_definition_identity(inactive)
        if intent.normalized_definition is not None and normalized != intent.normalized_definition:
            raise CategorizedError(
                "remote authority System normalized definition changed",
                category=ErrorCategory.CONFLICT,
            )
        return active

    @staticmethod
    def _normalized_definition(domain: _Domain) -> str:
        try:
            document = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote authority System inactive definition inspection failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        return preserved_definition_identity(document)

    def _overlay_owned(self, pool: _Pool, intent: _ProvisionIntentV1) -> bool:
        try:
            volume = pool.storageVolLookupByName(intent.overlay_volume)
            path = _storage_path(volume.path())
            backing = volume_backing_path(volume.XMLDesc(0))
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return False
            raise CategorizedError(
                "remote authority System overlay inspection failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        except ValueError as exc:
            raise CategorizedError(
                "remote authority System overlay metadata is invalid",
                category=ErrorCategory.CONFLICT,
            ) from exc
        if volume.name() != intent.overlay_volume or path != intent.overlay_path:
            raise CategorizedError(
                "remote authority System overlay identity changed",
                category=ErrorCategory.CONFLICT,
            )
        if _storage_path(backing or "") != intent.base_path:
            raise CategorizedError(
                "remote authority System overlay backing changed",
                category=ErrorCategory.CONFLICT,
            )
        return True

    def _observe_provision_locked(
        self, connection: _Connection, intent: _ProvisionIntentV1
    ) -> AuthoritySystemProvisionFacts:
        pool, _base, base_path = self._base(connection, self._manifest_entry_for_intent(intent))
        if base_path != intent.base_path:
            raise CategorizedError(
                "remote authority System base path changed",
                category=ErrorCategory.CONFLICT,
            )
        overlay_owned = self._overlay_owned(pool, intent)
        domain = self._domain_optional(connection, intent.system_id)
        active = domain is not None and self._validate_domain(domain, intent)
        domain_owned = domain is not None
        self._guard_graph(connection, intent.system_id, intent.overlay_volume)
        try:
            agent_connected = domain is not None and agent_channel_connected_strict(
                domain.XMLDesc(0),
                operation="observing authority System readiness",
                domain=domain_name_for(intent.system_id),
            )
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote authority System readiness inspection failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc
        boot_ready = (
            active
            and agent_connected
            and intent.phase in {"boot-ready", "bootstrap-ready", "complete"}
        )
        bootstrap_ready = boot_ready and intent.phase in {"bootstrap-ready", "complete"}
        complete = (
            overlay_owned
            and domain_owned
            and boot_ready
            and bootstrap_ready
            and intent.completed_at is not None
        )
        return AuthoritySystemProvisionFacts(
            intent_identity=intent.intent_identity,
            domain_owned=domain_owned,
            root_storage_owned=overlay_owned,
            boot_ready=boot_ready,
            bootstrap_ready=bootstrap_ready,
            quarantine_retained=not complete,
            completed_at=intent.completed_at if complete else None,
        )

    def _manifest_entry_for_intent(
        self, intent: _ProvisionIntentV1
    ) -> RemoteAuthoritySystemManifestEntry:
        try:
            return self._manifest[
                (intent.resource_name, intent.authority_instance, intent.root_identity)
            ]
        except KeyError:
            raise CategorizedError(
                "remote authority System private intent is no longer configured",
                category=ErrorCategory.CONFIGURATION_ERROR,
            ) from None

    def _execute_provision(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> AuthoritySystemProvisionFacts:
        entry = self._entry(request)
        if entry.architecture != snapshot.root_spec.architecture:
            raise CategorizedError(
                "remote authority System base architecture differs from root provenance",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        name = self._store.provision_name(request.system_id)
        intent = self._store.read(name, _ProvisionIntentV1)
        with self._connection() as connection:
            if intent is None:
                intent = self._new_intent(connection, entry, request, context, snapshot)
                self._store.write(name, intent)
            else:
                intent = self._adopt(intent, request, context, entry)
            if intent.phase == "complete":
                return self._observe_provision_locked(connection, intent)
            deadline = self._local_deadline(intent.deadline)
            pool, _base, base_path = self._base(connection, entry)
            if base_path != intent.base_path:
                raise CategorizedError(
                    "remote authority System base path changed",
                    category=ErrorCategory.CONFLICT,
                )
            self._guard_graph(connection, request.system_id, intent.overlay_volume)
            self._require_deadline(deadline)
            overlay = ensure_named_overlay(
                cast(Any, pool), entry.base_volume, intent.overlay_volume
            )
            if (
                _storage_path(overlay.path) != intent.overlay_path
                or _storage_path(overlay.backing_path) != intent.base_path
            ):
                raise CategorizedError(
                    "remote authority System overlay differs from private intent",
                    category=ErrorCategory.CONFLICT,
                )
            intent = self._checkpoint(intent, "overlay-ready")
            self._guard_graph(connection, request.system_id, intent.overlay_volume)
            domain = self._domain_optional(connection, request.system_id)
            if domain is None:
                self._require_deadline(deadline)
                try:
                    domain = connection.defineXML(intent.domain_xml)
                except libvirt.libvirtError as exc:
                    raise CategorizedError(
                        "remote authority System domain definition failed",
                        category=ErrorCategory.PROVISIONING_FAILURE,
                    ) from exc
            active = self._validate_domain(domain, intent)
            intent = self._checkpoint(
                intent,
                "domain-defined",
                normalized_definition=self._normalized_definition(domain),
            )
            self._guard_graph(connection, request.system_id, intent.overlay_volume)
            if not active:
                self._require_deadline(deadline)
                try:
                    domain.create()
                except libvirt.libvirtError as exc:
                    if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                        raise CategorizedError(
                            "remote authority System domain start failed",
                            category=ErrorCategory.PROVISIONING_FAILURE,
                        ) from exc
            self._validate_domain(domain, intent)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise TimeoutError("remote authority System deadline expired")
            wait_for_agent(
                cast(Any, connection),
                domain_name_for(request.system_id),
                monotonic=self._monotonic,
                sleep=self._sleep,
                timeout_s=remaining,
                poll_s=min(self._poll, remaining),
            )
            intent = self._checkpoint(intent, "boot-ready")
            if intent.phase not in {"bootstrap-ready", "complete"}:
                self._require_deadline(deadline)
                self._bootstrap.inject(cast(Any, domain), snapshot.bootstrap_public_key)
                intent = self._checkpoint(intent, "bootstrap-ready")
            intent = self._checkpoint(intent, "complete")
            return self._observe_provision_locked(connection, intent)

    def _observe_provision(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> AuthoritySystemProvisionFacts:
        entry = self._entry(request)
        intent = self._store.read(self._store.provision_name(request.system_id), _ProvisionIntentV1)
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
        intent = self._adopt_read_only(intent, request, context, entry)
        with self._connection() as connection:
            return self._observe_provision_locked(connection, intent)

    def _adopt_read_only(
        self,
        intent: _ProvisionIntentV1,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        entry: RemoteAuthoritySystemManifestEntry,
    ) -> _ProvisionIntentV1:
        if not self._same_subject(intent, request, entry):
            raise CategorizedError(
                "remote authority System private intent has another binding",
                category=ErrorCategory.CONFLICT,
            )
        del context
        return intent

    async def execute_system_provision(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> AuthoritySystemProvisionFacts:
        self._validate_context(request, context, AuthoritySystemOperation.PROVISION)
        self._validate_snapshot(request, snapshot)
        return await self._executor.run(lambda: self._execute_provision(request, context, snapshot))

    async def observe_system_provision(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        snapshot: AuthoritySystemProvisionSnapshot,
    ) -> AuthoritySystemProvisionFacts:
        self._validate_context(request, context, AuthoritySystemOperation.PROVISION)
        self._validate_snapshot(request, snapshot)
        return await self._executor.run(lambda: self._observe_provision(request, context, snapshot))

    async def execute_preactivation_teardown(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
    ) -> AuthoritySystemAbsenceFacts:
        self._validate_context(request, context, AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
        return await self._executor.run(lambda: self._execute_teardown(request, context))

    async def observe_preactivation_teardown(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
    ) -> AuthoritySystemAbsenceFacts:
        self._validate_context(request, context, AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
        return await self._executor.run(lambda: self._observe_teardown(request, context))

    def _execute_teardown(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
    ) -> AuthoritySystemAbsenceFacts:
        entry = self._entry(request)
        provision_name = self._store.provision_name(request.system_id)
        teardown_name = self._store.teardown_name(request.system_id)
        absent_name = self._store.absent_name(request.system_id)
        provision = self._store.read(provision_name, _ProvisionIntentV1)
        if provision is not None:
            self._validate_teardown_subject(provision, request, entry)
        receipt = self._store.read(absent_name, _AbsenceReceiptV1)
        provision_identity = (
            provision.intent_identity
            if provision is not None
            else (
                receipt.provision_intent_identity
                if receipt is not None
                else _never_began_identity(request)
            )
        )
        teardown = self._store.read(teardown_name, _TeardownIntentV1)
        if receipt is not None:
            self._validate_receipt(receipt, request, provision_identity)
        if teardown is None and receipt is None:
            teardown = _TeardownIntentV1(
                system_id=request.system_id,
                allocation_id=request.allocation_id,
                resource_id=request.resource_id,
                resource_name=request.resource_name,
                authority_instance=request.authority_instance,
                profile_identity=request.profile_identity,
                root_identity=request.root_identity,
                bootstrap_identity=request.bootstrap_identity,
                operation_identity=request.operation_identity,
                current_attempt=self._attempt(request, context),
                provision_intent_identity=provision_identity,
                deadline=self._deadline(),
            )
            self._store.write(teardown_name, teardown)
        elif teardown is not None:
            teardown = self._adopt_teardown(
                teardown, request, context, provision_identity, write=receipt is None
            )
        deadline = (
            self._local_deadline(teardown.deadline)
            if teardown is not None and receipt is None
            else None
        )
        with self._connection() as connection:
            if receipt is None:
                assert teardown is not None
                self._remove_remote_system(
                    connection, request, provision, deadline=cast(float, deadline)
                )
                observed = self._inspect_absence(connection, request, provision)
                if not all(observed):
                    return self._absence_facts(provision_identity, observed, None, False)
                completed_at = self._utc_now()
                values: dict[str, Any] = {
                    "system_id": request.system_id,
                    "allocation_id": request.allocation_id,
                    "resource_id": request.resource_id,
                    "resource_name": request.resource_name,
                    "authority_instance": request.authority_instance,
                    "profile_identity": request.profile_identity,
                    "root_identity": request.root_identity,
                    "bootstrap_identity": request.bootstrap_identity,
                    "operation_identity": request.operation_identity,
                    "provision_intent_identity": provision_identity,
                    "completed_at": completed_at,
                    "receipt_identity": "sha256:" + "0" * 64,
                }
                unchecked = _AbsenceReceiptV1.model_construct(**values)
                values["receipt_identity"] = _absence_identity(unchecked)
                receipt = _AbsenceReceiptV1.model_validate(values)
                self._store.write(absent_name, receipt)
            observed = self._inspect_absence(connection, request, provision)
            if not all(observed):
                raise CategorizedError(
                    "remote authority System absence receipt conflicts with provider state",
                    category=ErrorCategory.CONFLICT,
                )
        self._store.delete(provision_name)
        self._store.delete(teardown_name)
        return self._absence_facts(
            provision_identity, observed, receipt.completed_at, private_intent_absent=True
        )

    def _observe_teardown(
        self,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
    ) -> AuthoritySystemAbsenceFacts:
        entry = self._entry(request)
        provision = self._store.read(
            self._store.provision_name(request.system_id), _ProvisionIntentV1
        )
        if provision is not None:
            self._validate_teardown_subject(provision, request, entry)
        receipt = self._store.read(self._store.absent_name(request.system_id), _AbsenceReceiptV1)
        provision_identity = (
            provision.intent_identity
            if provision is not None
            else (
                receipt.provision_intent_identity
                if receipt is not None
                else _never_began_identity(request)
            )
        )
        teardown = self._store.read(self._store.teardown_name(request.system_id), _TeardownIntentV1)
        if teardown is not None:
            self._adopt_teardown(teardown, request, context, provision_identity, write=False)
        if receipt is not None:
            self._validate_receipt(receipt, request, provision_identity)
        with self._connection() as connection:
            observed = self._inspect_absence(connection, request, provision)
        private_absent = provision is None and teardown is None
        complete = receipt is not None and all(observed) and private_absent
        return self._absence_facts(
            provision_identity,
            observed,
            receipt.completed_at if complete else None,
            private_intent_absent=private_absent,
        )

    def _validate_teardown_subject(
        self,
        provision: _ProvisionIntentV1,
        request: AuthoritySystemMutationRequestV1,
        entry: RemoteAuthoritySystemManifestEntry,
    ) -> None:
        if (
            provision.system_id != request.system_id
            or provision.allocation_id != request.allocation_id
            or provision.resource_id != request.resource_id
            or provision.resource_name != request.resource_name
            or request.resource_name != entry.resource_name
            or provision.authority_instance != request.authority_instance
            or request.authority_instance != entry.authority_instance
            or provision.profile_identity != request.profile_identity
            or provision.root_identity != request.root_identity
            or request.root_identity != entry.root_identity
            or provision.bootstrap_identity != request.bootstrap_identity
            or provision.pool != self._pool_name
            or provision.base_volume != entry.base_volume
        ):
            raise CategorizedError(
                "remote authority System teardown differs from provision intent",
                category=ErrorCategory.CONFLICT,
            )

    def _adopt_teardown(
        self,
        teardown: _TeardownIntentV1,
        request: AuthoritySystemMutationRequestV1,
        context: AuthoritySystemCommitContextV1,
        provision_identity: str,
        *,
        write: bool,
    ) -> _TeardownIntentV1:
        if (
            teardown.system_id != request.system_id
            or teardown.allocation_id != request.allocation_id
            or teardown.resource_id != request.resource_id
            or teardown.resource_name != request.resource_name
            or teardown.authority_instance != request.authority_instance
            or teardown.profile_identity != request.profile_identity
            or teardown.root_identity != request.root_identity
            or teardown.bootstrap_identity != request.bootstrap_identity
            or teardown.operation_identity != request.operation_identity
            or teardown.provision_intent_identity != provision_identity
        ):
            raise CategorizedError(
                "remote authority System teardown intent has another binding",
                category=ErrorCategory.CONFLICT,
            )
        attempt = self._attempt(request, context)
        if teardown.current_attempt == attempt:
            return teardown
        adopted = _TeardownIntentV1.model_validate(
            {**teardown.model_dump(mode="python", by_alias=True), "current_attempt": attempt}
        )
        if write:
            self._store.write(self._store.teardown_name(request.system_id), adopted)
        return adopted

    @staticmethod
    def _validate_receipt(
        receipt: _AbsenceReceiptV1,
        request: AuthoritySystemMutationRequestV1,
        provision_identity: str,
    ) -> None:
        if (
            receipt.system_id != request.system_id
            or receipt.allocation_id != request.allocation_id
            or receipt.resource_id != request.resource_id
            or receipt.resource_name != request.resource_name
            or receipt.authority_instance != request.authority_instance
            or receipt.profile_identity != request.profile_identity
            or receipt.root_identity != request.root_identity
            or receipt.bootstrap_identity != request.bootstrap_identity
            or receipt.operation_identity != request.operation_identity
            or receipt.provision_intent_identity != provision_identity
        ):
            raise CategorizedError(
                "remote authority System absence receipt has another binding",
                category=ErrorCategory.CONFLICT,
            )

    def _inspect_absence(
        self,
        connection: _Connection,
        request: AuthoritySystemMutationRequestV1,
        provision: _ProvisionIntentV1 | None,
    ) -> tuple[bool, bool, bool]:
        domain = self._domain_optional(connection, request.system_id)
        overlay = overlay_volume_name(request.system_id)
        self._guard_graph(connection, request.system_id, overlay)
        if domain is not None:
            if provision is None:
                raise CategorizedError(
                    "remote authority System domain has no private provision intent",
                    category=ErrorCategory.CONFLICT,
                )
            self._validate_domain(domain, provision)
        try:
            pool = connection.storagePoolLookupByName(self._pool_name)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "remote authority System pool is unavailable",
                category=ErrorCategory.CONFIGURATION_ERROR,
            ) from exc
        return (
            domain is None,
            self._volume_optional(pool, overlay) is None,
            self._volume_optional(pool, supplied_base_volume_name(request.system_id)) is None,
        )

    @staticmethod
    def _volume_optional(pool: _Pool, name: str) -> _Volume | None:
        try:
            return pool.storageVolLookupByName(name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_VOL:
                return None
            raise CategorizedError(
                "remote authority System volume lookup failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            ) from exc

    def _remove_remote_system(
        self,
        connection: _Connection,
        request: AuthoritySystemMutationRequestV1,
        provision: _ProvisionIntentV1 | None,
        *,
        deadline: float,
    ) -> None:
        overlay_name = overlay_volume_name(request.system_id)
        self._guard_graph(connection, request.system_id, overlay_name)
        try:
            pool = connection.storagePoolLookupByName(self._pool_name)
        except libvirt.libvirtError as exc:
            category = (
                ErrorCategory.CONFIGURATION_ERROR
                if exc.get_error_code() == libvirt.VIR_ERR_NO_STORAGE_POOL
                else ErrorCategory.INFRASTRUCTURE_FAILURE
            )
            raise CategorizedError(
                "remote authority System pool is unavailable",
                category=category,
            ) from exc
        if self._volume_optional(pool, supplied_base_volume_name(request.system_id)) is not None:
            raise CategorizedError(
                "remote authority System has an unexpected per-System base volume",
                category=ErrorCategory.CONFLICT,
            )
        overlay = self._volume_optional(pool, overlay_name)
        if overlay is not None and (provision is None or not self._overlay_owned(pool, provision)):
            raise CategorizedError(
                "remote authority System overlay ownership is unavailable",
                category=ErrorCategory.CONFLICT,
            )
        domain = self._domain_optional(connection, request.system_id)
        if domain is not None:
            if provision is None:
                raise CategorizedError(
                    "remote authority System domain has no private provision intent",
                    category=ErrorCategory.CONFLICT,
                )
            try:
                persistent = bool(domain.isPersistent())
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    "remote authority System persistence inspection failed",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                ) from exc
            if not persistent:
                raise CategorizedError(
                    "remote authority System domain is not persistent",
                    category=ErrorCategory.CONFLICT,
                )
            active = self._validate_domain(domain, provision)
            if active:
                self._require_deadline(deadline)
                try:
                    domain.destroy()
                except libvirt.libvirtError as exc:
                    raise CategorizedError(
                        "remote authority System destroy failed",
                        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    ) from exc
                try:
                    still_active = bool(domain.isActive())
                except libvirt.libvirtError as exc:
                    raise CategorizedError(
                        "remote authority System activity inspection failed",
                        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    ) from exc
                if still_active:
                    raise RuntimeError("remote authority System remained active after destroy")
            self._validate_domain(domain, provision)
            self._guard_graph(connection, request.system_id, overlay_name)
            self._require_deadline(deadline)
            try:
                domain.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                    raise CategorizedError(
                        "remote authority System undefine failed",
                        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                    ) from exc
        if self._domain_optional(connection, request.system_id) is not None:
            raise RuntimeError("remote authority System remained defined after teardown")
        self._guard_graph(connection, request.system_id, overlay_name)
        overlay = self._volume_optional(pool, overlay_name)
        if overlay is not None:
            if provision is None:
                raise CategorizedError(
                    "remote authority System overlay has no private provision intent",
                    category=ErrorCategory.CONFLICT,
                )
            if not self._overlay_owned(pool, provision):
                raise CategorizedError(
                    "remote authority System overlay ownership is unavailable",
                    category=ErrorCategory.CONFLICT,
                )
            self._require_deadline(deadline)
            if overlay.name() != overlay_name:
                raise CategorizedError(
                    "remote authority System overlay name changed",
                    category=ErrorCategory.CONFLICT,
                )
            try:
                overlay.delete(0)
            except libvirt.libvirtError as exc:
                raise CategorizedError(
                    "remote authority System overlay deletion failed",
                    category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                ) from exc
        if self._volume_optional(pool, overlay_name) is not None:
            raise RuntimeError("remote authority System overlay remained after teardown")

    @staticmethod
    def _absence_facts(
        intent_identity: str,
        observed: tuple[bool, bool, bool],
        completed_at: datetime | None,
        private_intent_absent: bool,
    ) -> AuthoritySystemAbsenceFacts:
        domain_absent, root_absent, baseline_absent = observed
        complete = (
            domain_absent
            and root_absent
            and baseline_absent
            and private_intent_absent
            and completed_at is not None
        )
        return AuthoritySystemAbsenceFacts(
            intent_identity=intent_identity,
            domain_absent=domain_absent,
            root_storage_absent=root_absent,
            baseline_absent=baseline_absent,
            private_intent_absent=private_intent_absent,
            quarantine_retained=not complete,
            completed_at=completed_at if complete else None,
        )


def _storage_path(value: str) -> str:
    if (
        not value.startswith("/")
        or "\0" in value
        or len(value.encode()) > _MAX_STORAGE_PATH_BYTES
        or value != posixpath.normpath(value)
    ):
        raise CategorizedError(
            "remote authority System storage path is invalid",
            category=ErrorCategory.CONFLICT,
        )
    return value


__all__ = ["RemoteAuthoritySystemManifestEntry", "RemoteAuthoritySystemProvider"]
