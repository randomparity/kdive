"""Authority-owned remote-libvirt System provisioning over fixed private host inputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.providers.ports.external_boot import RootSource, RootSpecV1
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentity,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.xml import (
    overlay_volume_name,
    render_volume_xml,
    supplied_base_volume_name,
)
from kdive.providers.remote_libvirt.system_authority import (
    RemoteAuthoritySystemManifestEntry,
    RemoteAuthoritySystemProvider,
)
from kdive.providers.system_authority import (
    AuthoritySystemCommitContextV1,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemOperation,
    AuthoritySystemProvider,
    AuthoritySystemProvisionSnapshot,
)
from tests.providers.remote_libvirt.conftest import libvirt_error
from tests.providers.remote_libvirt.fakes import FakeStoragePool

SYSTEM_ID = UUID("00000000-0000-4000-8000-000000000101")
ALLOCATION_ID = UUID("00000000-0000-4000-8000-000000000102")
RESOURCE_ID = UUID("00000000-0000-4000-8000-000000000103")
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000104")
AUTHORITY_ID = UUID("00000000-0000-4000-8000-000000000105")
ATTEMPT_ID = UUID("00000000-0000-4000-8000-000000000106")
ROOT_IDENTITY = "sha256:" + "a" * 64
BOOTSTRAP_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIECB9Wd5HUF2geyiESLbVKWXf5F0M6dhj98HDZ0DnWyl"
BOOTSTRAP_IDENTITY = "sha256:" + hashlib.sha256(BOOTSTRAP_KEY.encode()).hexdigest()
DOMAIN_NAME = f"kdive-{SYSTEM_ID}"
BASE_VOLUME = "authority-base-a.qcow2"
POOL_NAME = "authority-systems"
type Architecture = Literal["x86_64", "ppc64le"]


def _profile(
    *, caller_base: str = "caller-selected.qcow2", architecture: Architecture = "x86_64"
) -> ProvisioningProfile:
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": architecture,
            "vcpu": 2,
            "memory_mb": 2048,
            "disk_gb": 8,
            "boot_method": "disk-image",
            "kernel_source_ref": "git+https://example.invalid/kernel#v1",
            "provider": {
                "remote-libvirt": {
                    "base_image_source": {
                        "kind": "catalog",
                        "provider": "remote-libvirt",
                        "name": caller_base,
                    },
                    "crashkernel": "256M",
                }
            },
        }
    )


def _request(
    operation: AuthoritySystemOperation = AuthoritySystemOperation.PROVISION,
    *,
    architecture: Architecture = "x86_64",
) -> AuthoritySystemMutationRequestV1:
    profile = _profile(architecture=architecture)
    return AuthoritySystemMutationRequestV1(
        system_id=SYSTEM_ID,
        allocation_id=ALLOCATION_ID,
        resource_id=RESOURCE_ID,
        provider_kind="remote-libvirt",
        resource_name="remote-a",
        authority_instance="authority-a",
        profile_identity="sha256:" + profile_digest(profile),
        root_identity=ROOT_IDENTITY,
        operation=operation,
        operation_identity=(
            "provision-a" if operation is AuthoritySystemOperation.PROVISION else "teardown-a"
        ),
        authority_id=AUTHORITY_ID,
        generation=1,
        attempt_id=ATTEMPT_ID,
        operation_digest="sha256:" + "b" * 64,
        bootstrap_identity=BOOTSTRAP_IDENTITY,
    )


def _context(
    operation: AuthoritySystemOperation = AuthoritySystemOperation.PROVISION,
) -> AuthoritySystemCommitContextV1:
    return AuthoritySystemCommitContextV1(
        attempt_id=ATTEMPT_ID,
        operation=operation,
        journal_sequence=3,
        journal_digest="sha256:" + "c" * 64,
    )


def _snapshot(*, architecture: Architecture = "x86_64") -> AuthoritySystemProvisionSnapshot:
    profile = _profile(architecture=architecture)
    return AuthoritySystemProvisionSnapshot(
        system_id=SYSTEM_ID,
        allocation_id=ALLOCATION_ID,
        resource_id=RESOURCE_ID,
        project="project-a",
        provider_kind="remote-libvirt",
        resource_name="remote-a",
        authority_instance="authority-a",
        profile=profile,
        profile_identity="sha256:" + profile_digest(profile),
        source_image_id=SOURCE_ID,
        root_identity=ROOT_IDENTITY,
        root_spec=RootSpecV1(
            architecture=architecture,
            root="/dev/vda1",
            arguments=("root=/dev/vda1", "ro"),
            authority="stage-inspection",
            source=RootSource(kind="staged-image", identity=ROOT_IDENTITY),
        ),
        bootstrap_public_key=BOOTSTRAP_KEY,
        bootstrap_identity=BOOTSTRAP_IDENTITY,
    )


def _manifest(*, architecture: Architecture = "x86_64") -> RemoteAuthoritySystemManifestEntry:
    return RemoteAuthoritySystemManifestEntry(
        resource_name="remote-a",
        authority_instance="authority-a",
        root_identity=ROOT_IDENTITY,
        architecture=architecture,
        base_volume=BASE_VOLUME,
        network="authority-net",
        machine="pc-i440fx-9.2" if architecture == "x86_64" else "pseries-9.2",
        gdb_addr="127.0.0.1",
        gdb_port_min=47000,
        gdb_port_max=47003,
        ssh_addr="127.0.0.1",
        ssh_port_min=47100,
        ssh_port_max=47103,
    )


def _set_text(root: ET.Element[str], path: str, value: str) -> None:
    element = root.find(path)
    assert element is not None
    element.text = value


class _Domain:
    def __init__(self, connection: _Connection, xml: str) -> None:
        self.connection = connection
        self.xml = xml
        self.active = False
        self.persistent = True
        self.snapshots = False
        self.destroyed = False
        self.undefined_flags: list[int] = []

    def name(self) -> str:
        return ET.fromstring(self.xml).findtext("name") or ""

    def create(self) -> int:
        self.connection.mutate("start")
        self.active = True
        return 0

    def destroy(self) -> int:
        self.connection.mutate("destroy")
        self.active = False
        self.destroyed = True
        return 0

    def undefineFlags(self, flags: int = 0) -> int:  # noqa: N802
        self.connection.mutate("undefine")
        self.undefined_flags.append(flags)
        self.connection.domains.pop(self.name(), None)
        return 0

    def isActive(self) -> int:  # noqa: N802
        return int(self.active)

    def isPersistent(self) -> int:  # noqa: N802
        return int(self.persistent)

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        if self.connection.xml_failures.get(flags, 0):
            self.connection.xml_failures[flags] -= 1
            raise libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)
        root = ET.fromstring(self.xml)
        if flags == 0 and self.active:
            root.set("id", "7")
            target = root.find("./devices/channel/target[@name='org.qemu.guest_agent.0']")
            assert target is not None
            if self.connection.agent_connected:
                target.set("state", "connected")
        devices = root.find("devices")
        assert devices is not None
        disk = devices.find("disk")
        assert disk is not None
        ET.SubElement(disk, "alias", name="virtio-disk0")
        ET.SubElement(
            disk,
            "address",
            type="pci",
            domain="0x0000",
            bus="0x03",
            slot="0x00",
            function="0x0",
        )
        for tag in ("emulator", "controller", "input", "audio", "watchdog", "memballoon"):
            ET.SubElement(devices, tag)
        transform = self.connection.xml_transforms.get(flags)
        if transform is not None:
            transform(root)
        return ET.tostring(root, encoding="unicode")


class _Pool(FakeStoragePool):
    def __init__(self, connection: _Connection) -> None:
        super().__init__(name=POOL_NAME, target_path="/authority/pool")
        self.connection = connection

    def createXML(self, xml: str, flags: int = 0):  # noqa: N802, ANN201
        name = ET.fromstring(xml).findtext("name")
        if name != BASE_VOLUME:
            self.connection.mutate("overlay")
        return super().createXML(xml, flags)

    def _remove(self, name: str) -> None:
        if name != BASE_VOLUME:
            self.connection.mutate("delete-overlay")
        super()._remove(name)


class _Connection:
    def __init__(self) -> None:
        self.domains: dict[str, _Domain] = {}
        self.mutations: list[str] = []
        self.mutation_hook: Any = None
        self.failures: dict[str, int] = {}
        self.xml_failures: dict[int, int] = {}
        self.xml_transforms: dict[int, Callable[[ET.Element[str]], None]] = {}
        self.agent_connected = True
        self.opens = 0
        self.pool = _Pool(self)
        self.pool.createXML(
            render_volume_xml(
                BASE_VOLUME,
                capacity_bytes=8 * 1024**3,
                backing_path="",
                owner_id=os.getuid(),
                group_id=os.getgid(),
            ).replace('<backingStore><path /><format type="qcow2" /></backingStore>', "")
        )
        self.mutations.clear()

    def mutate(self, operation: str) -> None:
        self.mutations.append(operation)
        if self.mutation_hook is not None:
            self.mutation_hook(operation)
        if self.failures.get(operation, 0):
            self.failures[operation] -= 1
            raise RuntimeError(f"interrupted during {operation}")

    def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
        if name != POOL_NAME:
            raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_POOL)
        return self.pool

    def listAllDomains(self, flags: int = 0) -> list[_Domain]:  # noqa: N802
        return list(self.domains.values())

    def lookupByName(self, name: str) -> _Domain:  # noqa: N802
        try:
            return self.domains[name]
        except KeyError as exc:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN) from exc

    def defineXML(self, xml: str) -> _Domain:  # noqa: N802
        self.mutate("define")
        domain = _Domain(self, xml)
        self.domains[domain.name()] = domain
        return domain


class _Identity:
    def __init__(self) -> None:
        self.aliases: dict[str, RemoteDeviceIdentity] = {}

    def identity(self, path: str) -> RemoteDeviceIdentity | None:
        return self.aliases.get(path) or RemoteDeviceIdentity(
            kind="inode", primary=1, secondary=abs(hash(path))
        )


class _Bootstrap:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.keys: list[str] = []
        self.release: threading.Event | None = None
        self.started = threading.Event()

    def inject(self, domain: _Domain, pubkey: str) -> None:
        assert domain.name() == DOMAIN_NAME
        self.connection.mutate("bootstrap")
        self.started.set()
        if self.release is not None:
            self.release.wait()
        self.keys.append(pubkey)


class _Clock:
    def __init__(self) -> None:
        self.utc = datetime(2026, 9, 6, 20, 0, tzinfo=UTC)
        self.monotonic = 100.0

    def now(self) -> datetime:
        return self.utc

    def tick(self) -> float:
        return self.monotonic


def _provider(
    tmp_path: Path,
    connection: _Connection,
    *,
    executor: RemoteModulePreparationExecutor | None = None,
    identity: _Identity | None = None,
    clock: _Clock | None = None,
    sleep: Callable[[float], None] | None = None,
    architecture: Architecture = "x86_64",
    validate_private_base: Callable[[str, str], None] = lambda _name, _path: None,
) -> tuple[RemoteAuthoritySystemProvider, _Bootstrap, RemoteModulePreparationExecutor, Path]:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    bootstrap = _Bootstrap(connection)
    owned_executor = executor or RemoteModulePreparationExecutor()
    authority_clock = clock or _Clock()

    @contextmanager
    def open_connection():
        connection.opens += 1
        yield connection

    provider = RemoteAuthoritySystemProvider(
        connection=open_connection,
        pool_name=POOL_NAME,
        manifest=(_manifest(architecture=architecture),),
        private_base_paths={BASE_VOLUME: Path("/authority/pool") / BASE_VOLUME},
        validate_private_base=validate_private_base,
        state_dir=state,
        executor=owned_executor,
        identity_port=identity or _Identity(),
        bootstrap_injector=bootstrap,
        clock=authority_clock.now,
        monotonic=authority_clock.tick,
        sleep=sleep or (lambda _seconds: None),
        provision_timeout_s=300.0,
        poll_interval_s=0.01,
    )
    return provider, bootstrap, owned_executor, state


@pytest.mark.anyio
async def test_provider_implements_fixed_port_and_persists_intent_before_mutation(
    tmp_path: Path,
) -> None:
    connection = _Connection()
    provider, bootstrap, executor, state = _provider(tmp_path, connection)
    intent_path = state / f"{SYSTEM_ID}.provision.json"
    intents_seen: list[dict[str, object]] = []
    connection.mutation_hook = lambda _operation: intents_seen.append(
        json.loads(intent_path.read_bytes())
    )
    typed: AuthoritySystemProvider = provider
    try:
        facts = await typed.execute_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()

    assert facts.complete
    assert intents_seen
    assert all(intent["bootstrap_identity"] == BOOTSTRAP_IDENTITY for intent in intents_seen)
    assert all(intent["deadline"] == intents_seen[0]["deadline"] for intent in intents_seen)
    assert all(intent["gdb_port"] == 47001 for intent in intents_seen)
    assert all(intent["ssh_port"] == 47100 for intent in intents_seen)
    assert bootstrap.keys == [BOOTSTRAP_KEY]
    assert BASE_VOLUME in connection.pool.listVolumes()
    assert "caller-selected.qcow2" not in connection.pool.listVolumes()
    assert overlay_volume_name(SYSTEM_ID) in connection.pool.listVolumes()
    assert connection.domains[DOMAIN_NAME].active
    assert (state / f"{SYSTEM_ID}.provision.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.anyio
@pytest.mark.parametrize("retained_domain", [False, True])
async def test_missing_intent_never_adopts_retained_provider_objects(
    tmp_path: Path, retained_domain: bool
) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        (state / f"{SYSTEM_ID}.provision.json").unlink()
        if not retained_domain:
            connection.domains.clear()
        mutations = list(connection.mutations)
        with pytest.raises(CategorizedError) as caught:
            await provider.execute_system_provision(_request(), _context(), _snapshot())
        assert caught.value.category is ErrorCategory.CONFLICT
        assert connection.mutations == mutations
        assert not list(state.iterdir())
    finally:
        executor.shutdown()


@pytest.mark.anyio
async def test_hardlinked_private_intent_is_rejected_before_provider_access(tmp_path: Path) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        os.link(state / f"{SYSTEM_ID}.provision.json", tmp_path / "intent-alias")
        opens = connection.opens
        with pytest.raises(ValueError, match="unsafe"):
            await provider.observe_system_provision(_request(), _context(), _snapshot())
        assert connection.opens == opens
    finally:
        executor.shutdown()


@pytest.mark.anyio
async def test_wrong_snapshot_binding_opens_no_connection(tmp_path: Path) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection)
    wrong = replace(_snapshot(), resource_name="remote-b")
    try:
        with pytest.raises(ValueError, match="snapshot"):
            await provider.execute_system_provision(_request(), _context(), wrong)
    finally:
        executor.shutdown()
    assert connection.opens == 0
    assert connection.mutations == []


@pytest.mark.anyio
async def test_base_volume_path_outside_manifest_private_base_fails_before_mutation(
    tmp_path: Path,
) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection)
    volume = connection.pool.storageVolLookupByName(BASE_VOLUME)
    volume._state = replace(volume._state, path="/escaped/base.qcow2")  # noqa: SLF001
    try:
        with pytest.raises(CategorizedError, match="base volume identity is invalid"):
            await provider.execute_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()

    assert connection.mutations == []


@pytest.mark.anyio
async def test_changed_private_base_identity_fails_before_mutation(tmp_path: Path) -> None:
    connection = _Connection()

    def reject_changed_base(_name: str, _path: str) -> None:
        raise ValueError("private base changed")

    provider, _bootstrap, executor, _state = _provider(
        tmp_path, connection, validate_private_base=reject_changed_base
    )
    try:
        with pytest.raises(CategorizedError, match="base volume identity is invalid"):
            await provider.execute_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()

    assert connection.mutations == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "source",
    (
        {"kind": "local", "path": "/caller/base.qcow2"},
        {
            "kind": "local",
            "path": "/caller/base.qcow2",
            "sha256": "sha256:" + "f" * 64,
        },
    ),
)
async def test_unpinned_or_mismatched_local_source_opens_no_connection(
    tmp_path: Path, source: dict[str, object]
) -> None:
    profile = ProvisioningProfile.parse(
        {
            **_profile().model_dump(mode="json", by_alias=True),
            "provider": {"remote-libvirt": {"base_image_source": source}},
        }
    )
    snapshot = replace(
        _snapshot(), profile=profile, profile_identity="sha256:" + profile_digest(profile)
    )
    request = _request().model_copy(update={"profile_identity": snapshot.profile_identity})
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection)
    try:
        with pytest.raises(CategorizedError, match="verified catalog or pinned"):
            await provider.execute_system_provision(request, _context(), snapshot)
    finally:
        executor.shutdown()
    assert connection.opens == 0
    assert connection.mutations == []


@pytest.mark.anyio
async def test_wrong_catalog_provider_opens_no_connection(tmp_path: Path) -> None:
    profile = ProvisioningProfile.parse(
        {
            **_profile().model_dump(mode="json", by_alias=True),
            "provider": {
                "remote-libvirt": {
                    "base_image_source": {
                        "kind": "catalog",
                        "provider": "local-libvirt",
                        "name": "base-a",
                    }
                }
            },
        }
    )
    snapshot = replace(
        _snapshot(), profile=profile, profile_identity="sha256:" + profile_digest(profile)
    )
    request = _request().model_copy(update={"profile_identity": snapshot.profile_identity})
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection)
    try:
        with pytest.raises(CategorizedError, match="verified catalog or pinned"):
            await provider.execute_system_provision(request, _context(), snapshot)
    finally:
        executor.shutdown()
    assert connection.opens == 0
    assert connection.mutations == []


@pytest.mark.anyio
async def test_absent_observation_is_read_only(tmp_path: Path) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    before = tuple(state.iterdir())
    try:
        facts = await provider.observe_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()
    assert not facts.complete
    assert facts.quarantine_retained
    assert tuple(state.iterdir()) == before
    assert connection.mutations == []


@pytest.mark.anyio
async def test_interrupted_definition_reuses_overlay_ports_and_retained_deadline(
    tmp_path: Path,
) -> None:
    connection = _Connection()
    connection.failures["define"] = 1
    provider, bootstrap, executor, state = _provider(tmp_path, connection)
    try:
        with pytest.raises(RuntimeError, match="define"):
            await provider.execute_system_provision(_request(), _context(), _snapshot())
        intent_path = state / f"{SYSTEM_ID}.provision.json"
        first = json.loads(intent_path.read_bytes())
        assert first["phase"] == "overlay-ready"

        facts = await provider.execute_system_provision(_request(), _context(), _snapshot())
        second = json.loads(intent_path.read_bytes())
    finally:
        executor.shutdown()

    assert facts.complete
    assert second["deadline"] == first["deadline"]
    assert second["gdb_port"] == first["gdb_port"]
    assert second["ssh_port"] == first["ssh_port"]
    assert connection.mutations.count("overlay") == 1
    assert bootstrap.keys == [BOOTSTRAP_KEY]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "phase"),
    (("overlay", "planned"), ("start", "domain-defined"), ("bootstrap", "boot-ready")),
)
async def test_interrupted_provision_phase_replays_exact_intent(
    tmp_path: Path, operation: str, phase: str
) -> None:
    connection = _Connection()
    connection.failures[operation] = 1
    provider, bootstrap, executor, state = _provider(tmp_path, connection)
    intent_path = state / f"{SYSTEM_ID}.provision.json"
    try:
        with pytest.raises(RuntimeError, match=operation):
            await provider.execute_system_provision(_request(), _context(), _snapshot())
        first = json.loads(intent_path.read_bytes())
        assert first["phase"] == phase

        facts = await provider.execute_system_provision(_request(), _context(), _snapshot())
        replayed = json.loads(intent_path.read_bytes())
    finally:
        executor.shutdown()

    assert facts.complete
    assert replayed["deadline"] == first["deadline"]
    assert replayed["gdb_port"] == first["gdb_port"]
    assert replayed["ssh_port"] == first["ssh_port"]
    assert bootstrap.keys == [BOOTSTRAP_KEY]


@pytest.mark.anyio
async def test_interrupted_readiness_reuses_started_domain(tmp_path: Path) -> None:
    connection = _Connection()
    connection.agent_connected = False

    def stop_polling(_seconds: float) -> None:
        raise RuntimeError("interrupted during readiness")

    provider, bootstrap, executor, state = _provider(tmp_path, connection, sleep=stop_polling)
    try:
        with pytest.raises(RuntimeError, match="readiness"):
            await provider.execute_system_provision(_request(), _context(), _snapshot())
        first = json.loads((state / f"{SYSTEM_ID}.provision.json").read_bytes())
        connection.agent_connected = True

        facts = await provider.execute_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()

    assert first["phase"] == "domain-defined"
    assert facts.complete
    assert connection.mutations.count("start") == 1
    assert bootstrap.keys == [BOOTSTRAP_KEY]


@pytest.mark.anyio
async def test_completed_provision_replays_after_retained_deadline(tmp_path: Path) -> None:
    connection = _Connection()
    clock = _Clock()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection, clock=clock)
    try:
        first = await provider.execute_system_provision(_request(), _context(), _snapshot())
        mutations = list(connection.mutations)
        clock.utc += timedelta(hours=1)
        replay = await provider.execute_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()

    assert replay == first
    assert connection.mutations == mutations


@pytest.mark.anyio
async def test_expired_partial_provision_does_not_resume_mutation(tmp_path: Path) -> None:
    connection = _Connection()
    connection.failures["define"] = 1
    clock = _Clock()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection, clock=clock)
    try:
        with pytest.raises(RuntimeError, match="define"):
            await provider.execute_system_provision(_request(), _context(), _snapshot())
        mutations = list(connection.mutations)
        clock.utc += timedelta(minutes=6)
        with pytest.raises(TimeoutError, match="deadline"):
            await provider.execute_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()

    assert connection.mutations == mutations


@pytest.mark.anyio
async def test_observe_complete_provision_changes_no_private_or_libvirt_state(
    tmp_path: Path,
) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        before_files = {path.name: path.read_bytes() for path in state.iterdir()}
        before_mutations = list(connection.mutations)
        facts = await provider.observe_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()

    assert facts.complete
    assert {path.name: path.read_bytes() for path in state.iterdir()} == before_files
    assert connection.mutations == before_mutations


@pytest.mark.anyio
@pytest.mark.parametrize("xml_flags", (0, libvirt.VIR_DOMAIN_XML_INACTIVE))
async def test_live_and_inactive_domain_mismatch_fail_closed(
    tmp_path: Path, xml_flags: int
) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection)
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        before = list(connection.mutations)

        def change_network(root: ET.Element[str]) -> None:
            source = root.find("./devices/interface/source")
            assert source is not None
            source.set("network", "foreign-net")

        connection.xml_transforms[xml_flags] = change_network
        with pytest.raises(CategorizedError) as caught:
            await provider.observe_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()

    assert caught.value.category is ErrorCategory.CONFLICT
    assert connection.mutations == before


@pytest.mark.anyio
@pytest.mark.parametrize(
    "extra_device", ("disk", "interface", "channel", "filesystem", "hostdev", "rng")
)
async def test_unexpected_device_in_readback_fails_closed(
    tmp_path: Path, extra_device: str
) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection)
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        before = list(connection.mutations)

        def add_device(root: ET.Element[str]) -> None:
            devices = root.find("./devices")
            assert devices is not None
            device = ET.SubElement(devices, extra_device)
            if extra_device == "disk":
                device.set("device", "cdrom")
            elif extra_device == "interface":
                device.set("type", "bridge")
            elif extra_device == "channel":
                device.set("type", "spicevmc")

        connection.xml_transforms[libvirt.VIR_DOMAIN_XML_INACTIVE] = add_device
        with pytest.raises(CategorizedError) as caught:
            await provider.observe_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()

    assert caught.value.category is ErrorCategory.CONFLICT
    assert connection.mutations == before


@pytest.mark.anyio
async def test_preactivation_teardown_removes_only_owned_system_and_replays_absence(
    tmp_path: Path,
) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    sibling = "sibling.qcow2"
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        domain = connection.domains[DOMAIN_NAME]
        domain.snapshots = True
        connection.pool.createXML(
            render_volume_xml(
                sibling,
                capacity_bytes=4096,
                backing_path="/authority/pool/authority-base-a.qcow2",
                owner_id=os.getuid(),
                group_id=os.getgid(),
            )
        )
        connection.mutations.clear()
        request = _request(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
        context = _context(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)

        first = await provider.execute_preactivation_teardown(request, context)
        replay = await provider.execute_preactivation_teardown(request, context)
        observed = await provider.observe_preactivation_teardown(request, context)
    finally:
        executor.shutdown()

    assert first.complete and replay == first and observed == first
    assert domain.undefined_flags == [libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA]
    assert DOMAIN_NAME not in connection.domains
    assert overlay_volume_name(SYSTEM_ID) not in connection.pool.listVolumes()
    assert BASE_VOLUME in connection.pool.listVolumes()
    assert sibling in connection.pool.listVolumes()
    assert not (state / f"{SYSTEM_ID}.provision.json").exists()
    assert not (state / f"{SYSTEM_ID}.teardown.json").exists()
    assert (state / f"{SYSTEM_ID}.absent.json").is_file()


@pytest.mark.anyio
async def test_teardown_without_prior_intent_records_exact_absent_replay(tmp_path: Path) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection)
    request = _request(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
    context = _context(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
    try:
        first = await provider.execute_preactivation_teardown(request, context)
        second = await provider.execute_preactivation_teardown(request, context)
    finally:
        executor.shutdown()
    assert first.complete
    assert second == first
    assert connection.mutations == []


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ("destroy", "undefine", "delete-overlay"))
async def test_interrupted_teardown_resumes_without_touching_siblings(
    tmp_path: Path, operation: str
) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    sibling = "retained-sibling.qcow2"
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        connection.pool.createXML(
            render_volume_xml(
                sibling,
                capacity_bytes=4096,
                backing_path=f"/authority/pool/{BASE_VOLUME}",
                owner_id=os.getuid(),
                group_id=os.getgid(),
            )
        )
        connection.failures[operation] = 1
        request = _request(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
        context = _context(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
        with pytest.raises(RuntimeError, match=operation):
            await provider.execute_preactivation_teardown(request, context)
        first = json.loads((state / f"{SYSTEM_ID}.teardown.json").read_bytes())

        facts = await provider.execute_preactivation_teardown(request, context)
    finally:
        executor.shutdown()

    assert facts.complete
    assert first["deadline"]
    assert sibling in connection.pool.listVolumes()
    assert BASE_VOLUME in connection.pool.listVolumes()


@pytest.mark.anyio
async def test_partial_teardown_observation_is_byte_for_byte_read_only(tmp_path: Path) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    request = _request(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
    context = _context(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        connection.failures["destroy"] = 1
        with pytest.raises(RuntimeError, match="destroy"):
            await provider.execute_preactivation_teardown(request, context)
        before_files = {path.name: path.read_bytes() for path in state.iterdir()}
        before_mutations = list(connection.mutations)

        facts = await provider.observe_preactivation_teardown(request, context)
    finally:
        executor.shutdown()

    assert not facts.complete
    assert facts.quarantine_retained
    assert {path.name: path.read_bytes() for path in state.iterdir()} == before_files
    assert connection.mutations == before_mutations


@pytest.mark.anyio
async def test_persisted_absence_receipt_replays_after_deadline(tmp_path: Path) -> None:
    connection = _Connection()
    clock = _Clock()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection, clock=clock)
    request = _request(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
    context = _context(AuthoritySystemOperation.PREACTIVATION_TEARDOWN)
    store = cast(Any, provider)._store
    original_delete = store.delete
    interrupted = False

    def stop_after_receipt(name: str) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise RuntimeError("interrupted after receipt")
        original_delete(name)

    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        store.delete = stop_after_receipt
        with pytest.raises(RuntimeError, match="after receipt"):
            await provider.execute_preactivation_teardown(request, context)
        assert (state / f"{SYSTEM_ID}.absent.json").is_file()
        assert (state / f"{SYSTEM_ID}.teardown.json").is_file()
        store.delete = original_delete
        clock.utc += timedelta(hours=1)

        facts = await provider.execute_preactivation_teardown(request, context)
    finally:
        executor.shutdown()

    assert facts.complete
    assert not (state / f"{SYSTEM_ID}.provision.json").exists()
    assert not (state / f"{SYSTEM_ID}.teardown.json").exists()


@pytest.mark.anyio
async def test_unexpected_per_system_base_blocks_teardown_before_effect(tmp_path: Path) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection)
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        connection.pool.createXML(
            render_volume_xml(
                supplied_base_volume_name(SYSTEM_ID),
                capacity_bytes=4096,
                backing_path=f"/authority/pool/{BASE_VOLUME}",
                owner_id=os.getuid(),
                group_id=os.getgid(),
            )
        )
        connection.mutations.clear()

        with pytest.raises(CategorizedError, match="unexpected per-System base") as caught:
            await provider.execute_preactivation_teardown(
                _request(AuthoritySystemOperation.PREACTIVATION_TEARDOWN),
                _context(AuthoritySystemOperation.PREACTIVATION_TEARDOWN),
            )
    finally:
        executor.shutdown()

    assert caught.value.category is ErrorCategory.CONFLICT
    assert connection.mutations == []
    assert connection.domains[DOMAIN_NAME].active
    assert overlay_volume_name(SYSTEM_ID) in connection.pool.listVolumes()


@pytest.mark.anyio
async def test_foreign_named_volume_alias_blocks_teardown_without_effect(tmp_path: Path) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection)
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        foreign_root = ET.fromstring(connection.domains[DOMAIN_NAME].xml)
        _set_text(foreign_root, "name", "foreign-domain")
        _set_text(foreign_root, "uuid", str(UUID(int=999)))
        _set_text(
            foreign_root,
            "./metadata/{https://kdive.dev/libvirt/1}domain/{https://kdive.dev/libvirt/1}system",
            str(UUID(int=999)),
        )
        source = foreign_root.find("./devices/disk/source")
        assert source is not None
        source.attrib.clear()
        source.set("pool", POOL_NAME)
        source.set("volume", overlay_volume_name(SYSTEM_ID))
        foreign = _Domain(connection, ET.tostring(foreign_root, encoding="unicode"))
        connection.domains[foreign.name()] = foreign
        before = list(connection.mutations)

        with pytest.raises(CategorizedError, match="another domain references") as caught:
            await provider.execute_preactivation_teardown(
                _request(AuthoritySystemOperation.PREACTIVATION_TEARDOWN),
                _context(AuthoritySystemOperation.PREACTIVATION_TEARDOWN),
            )
    finally:
        executor.shutdown()
    assert connection.domains[DOMAIN_NAME].active
    assert overlay_volume_name(SYSTEM_ID) in connection.pool.listVolumes()
    assert connection.mutations == before
    assert caught.value.category is ErrorCategory.CONFLICT


@pytest.mark.anyio
async def test_path_alias_blocks_teardown_without_effect(tmp_path: Path) -> None:
    connection = _Connection()
    identity = _Identity()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection, identity=identity)
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        overlay_path = f"/authority/pool/{overlay_volume_name(SYSTEM_ID)}"
        shared = RemoteDeviceIdentity(kind="inode", primary=7, secondary=9)
        identity.aliases[overlay_path] = shared
        identity.aliases["/authority/alias"] = shared
        foreign_root = ET.fromstring(connection.domains[DOMAIN_NAME].xml)
        _set_text(foreign_root, "name", "foreign-domain")
        _set_text(foreign_root, "uuid", str(UUID(int=999)))
        _set_text(
            foreign_root,
            "./metadata/{https://kdive.dev/libvirt/1}domain/{https://kdive.dev/libvirt/1}system",
            str(UUID(int=999)),
        )
        source = foreign_root.find("./devices/disk/source")
        assert source is not None
        source.set("file", "/authority/alias")
        foreign = _Domain(connection, ET.tostring(foreign_root, encoding="unicode"))
        connection.domains[foreign.name()] = foreign

        with pytest.raises(CategorizedError, match="another domain references") as caught:
            await provider.execute_preactivation_teardown(
                _request(AuthoritySystemOperation.PREACTIVATION_TEARDOWN),
                _context(AuthoritySystemOperation.PREACTIVATION_TEARDOWN),
            )
    finally:
        executor.shutdown()
    assert connection.domains[DOMAIN_NAME].active
    assert overlay_volume_name(SYSTEM_ID) in connection.pool.listVolumes()
    assert caught.value.category is ErrorCategory.CONFLICT


@pytest.mark.anyio
async def test_same_system_metadata_on_sibling_domain_blocks_teardown(tmp_path: Path) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection)
    try:
        await provider.execute_system_provision(_request(), _context(), _snapshot())
        foreign_root = ET.fromstring(connection.domains[DOMAIN_NAME].xml)
        _set_text(foreign_root, "name", "foreign-domain")
        _set_text(foreign_root, "uuid", str(UUID(int=999)))
        foreign = _Domain(connection, ET.tostring(foreign_root, encoding="unicode"))
        connection.domains[foreign.name()] = foreign
        before = list(connection.mutations)

        with pytest.raises(CategorizedError, match="another domain references") as caught:
            await provider.execute_preactivation_teardown(
                _request(AuthoritySystemOperation.PREACTIVATION_TEARDOWN),
                _context(AuthoritySystemOperation.PREACTIVATION_TEARDOWN),
            )
    finally:
        executor.shutdown()

    assert caught.value.category is ErrorCategory.CONFLICT
    assert connection.domains[DOMAIN_NAME].active
    assert connection.mutations == before


@pytest.mark.anyio
async def test_domain_enumeration_4097_fails_before_intent_or_mutation(tmp_path: Path) -> None:
    connection = _Connection()
    for index in range(4097):
        xml = f"<domain><name>foreign-{index}</name><devices/></domain>"
        connection.domains[f"foreign-{index}"] = _Domain(connection, xml)
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    try:
        with pytest.raises(CategorizedError, match="4096") as caught:
            await provider.execute_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()
    assert tuple(state.iterdir()) == ()
    assert connection.mutations == []
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


@pytest.mark.anyio
async def test_ppc64le_uses_fixed_machine_and_completes_without_host_arch_inference(
    tmp_path: Path,
) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection, architecture="ppc64le")
    try:
        facts = await provider.execute_system_provision(
            _request(architecture="ppc64le"),
            _context(),
            _snapshot(architecture="ppc64le"),
        )
    finally:
        executor.shutdown()

    domain_xml = connection.domains[DOMAIN_NAME].xml
    assert facts.complete
    assert '<type arch="ppc64le" machine="pseries-9.2">hvm</type>' in domain_xml


@pytest.mark.anyio
async def test_architecture_mismatch_is_rejected_before_connection(tmp_path: Path) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, _state = _provider(tmp_path, connection, architecture="ppc64le")
    try:
        with pytest.raises(CategorizedError, match="architecture") as caught:
            await provider.execute_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert connection.opens == 0
    assert connection.mutations == []


@pytest.mark.parametrize(
    ("architecture", "machine"),
    (("x86_64", "pc"), ("x86_64", "q35"), ("ppc64le", "pseries")),
)
def test_manifest_rejects_machine_aliases(architecture: Architecture, machine: str) -> None:
    with pytest.raises(ValueError, match="concrete architecture-matched"):
        replace(_manifest(architecture=architecture), machine=machine)


@pytest.mark.anyio
async def test_cancellation_waits_for_bootstrap_completion_and_preserves_count(
    tmp_path: Path,
) -> None:
    connection = _Connection()
    provider, bootstrap, executor, state = _provider(tmp_path, connection)
    bootstrap.release = threading.Event()
    task = asyncio.create_task(
        provider.execute_system_provision(_request(), _context(), _snapshot())
    )
    await asyncio.to_thread(bootstrap.started.wait)
    task.cancel("caller stopped")
    await asyncio.sleep(0)
    task.cancel("later cancellation")
    await asyncio.sleep(0)
    assert not task.done()
    bootstrap.release.set()
    try:
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
    finally:
        executor.shutdown()
    assert caught.value.args == ("caller stopped",)
    assert task.cancelling() == 2
    intent = json.loads((state / f"{SYSTEM_ID}.provision.json").read_bytes())
    assert intent["phase"] == "complete"


@pytest.mark.anyio
async def test_symlinked_private_record_fails_closed_without_provider_access(
    tmp_path: Path,
) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    target = tmp_path / "target"
    target.write_text("{}")
    (state / f"{SYSTEM_ID}.provision.json").symlink_to(target)
    try:
        with pytest.raises(ValueError, match="unsafe"):
            await provider.observe_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()
    assert connection.opens == 0
    assert connection.mutations == []


@pytest.mark.anyio
@pytest.mark.parametrize("unsafe", (b"{}", b"{}\n\n"))
async def test_noncanonical_private_record_fails_before_provider_access(
    tmp_path: Path, unsafe: bytes
) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    (state / f"{SYSTEM_ID}.provision.json").write_bytes(unsafe)
    os.chmod(state / f"{SYSTEM_ID}.provision.json", 0o600)
    try:
        with pytest.raises(ValueError, match="newline"):
            await provider.observe_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()
    assert connection.opens == 0
    assert connection.mutations == []


@pytest.mark.anyio
async def test_wrong_private_record_mode_fails_before_provider_access(tmp_path: Path) -> None:
    connection = _Connection()
    provider, _bootstrap, executor, state = _provider(tmp_path, connection)
    record = state / f"{SYSTEM_ID}.provision.json"
    record.write_bytes(b"{}\n")
    record.chmod(0o644)
    try:
        with pytest.raises(ValueError, match="unsafe"):
            await provider.observe_system_provision(_request(), _context(), _snapshot())
    finally:
        executor.shutdown()
    assert connection.opens == 0
    assert connection.mutations == []
