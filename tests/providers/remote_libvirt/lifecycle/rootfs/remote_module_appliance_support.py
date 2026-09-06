from __future__ import annotations

import json
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import replace

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs import remote_module_appliance
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance import (
    INVOCATION_TIMEOUT_SECONDS,
    ApplianceRequest,
    ApplianceStream,
    UnresolvedCallError,
    observe_appliance_absent_and_detached,
    render_remote_module_appliance,
    run_or_adopt_appliance,
    teardown_remote_module_appliance,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
    ExpectedAppliance,
    ExpectedAttachmentState,
    RemoteDeviceIdentity,
    inspect_module_attachments,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleResultV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import PreparedVolume
from kdive.security.secrets.secret_registry import SecretRegistry

UUID1 = "12345678-1234-4123-8123-123456789abc"
UUID2 = "22345678-1234-4123-8123-123456789abc"
DIGEST = "sha256:" + "a" * 64
MANIFEST = "sha256:" + "b" * 64


def libvirt_error(code: int) -> libvirt.libvirtError:
    error = libvirt.libvirtError("synthetic")
    error.err = (code, 0, "synthetic", 0, "", None, None, 0, 0)
    return error


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class Executor:
    def call[T](self, operation: Callable[[], T], deadline: float) -> T:
        return operation()


class DeadlineAwareExecutor:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.abort_deadline: float | None = None
        self.deadlines: list[float] = []

    def call[T](self, operation: Callable[[], T], deadline: float) -> T:
        self.deadlines.append(deadline)
        if deadline <= self.clock():
            raise TimeoutError
        if getattr(operation, "__name__", None) == "abort":
            self.abort_deadline = deadline
        return operation()


class Stream:
    def __init__(self, chunks: Sequence[bytes | int | None], clock: Clock) -> None:
        self.chunks = list(chunks)
        self.clock = clock
        self.aborted = False

    def recv(self, size: int) -> bytes | int | None:
        self.clock.value += 1
        return self.chunks.pop(0) if self.chunks else b""

    def abort(self) -> int:
        self.aborted = True
        return 0


class Domain:
    def __init__(self, xml: str, *, active: bool = True, persistent: bool = False) -> None:
        self.xml = xml
        self.active = active
        self.persistent = persistent
        self.destroyed = False

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        return self.xml

    def isActive(self) -> int:  # noqa: N802
        return int(self.active)

    def isPersistent(self) -> int:  # noqa: N802
        return int(self.persistent)

    def openConsole(  # noqa: N802
        self, device: str | None, stream: ApplianceStream, flags: int
    ) -> int:
        return 0

    def destroy(self) -> int:
        self.destroyed = True
        return 0


class Conn:
    def __init__(
        self,
        chunks: Sequence[bytes | int | None],
        clock: Clock,
        *,
        existing: Domain | None = None,
        lookup_error: libvirt.libvirtError | None = None,
    ) -> None:
        self.chunks = chunks
        self.clock = clock
        self.domain = existing
        self.lookup_error = lookup_error
        self.created_flags: int | None = None
        self.stream: Stream | None = None

    def lookupByName(self, name: str) -> Domain:  # noqa: N802
        if self.lookup_error is not None:
            raise self.lookup_error
        if self.domain is None:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return self.domain

    def createXML(self, xml: str, flags: int = 0) -> Domain:  # noqa: N802
        self.created_flags = flags
        self.domain = Domain(xml)
        return self.domain

    def newStream(self, flags: int = 0) -> Stream:  # noqa: N802
        self.stream = Stream(self.chunks, self.clock)
        return self.stream


class FailingDestroyDomain(Domain):
    def destroy(self) -> int:
        raise RuntimeError("destroy failed")


class FailingDestroyConn(Conn):
    def createXML(self, xml: str, flags: int = 0) -> Domain:  # noqa: N802
        self.created_flags = flags
        self.domain = FailingDestroyDomain(xml)
        return self.domain


class FailingOpenDomain(Domain):
    def __init__(self, xml: str, failure: Exception) -> None:
        super().__init__(xml)
        self.failure = failure

    def openConsole(  # noqa: N802
        self, device: str | None, stream: ApplianceStream, flags: int
    ) -> int:
        raise self.failure


def operation() -> RemoteModuleOperationV1:
    return RemoteModuleOperationV1(
        operation="capture_install",
        system_id=UUID1,
        run_id=UUID2,
        plan_identity=DIGEST,
        operation_nonce="1" * 32,
        release="6.8.0-test",
        root_volume={"key": "root", "identity": DIGEST},
        source_manifest=MANIFEST,
        appliance_image_digest=DIGEST,
    )


def success_result() -> bytes:
    return RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=UUID1,
        run_id=UUID2,
        plan_identity=DIGEST,
        operation_nonce="1" * 32,
        appliance_image_digest=DIGEST,
        release="6.8.0-test",
        root_volume_key="root",
        root_volume_identity=DIGEST,
        source_manifest=MANIFEST,
        installed_manifest=MANIFEST,
        capture_absent=True,
        entry_count=0,
        content_bytes=0,
    ).to_wire_bytes()


def volume(name: str, purpose: str) -> PreparedVolume:
    return PreparedVolume("pool", name, UUID1, UUID2, "1" * 32, purpose, DIGEST, 4096)


def request(clock: Clock, *, result: bytes | None = None) -> ApplianceRequest:
    registry = SecretRegistry()
    registry.register("split-secret", scope=None)
    return ApplianceRequest(
        name="kdive-module-12345678-11111111",
        architecture="x86_64",
        emulator_path="/usr/bin/qemu-system-x86_64",
        memory_kib=262_144,
        vcpus=1,
        pool="pool",
        appliance_volume="appliance-x86_64.qcow2",
        appliance_image_digest=DIGEST,
        root=volume("root", "root"),
        source=volume("source", "source"),
        scratch=volume("scratch", "scratch"),
        operation=operation(),
        secret_registry=registry,
        read_scratch_result=lambda: result,
        inspect_attachments=lambda: AttachmentInspection(
            True,
            True,
            False,
            frozenset({("pool", "source"), ("pool", "scratch")}),
        ),
        executor=Executor(),
        monotonic=clock,
    )


@pytest.mark.parametrize("architecture", ["x86_64", "ppc64le"])
def test_xml_is_exactly_confined_and_native(architecture: str) -> None:
    req = replace(request(Clock()), architecture=architecture)
    root = ET.fromstring(render_remote_module_appliance(req))
    assert root.attrib == {"type": "kvm"}
    assert root.findtext("name") == req.name
    type_node = root.find("./os/type")
    assert type_node is not None
    machine = "q35" if architecture == "x86_64" else "pseries"
    assert type_node.attrib == {"arch": architecture, "machine": machine}
    assert root.find("./devices/interface") is None
    assert root.find("./devices/graphics") is None
    assert root.find("./devices/filesystem") is None
    assert root.find("./devices/channel") is None
    disks = []
    for disk in root.findall("./devices/disk"):
        source = disk.find("source")
        target = disk.find("target")
        assert source is not None and target is not None
        disks.append((source.attrib, target.attrib, disk.find("readonly") is not None))
    assert disks == [
        (
            {"pool": "pool", "volume": "appliance-x86_64.qcow2"},
            {"dev": "vda", "bus": "virtio"},
            True,
        ),
        ({"pool": "pool", "volume": "root"}, {"dev": "vdb", "bus": "virtio"}, False),
        ({"pool": "pool", "volume": "source"}, {"dev": "vdc", "bus": "virtio"}, True),
        ({"pool": "pool", "volume": "scratch"}, {"dev": "vdd", "bus": "virtio"}, False),
    ]


def test_launch_uses_auto_destroy_and_only_scratch_proves_completion() -> None:
    clock = Clock()
    forged = json.dumps({"status": "success", "phase": "installed"}).encode() + b"\n"
    result = RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=UUID1,
        run_id=UUID2,
        plan_identity=DIGEST,
        operation_nonce="1" * 32,
        appliance_image_digest=DIGEST,
        release="6.8.0-test",
        root_volume_key="root",
        root_volume_identity=DIGEST,
        source_manifest=MANIFEST,
        installed_manifest=MANIFEST,
        capture_absent=True,
        entry_count=0,
        content_bytes=0,
    ).to_wire_bytes()
    conn = Conn([forged], clock)
    outcome = run_or_adopt_appliance(conn, request(clock, result=result))
    assert conn.created_flags == libvirt.VIR_DOMAIN_START_AUTODESTROY
    assert outcome.result is not None and outcome.result.phase == "installed"


def test_console_gets_fresh_wait_after_domain_setup_with_shared_cap() -> None:
    clock = Clock()
    req = request(clock)
    executor = DeadlineAwareExecutor(clock)

    class SlowLookupConn(Conn):
        def lookupByName(self, name: str) -> Domain:  # noqa: N802
            clock.value = 20.0
            return super().lookupByName(name)

    conn = SlowLookupConn([], clock, existing=Domain(render_remote_module_appliance(req)))
    outcome = run_or_adopt_appliance(conn, replace(req, executor=executor))
    assert outcome.timed_out is False
    assert executor.deadlines[:4] == [30.0, 30.0, 30.0, 30.0]
    assert executor.deadlines[4] == 50.0
    assert outcome.result is None


def test_teardown_uses_inherited_invocation_deadline_without_starting_lookup() -> None:
    clock = Clock()
    clock.value = 31.0
    req = replace(
        request(clock),
        executor=DeadlineAwareExecutor(clock),
        invocation_deadline=30.0,
    )
    conn = Conn([], clock)
    with pytest.raises(TimeoutError):
        teardown_remote_module_appliance(conn, req)
    assert conn.domain is None


@pytest.mark.parametrize("effect", ["create", "destroy"])
def test_queued_appliance_mutation_cannot_start_after_public_timeout(effect: str) -> None:
    clock = Clock()
    release = threading.Event()
    domain = Domain("<domain/>")
    conn = Conn([], clock)

    class OccupiedExecutor:
        def __init__(self) -> None:
            self.pool = ThreadPoolExecutor(max_workers=1)
            self.pool.submit(release.wait)

        def call[T](self, operation: Callable[[], T], deadline: float) -> T:
            future = self.pool.submit(operation)
            try:
                return future.result(timeout=0.01)
            except FutureTimeoutError:
                clock.value = deadline
                raise TimeoutError from None

    executor = OccupiedExecutor()
    req = replace(request(clock), executor=executor, invocation_deadline=30.0)

    def operation() -> Domain | int:
        if effect == "create":
            return conn.createXML("<domain/>", libvirt.VIR_DOMAIN_START_AUTODESTROY)
        return domain.destroy()

    with pytest.raises(TimeoutError):
        remote_module_appliance._call_mutation(req, 30.0, operation)  # noqa: SLF001
    release.set()
    executor.pool.shutdown(wait=True)
    assert conn.created_flags is None
    assert domain.destroyed is False


def test_console_is_redacted_before_bounding_and_invalid_utf8_is_safe() -> None:
    clock = Clock()
    chunks = [b"x" * 20_000 + b" split-", b"secret token=hunter2 \xff"]
    outcome = run_or_adopt_appliance(Conn(chunks, clock), request(clock))
    assert len(outcome.console_tail.encode()) <= 16_384
    assert "split-secret" not in outcome.console_tail
    assert "hunter2" not in outcome.console_tail
    assert outcome.console_tail == ""
    assert outcome.result is None


def test_console_retains_only_valid_stable_protocol_fields() -> None:
    clock = Clock()
    chunks = [
        (
            f"phase=installed\nerror_code=FILESYSTEM_FAILURE\n"  # pragma: allowlist secret
            f"system_id={UUID1}\nsource_manifest={MANIFEST}\n"
            "entry_count=200000\ncontent_bytes=8589934592\n"
            "unknown=value\npath=/etc/shadow\nlisting=private\n"
            "raw tool output\nentry_count=200001\nsource_manifest=bad\n"
        ).encode()
    ]
    outcome = run_or_adopt_appliance(Conn(chunks, clock), request(clock))
    assert outcome.console_tail == (
        f"phase=installed\nerror_code=FILESYSTEM_FAILURE\nsystem_id={UUID1}\n"
        f"source_manifest={MANIFEST}\nentry_count=200000\ncontent_bytes=8589934592\n"
    )


@pytest.mark.parametrize(
    "root_volume_key",
    ["root\\alias", "root//alias", "root/./alias", "é" * 128],
)
def test_console_rejects_noncanonical_root_volume_key(root_volume_key: str) -> None:
    clock = Clock()
    outcome = run_or_adopt_appliance(
        Conn([f"root_volume_key={root_volume_key}\n".encode()], clock),
        request(clock),
    )

    assert outcome.console_tail == ""


def test_console_discards_overlong_numeric_fields() -> None:
    clock = Clock()
    digits = "9" * 10_000
    console = f"entry_count={digits}\ncontent_bytes={digits}\n".encode()
    outcome = run_or_adopt_appliance(Conn([console], clock), request(clock))
    assert outcome.console_tail == ""


def test_scratch_and_inspection_each_receive_fresh_wait_deadline() -> None:
    clock = Clock()
    executor = DeadlineAwareExecutor(clock)
    result = RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=UUID1,
        run_id=UUID2,
        plan_identity=DIGEST,
        operation_nonce="1" * 32,
        appliance_image_digest=DIGEST,
        release="6.8.0-test",
        root_volume_key="root",
        root_volume_identity=DIGEST,
        source_manifest=MANIFEST,
        installed_manifest=MANIFEST,
        capture_absent=True,
        entry_count=0,
        content_bytes=0,
    ).to_wire_bytes()
    outcome = run_or_adopt_appliance(
        Conn([], clock), replace(request(clock, result=result), executor=executor)
    )
    assert outcome.result is not None
    assert executor.deadlines[-2:] == [31.0, 31.0]


def test_matching_domain_is_adopted_but_mismatch_fails_closed() -> None:
    clock = Clock()
    req = request(clock)
    xml = render_remote_module_appliance(req)
    normalized = xml.replace(
        '<domain type="kvm">',
        f'<domain type="kvm" id="7"><uuid>{UUID2}</uuid>',
    ).replace(
        '<source pool="pool" volume="root" />',
        '<driver name="qemu" type="raw" /><source pool="pool" volume="root" />',
    )
    conn = Conn([], clock, existing=Domain(normalized))
    run_or_adopt_appliance(conn, req)
    assert conn.created_flags is None
    bad = Domain(xml.replace(DIGEST, "sha256:" + "c" * 64, 1))
    with pytest.raises(CategorizedError):
        run_or_adopt_appliance(Conn([], Clock(), existing=bad), request(Clock()))


@pytest.mark.parametrize(
    "code",
    [libvirt.VIR_ERR_AUTH_FAILED, libvirt.VIR_ERR_SYSTEM_ERROR, libvirt.VIR_ERR_INTERNAL_ERROR],
)
def test_lookup_failure_other_than_absence_never_creates_domain(code: int) -> None:
    clock = Clock()
    conn = Conn([], clock, lookup_error=libvirt_error(code))
    with pytest.raises(libvirt.libvirtError):
        run_or_adopt_appliance(conn, request(clock))
    assert conn.created_flags is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('<domain type="kvm">', '<domain type="qemu">'),
        ('machine="q35"', 'machine="pc"'),
        (">hvm</type>", ">linux</type>"),
        ('<memory unit="KiB">262144</memory>', '<memory unit="MiB">256</memory>'),
        ("<vcpu>1</vcpu>", "<vcpu>2</vcpu>"),
    ],
)
def test_adoption_rejects_immutable_domain_shape_mismatch(old: str, new: str) -> None:
    clock = Clock()
    xml = render_remote_module_appliance(request(clock))
    assert old in xml
    with pytest.raises(CategorizedError):
        run_or_adopt_appliance(
            Conn([], clock, existing=Domain(xml.replace(old, new))), request(clock)
        )


@pytest.mark.parametrize(
    ("anchor", "extra"),
    [
        ("</os>", "<kernel>/boot/vmlinuz</kernel>"),
        ("</os>", "<initrd>/boot/initrd</initrd>"),
        ("</os>", "<loader>/firmware</loader>"),
        ("<os>", '<currentMemory unit="KiB">262144</currentMemory>'),
        ("<os>", '<cpu mode="host-passthrough" />'),
        ("<os>", "<iothreads>1</iothreads>"),
    ],
)
def test_adoption_rejects_extra_execution_or_resource_nodes(anchor: str, extra: str) -> None:
    clock = Clock()
    xml = render_remote_module_appliance(request(clock))
    xml = xml.replace(anchor, f"{extra}{anchor}")
    with pytest.raises(CategorizedError):
        run_or_adopt_appliance(Conn([], clock, existing=Domain(xml)), request(clock))


@pytest.mark.parametrize(
    ("active", "persistent"),
    [(False, False), (True, True)],
)
def test_adoption_requires_active_transient_domain(active: bool, persistent: bool) -> None:
    clock = Clock()
    domain = Domain(
        render_remote_module_appliance(request(clock)),
        active=active,
        persistent=persistent,
    )
    with pytest.raises(RuntimeError, match="active and transient"):
        run_or_adopt_appliance(Conn([], clock, existing=domain), request(clock))


@pytest.mark.parametrize("device", ["hostdev", "tpm", "rng", "video", "interface", "channel"])
def test_adoption_rejects_non_allowlisted_devices(device: str) -> None:
    clock = Clock()
    xml = render_remote_module_appliance(request(clock)).replace(
        "</devices>", f"<{device} /></devices>"
    )
    with pytest.raises(CategorizedError):
        run_or_adopt_appliance(Conn([], clock, existing=Domain(xml)), request(clock))


def test_adoption_rejects_unlisted_attributes_on_normalized_devices() -> None:
    clock = Clock()
    xml = render_remote_module_appliance(request(clock)).replace(
        "</devices>", '<controller type="pci" unexpected="value" /></devices>'
    )
    with pytest.raises(CategorizedError):
        run_or_adopt_appliance(Conn([], clock, existing=Domain(xml)), request(clock))


@pytest.mark.parametrize(
    "normalized",
    [
        "<emulator>/tmp/evil</emulator>",
        "<emulator>/usr/bin/qemu-system-x86_64</emulator>" * 2,
        '<controller type="pci" index="0" />' * 2,
        '<input type="mouse" bus="ps2" />' * 2,
        '<memballoon model="virtio" />' * 2,
    ],
)
def test_adoption_rejects_altered_or_duplicate_normalized_devices(normalized: str) -> None:
    clock = Clock()
    xml = render_remote_module_appliance(request(clock)).replace(
        "</devices>", normalized + "</devices>"
    )
    with pytest.raises(CategorizedError):
        run_or_adopt_appliance(Conn([], clock, existing=Domain(xml)), request(clock))


@pytest.mark.parametrize(
    "normalized",
    [
        '<controller type="usb" index="0" />',
        '<input type="mouse" bus="ps2" />',
        '<controller type="pci" index="0"><alias name="pci.0" />'
        '<alias name="pci.1" /></controller>',
    ],
)
def test_adoption_rejects_extra_unique_or_malformed_normalized_devices(
    normalized: str,
) -> None:
    clock = Clock()
    xml = render_remote_module_appliance(request(clock)).replace(
        "</devices>", '<controller type="pci" index="0" />' + normalized + "</devices>"
    )
    with pytest.raises(CategorizedError):
        run_or_adopt_appliance(Conn([], clock, existing=Domain(xml)), request(clock))


def test_adoption_uses_exact_configured_native_ppc64le_emulator() -> None:
    clock = Clock()
    req = replace(
        request(clock),
        architecture="ppc64le",
        emulator_path="/usr/libexec/qemu-kvm",
    )
    xml = render_remote_module_appliance(req)
    run_or_adopt_appliance(Conn([], clock, existing=Domain(xml)), req)

    altered = xml.replace("/usr/libexec/qemu-kvm", "/usr/bin/qemu-system-ppc64")
    with pytest.raises(CategorizedError):
        run_or_adopt_appliance(Conn([], clock, existing=Domain(altered)), req)


@pytest.mark.parametrize(
    ("architecture", "emulator", "normalized_devices"),
    [
        (
            "x86_64",
            "/usr/bin/qemu-system-x86_64",
            """
            <controller type="usb" index="0" model="qemu-xhci" ports="15">
              <alias name="usb"/><address type="pci" domain="0x0000" bus="0x02"
                slot="0x00" function="0x0"/>
            </controller>
            <controller type="pci" index="0" model="pcie-root">
              <alias name="pcie.0"/>
            </controller>
            <controller type="sata" index="0">
              <alias name="sata0"/><address type="pci" domain="0x0000" bus="0x00"
                slot="0x1f" function="0x2"/>
            </controller>
            <input type="mouse" bus="ps2"><alias name="input0"/></input>
            <input type="keyboard" bus="ps2"><alias name="input1"/></input>
            <memballoon model="virtio"><alias name="balloon0"/></memballoon>
            """,
        ),
        (
            "ppc64le",
            "/usr/libexec/qemu-kvm",
            """
            <controller type="usb" index="0" model="qemu-xhci" ports="15">
              <alias name="usb"/><address type="pci" domain="0x0000" bus="0x00"
                slot="0x01" function="0x0"/>
            </controller>
            <controller type="pci" index="0" model="pci-root">
              <alias name="pci.0"/>
            </controller>
            <input type="mouse" bus="usb"><alias name="input0"/></input>
            <input type="keyboard" bus="usb"><alias name="input1"/></input>
            <memballoon model="virtio"><alias name="balloon0"/></memballoon>
            """,
        ),
    ],
)
def test_adoption_accepts_exact_architecture_normalized_device_multiset(
    architecture: str, emulator: str, normalized_devices: str
) -> None:
    clock = Clock()
    req = replace(request(clock), architecture=architecture, emulator_path=emulator)
    xml = render_remote_module_appliance(req)
    root = ET.fromstring(xml)
    devices = root.find("devices")
    assert devices is not None
    for tag in ("controller", "input", "memballoon", "emulator"):
        for device in devices.findall(tag):
            devices.remove(device)
    devices.extend(ET.fromstring(f"<devices>{normalized_devices}</devices>"))
    ET.SubElement(devices, "emulator").text = emulator

    run_or_adopt_appliance(
        Conn([], clock, existing=Domain(ET.tostring(root, encoding="unicode"))), req
    )


@pytest.mark.parametrize("architecture", ["x86_64", "ppc64le"])
def test_adoption_rejects_extra_normalized_device_for_each_architecture(
    architecture: str,
) -> None:
    clock = Clock()
    emulator = (
        "/usr/bin/qemu-system-x86_64" if architecture == "x86_64" else "/usr/libexec/qemu-kvm"
    )
    req = replace(request(clock), architecture=architecture, emulator_path=emulator)
    xml = render_remote_module_appliance(req).replace(
        "</devices>", '<input type="tablet" bus="usb" /></devices>'
    )
    with pytest.raises(CategorizedError):
        run_or_adopt_appliance(Conn([], clock, existing=Domain(xml)), req)


@pytest.mark.parametrize(
    "console",
    [
        '<console type="tcp" />',
        '<console type="pty" /><console type="pty" />',
        '<console type="pty"><protocol type="raw" /></console>',
    ],
)
def test_adoption_requires_one_allowlisted_pty_console(console: str) -> None:
    clock = Clock()
    xml = render_remote_module_appliance(request(clock))
    xml = xml.replace('<console type="pty" />', console)
    with pytest.raises(CategorizedError):
        run_or_adopt_appliance(Conn([], clock, existing=Domain(xml)), request(clock))


def test_wait_timeout_aborts_stream_and_attempts_bounded_teardown() -> None:
    clock = Clock()
    conn = Conn([-2] * 30, clock)
    forged_result = RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=UUID1,
        run_id=UUID2,
        plan_identity=DIGEST,
        operation_nonce="1" * 32,
        appliance_image_digest=DIGEST,
        release="6.8.0-test",
        root_volume_key="root",
        root_volume_identity=DIGEST,
        source_manifest=MANIFEST,
        installed_manifest=MANIFEST,
        capture_absent=True,
        entry_count=0,
        content_bytes=0,
    ).to_wire_bytes()
    outcome = run_or_adopt_appliance(conn, request(clock, result=forged_result))
    assert outcome.result is None
    assert outcome.timed_out
    assert conn.stream is not None and conn.stream.aborted
    assert conn.domain is not None and conn.domain.destroyed


def test_foreign_same_name_domain_preserves_conflict_category() -> None:
    clock = Clock()
    req = request(clock)
    foreign = render_remote_module_appliance(req).replace(
        f'image-digest="{DIGEST}"',
        f'image-digest="sha256:{"f" * 64}"',
    )

    with pytest.raises(CategorizedError) as raised:
        run_or_adopt_appliance(Conn([], clock, existing=Domain(foreign)), req)

    assert raised.value.category is ErrorCategory.CONFLICT


def test_console_timeout_uses_fresh_bounded_deadline_for_abort() -> None:
    clock = Clock()
    executor = DeadlineAwareExecutor(clock)
    conn = Conn([-2] * 30, clock)
    outcome = run_or_adopt_appliance(conn, replace(request(clock), executor=executor))
    assert outcome.timed_out
    assert conn.stream is not None and conn.stream.aborted
    assert executor.abort_deadline == 60.0
    assert executor.abort_deadline is not None
    assert executor.abort_deadline <= INVOCATION_TIMEOUT_SECONDS
    assert conn.domain is not None and conn.domain.destroyed


def test_would_block_does_not_end_console_before_later_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    conn = Conn([-2, -2, b"phase=accepted\n", b""], clock)
    delays: list[float] = []
    monkeypatch.setattr(remote_module_appliance.time, "sleep", delays.append)
    outcome = run_or_adopt_appliance(conn, request(clock))
    assert outcome.console_tail == "phase=accepted\n"
    assert not outcome.timed_out
    assert delays == [0.01, 0.01]


def test_would_block_does_not_rearm_idle_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    conn = Conn([-2] * 30, clock)
    monkeypatch.setattr(remote_module_appliance.time, "sleep", lambda _delay: None)

    outcome = run_or_adopt_appliance(conn, request(clock))

    assert outcome.timed_out
    assert clock.value == 30
    assert conn.domain is not None and conn.domain.destroyed


def test_synchronous_open_console_failure_releases_stream() -> None:
    clock = Clock()
    req = request(clock)
    conn = Conn(
        [],
        clock,
        existing=FailingOpenDomain(render_remote_module_appliance(req), libvirt_error(1)),
    )

    with pytest.raises(libvirt.libvirtError):
        run_or_adopt_appliance(conn, req)

    assert conn.stream is not None and conn.stream.aborted


def test_unresolved_open_console_does_not_race_with_abort() -> None:
    clock = Clock()
    req = request(clock)
    conn = Conn(
        [],
        clock,
        existing=FailingOpenDomain(
            render_remote_module_appliance(req),
            UnresolvedCallError("open console unresolved"),
        ),
    )

    outcome = run_or_adopt_appliance(conn, req)

    assert outcome.timed_out
    assert conn.stream is not None and not conn.stream.aborted


def test_console_progress_rearms_idle_deadline_but_not_invocation_cap() -> None:
    clock = Clock()

    class ProgressStream(Stream):
        def recv(self, size: int) -> bytes | int | None:
            self.clock.value += 20
            return self.chunks.pop(0) if self.chunks else b""

    class ProgressConn(Conn):
        def newStream(self, flags: int = 0) -> Stream:  # noqa: N802
            self.stream = ProgressStream(self.chunks, self.clock)
            return self.stream

    outcome = run_or_adopt_appliance(
        ProgressConn([b"phase=accepted\n", b"phase=captured\n", b""], clock),
        request(clock),
    )

    assert not outcome.timed_out
    assert clock.value == 60
    assert outcome.console_tail == "phase=accepted\nphase=captured\n"


def test_success_is_not_promoted_while_appliance_remains_present() -> None:
    clock = Clock()
    valid_result = RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=UUID1,
        run_id=UUID2,
        plan_identity=DIGEST,
        operation_nonce="1" * 32,
        appliance_image_digest=DIGEST,
        release="6.8.0-test",
        root_volume_key="root",
        root_volume_identity=DIGEST,
        source_manifest=MANIFEST,
        installed_manifest=MANIFEST,
        capture_absent=True,
        entry_count=0,
        content_bytes=0,
    ).to_wire_bytes()
    req = replace(
        request(clock, result=valid_result),
        inspect_attachments=lambda: AttachmentInspection(True, True, True, frozenset()),
    )
    assert run_or_adopt_appliance(Conn([], clock), req).result is None


class TimingOutExecutor:
    def call[T](self, operation: Callable[[], T], deadline: float) -> T:
        raise TimeoutError


def test_blocking_libvirt_call_is_held_behind_deadline_executor() -> None:
    clock = Clock()
    req = replace(request(clock), executor=TimingOutExecutor())
    outcome = run_or_adopt_appliance(Conn([], clock), req)
    assert outcome.timed_out and outcome.result is None


class ThreadDeadlineExecutor:
    def call[T](self, operation: Callable[[], T], deadline: float) -> T:
        done = threading.Event()
        result: list[T] = []
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(operation())
            except BaseException as exc:  # test executor must transport worker failures
                errors.append(exc)
            finally:
                done.set()

        threading.Thread(target=invoke, daemon=True).start()
        if not done.wait(0.01):
            raise UnresolvedCallError
        if errors:
            raise errors[0]
        return result[0]


class BlockingConn(Conn):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__([], clock)
        self.release = release

    def lookupByName(self, name: str) -> Domain:  # noqa: N802
        self.release.wait()
        return super().lookupByName(name)


class BlockingStream(Stream):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__([], clock)
        self.release = release

    def recv(self, size: int) -> bytes | int | None:
        self.release.wait()
        return b""


class BlockingConsoleConn(Conn):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__([], clock)
        self.blocking_stream = BlockingStream(release, clock)

    def newStream(self, flags: int = 0) -> BlockingStream:  # noqa: N802
        self.stream = self.blocking_stream
        return self.blocking_stream


class BlockingAbortStream(Stream):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__([-2] * 30, clock)
        self.release = release

    def abort(self) -> int:
        self.release.wait()
        return super().abort()


class BlockingAbortConn(Conn):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__([], clock)
        self.blocking_stream = BlockingAbortStream(release, clock)

    def newStream(self, flags: int = 0) -> BlockingAbortStream:  # noqa: N802
        self.stream = self.blocking_stream
        return self.blocking_stream


class CompletedBlockingAbortStream(BlockingAbortStream):
    def __init__(self, release: threading.Event, clock: Clock) -> None:
        super().__init__(release, clock)
        self.chunks = []


class CompletedBlockingAbortConn(Conn):
    def __init__(
        self,
        release: threading.Event,
        clock: Clock,
        *,
        existing: Domain | None = None,
    ) -> None:
        super().__init__([], clock, existing=existing)
        self.blocking_stream = CompletedBlockingAbortStream(release, clock)

    def newStream(self, flags: int = 0) -> CompletedBlockingAbortStream:  # noqa: N802
        self.stream = self.blocking_stream
        return self.blocking_stream


def test_genuinely_blocking_libvirt_call_times_out_without_waiting_for_rpc() -> None:
    clock = Clock()
    release = threading.Event()
    req = replace(request(clock), executor=ThreadDeadlineExecutor())
    try:
        outcome = run_or_adopt_appliance(BlockingConn(release, clock), req)
        assert outcome.timed_out and outcome.result is None
    finally:
        release.set()


def test_genuinely_blocking_console_recv_times_out_and_preserves_recovery() -> None:
    clock = Clock()
    release = threading.Event()
    scratch_reads = 0
    valid_result = RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=UUID1,
        run_id=UUID2,
        plan_identity=DIGEST,
        operation_nonce="1" * 32,
        appliance_image_digest=DIGEST,
        release="6.8.0-test",
        root_volume_key="root",
        root_volume_identity=DIGEST,
        source_manifest=MANIFEST,
        installed_manifest=MANIFEST,
        capture_absent=True,
        entry_count=0,
        content_bytes=0,
    ).to_wire_bytes()

    def read_scratch() -> bytes:
        nonlocal scratch_reads
        scratch_reads += 1
        return valid_result

    conn = BlockingConsoleConn(release, clock)
    req = replace(
        request(clock, result=valid_result),
        read_scratch_result=read_scratch,
        executor=ThreadDeadlineExecutor(),
    )
    try:
        outcome = run_or_adopt_appliance(conn, req)
        assert outcome.timed_out and outcome.result is None
        assert not conn.blocking_stream.aborted
        assert scratch_reads == 0
        assert conn.domain is not None and not conn.domain.destroyed
    finally:
        release.set()


def test_genuinely_blocking_console_abort_preserves_appliance() -> None:
    clock = Clock()
    release = threading.Event()
    conn = BlockingAbortConn(release, clock)
    try:
        outcome = run_or_adopt_appliance(
            conn,
            replace(request(clock), executor=ThreadDeadlineExecutor()),
        )
        assert outcome.timed_out and outcome.result is None
        assert conn.domain is not None and not conn.domain.destroyed
    finally:
        release.set()


def test_synchronous_console_failure_with_unresolved_abort_suppresses_teardown() -> None:
    clock = Clock()
    release = threading.Event()
    req = request(clock)
    domain = FailingOpenDomain(render_remote_module_appliance(req), RuntimeError("open failed"))
    conn = CompletedBlockingAbortConn(release, clock, existing=domain)
    try:
        outcome = run_or_adopt_appliance(
            conn,
            replace(req, executor=ThreadDeadlineExecutor()),
        )
        assert outcome.timed_out and outcome.result is None
        assert not domain.destroyed
    finally:
        release.set()


def test_completed_console_with_unresolved_abort_skips_scratch_and_teardown() -> None:
    clock = Clock()
    release = threading.Event()
    scratch_reads = 0

    def malformed_scratch() -> bytes:
        nonlocal scratch_reads
        scratch_reads += 1
        return b"not-json"

    conn = CompletedBlockingAbortConn(release, clock)
    try:
        outcome = run_or_adopt_appliance(
            conn,
            replace(
                request(clock),
                executor=ThreadDeadlineExecutor(),
                read_scratch_result=malformed_scratch,
            ),
        )
        assert outcome.timed_out and outcome.result is None
        assert scratch_reads == 0
        assert conn.domain is not None and not conn.domain.destroyed
    finally:
        release.set()


@pytest.mark.parametrize("stage", ["scratch", "inspection"])
def test_genuinely_blocking_evidence_call_never_promotes_success(stage: str) -> None:
    clock = Clock()
    release = threading.Event()
    valid_result = RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=UUID1,
        run_id=UUID2,
        plan_identity=DIGEST,
        operation_nonce="1" * 32,
        appliance_image_digest=DIGEST,
        release="6.8.0-test",
        root_volume_key="root",
        root_volume_identity=DIGEST,
        source_manifest=MANIFEST,
        installed_manifest=MANIFEST,
        capture_absent=True,
        entry_count=0,
        content_bytes=0,
    ).to_wire_bytes()

    def blocking_result() -> bytes:
        release.wait()
        return valid_result

    def blocking_inspection() -> AttachmentInspection:
        release.wait()
        return AttachmentInspection(
            True, True, False, frozenset({("pool", "source"), ("pool", "scratch")})
        )

    req = replace(
        request(clock, result=valid_result),
        read_scratch_result=blocking_result if stage == "scratch" else lambda: valid_result,
        inspect_attachments=(
            blocking_inspection if stage == "inspection" else request(clock).inspect_attachments
        ),
        executor=ThreadDeadlineExecutor(),
    )
    try:
        conn = Conn([], clock)
        outcome = run_or_adopt_appliance(conn, req)
        assert outcome.timed_out and outcome.result is None
        assert conn.domain is not None and not conn.domain.destroyed
    finally:
        release.set()


def test_resolved_result_deadline_attempts_teardown() -> None:
    clock = Clock()

    class ExpiredResultExecutor(Executor):
        def call[T](self, operation: Callable[[], T], deadline: float) -> T:
            if getattr(operation, "__name__", None) == "<lambda>" and clock.value == 0:
                return super().call(operation, deadline)
            if getattr(operation, "__name__", None) == "read_result":
                raise TimeoutError
            return super().call(operation, deadline)

    def read_result() -> bytes | None:
        return None

    conn = Conn([], clock)
    req = replace(request(clock), read_scratch_result=read_result, executor=ExpiredResultExecutor())
    outcome = run_or_adopt_appliance(conn, req)
    assert outcome.timed_out and outcome.result is None
    assert conn.domain is not None and conn.domain.destroyed


def test_malformed_result_attempts_teardown_before_preserving_failure() -> None:
    clock = Clock()
    conn = Conn([], clock)
    with pytest.raises(ValueError):
        run_or_adopt_appliance(conn, request(clock, result=b"not-json"))
    assert conn.domain is not None and conn.domain.destroyed


@pytest.mark.parametrize(
    "malformed",
    [success_result().removesuffix(b"\n"), success_result() + b"\n"],
)
def test_result_requires_exact_single_newline_frame(malformed: bytes) -> None:
    clock = Clock()
    conn = Conn([], clock)
    with pytest.raises(ValueError, match="newline-framed"):
        run_or_adopt_appliance(conn, request(clock, result=malformed))
    assert conn.domain is not None and conn.domain.destroyed


def test_teardown_inspection_failure_does_not_mask_malformed_result() -> None:
    clock = Clock()
    conn = Conn([], clock)

    def failed_inspection() -> AttachmentInspection:
        raise RuntimeError("cleanup failed")

    req = replace(
        request(clock, result=b"not-json"),
        inspect_attachments=failed_inspection,
    )
    with pytest.raises(ValueError):
        run_or_adopt_appliance(conn, req)
    assert conn.domain is not None and conn.domain.destroyed


@pytest.mark.parametrize("stage", ["console", "scratch", "inspection"])
def test_resolved_supervision_exception_attempts_teardown(stage: str) -> None:
    clock = Clock()
    conn = Conn([1] if stage == "console" else [], clock)

    def failed_result() -> bytes | None:
        raise LookupError("scratch failed")

    def failed_inspection() -> AttachmentInspection:
        raise LookupError("inspection failed")

    req = replace(
        request(clock, result=success_result()),
        read_scratch_result=(failed_result if stage == "scratch" else lambda: success_result()),
        inspect_attachments=(
            failed_inspection if stage == "inspection" else request(clock).inspect_attachments
        ),
    )
    expected = RuntimeError if stage == "console" else LookupError
    with pytest.raises(expected):
        run_or_adopt_appliance(conn, req)
    assert conn.domain is not None and conn.domain.destroyed


def test_resolved_destroy_failure_still_inspects_and_preserves_primary_failure() -> None:
    clock = Clock()
    inspections = 0

    def inspect() -> AttachmentInspection:
        nonlocal inspections
        inspections += 1
        return request(clock).inspect_attachments()

    conn = FailingDestroyConn([1], clock)
    with pytest.raises(RuntimeError, match="invalid appliance console"):
        run_or_adopt_appliance(conn, replace(request(clock), inspect_attachments=inspect))
    assert inspections == 1


class InspectionDomain:
    def __init__(self, xml: str, active: bool) -> None:
        self.xml = xml
        self.active = active

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        return self.xml

    def isActive(self) -> int:  # noqa: N802
        return int(self.active)

    def isPersistent(self) -> int:  # noqa: N802
        return 0


class InspectionConn:
    def __init__(self, domains: list[InspectionDomain]) -> None:
        self.domains = domains

    def listAllDomains(self, flags: int = 0) -> list[InspectionDomain]:  # noqa: N802
        return self.domains

    def storagePoolLookupByName(self, name: str) -> InspectionPool:  # noqa: N802
        assert name == "pool"
        return InspectionPool()


class InspectionVolume:
    def __init__(self, name: str) -> None:
        self.name = name

    def path(self) -> str:
        return f"/pool/{self.name}"


class InspectionPool:
    def storageVolLookupByName(self, name: str) -> InspectionVolume:  # noqa: N802
        return InspectionVolume(name)


class InspectionIdentity:
    def identity(self, path: str) -> RemoteDeviceIdentity | None:
        names = {"root": 1, "source": 2, "scratch": 3, "appliance-x86_64.qcow2": 4}
        return RemoteDeviceIdentity("inode", 1, names[path.rsplit("/", 1)[-1]])


def test_rendered_appliance_passes_attachment_inspector() -> None:
    req = request(Clock())
    system_xml = (
        "<domain><name>system</name><metadata><kdive:system "
        'xmlns:kdive="https://kdive.dev/libvirt/1">'
        f'{UUID1}</kdive:system></metadata><devices><disk><source pool="pool" '
        'volume="root" /></disk></devices></domain>'
    )
    expected = ExpectedAttachmentState(
        UUID1,
        "pool",
        "root",
        "source",
        "scratch",
        ExpectedAppliance(
            req.name,
            "x86_64",
            DIGEST,
            "1" * 32,
            req.appliance_volume,
            "q35",
            req.memory_kib,
            req.vcpus,
            req.emulator_path,
        ),
    )
    inspection = inspect_module_attachments(
        InspectionConn(
            [
                InspectionDomain(system_xml, False),
                InspectionDomain(render_remote_module_appliance(req), True),
            ]
        ),
        InspectionIdentity(),
        expected,
    )
    assert inspection.appliance_present


def test_teardown_requires_absence_and_all_detach_proofs() -> None:
    expected = ExpectedAttachmentState(
        UUID1,
        "pool",
        "root",
        "source",
        "scratch",
        ExpectedAppliance("appliance", "x86_64", DIGEST, "1" * 32),
    )
    proof = AttachmentInspection(
        True, True, False, frozenset({("pool", "source"), ("pool", "scratch")})
    )
    observed = observe_appliance_absent_and_detached(expected, lambda: proof)
    assert observed.absent and observed.source_detached and observed.scratch_detached
    unresolved = replace(proof, appliance_present=True)
    assert not observe_appliance_absent_and_detached(expected, lambda: unresolved).complete
