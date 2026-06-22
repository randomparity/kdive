"""Tests for the local-libvirt Provisioning plane (ADR-0025)."""

from __future__ import annotations

import copy
import importlib
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import libvirt
import pytest
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle import provisioning as provisioning_module
from kdive.providers.local_libvirt.lifecycle import storage as storage_module
from kdive.providers.local_libvirt.lifecycle import xml as xml_module
from kdive.providers.local_libvirt.lifecycle.materialize import (
    RootfsMaterializationContext,
)
from kdive.providers.local_libvirt.lifecycle.provisioning import (
    LocalLibvirtProvisioning,
    ProvisioningFiles,
    console_log_path,
    domain_name_for,
    overlay_path,
    render_domain_xml,
)
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.shared import libvirt_xml as libvirt_xml_contract
from kdive.providers.shared.libvirt_xml import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    parse_metadata_system_id,
    recorded_gdb_port,
)
from tests.providers.local_libvirt.fakes import libvirt_error

_SYS = UUID("11111111-1111-1111-1111-111111111111")
_DISK = "/var/lib/kdive/rootfs/fedora-40.qcow2"

_VALID: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "pc-q35-9.0"},
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


def _profile(**overrides: Any) -> ProvisioningProfile:
    data = copy.deepcopy(_VALID)
    data["provider"]["local-libvirt"].update(overrides)
    return ProvisioningProfile.parse(data)


def _render(
    system_id: UUID = _SYS,
    profile: ProvisioningProfile | None = None,
    *,
    disk_path: str = _DISK,
) -> str:
    return render_domain_xml(system_id, profile or _profile(), disk_path=disk_path)


def test_domain_name_is_kdive_prefixed() -> None:
    assert domain_name_for(_SYS) == "kdive-11111111-1111-1111-1111-111111111111"


def test_import_does_not_register_elementtree_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(libvirt_xml_contract, "_kdive_namespace_registered", False)

    def fake_register_namespace(prefix: str, uri: str) -> None:
        calls.append((prefix, uri))

    monkeypatch.setattr(libvirt_xml_contract, "_qemu_namespace_registered", False)
    monkeypatch.setattr(ET, "register_namespace", fake_register_namespace)
    reloaded = importlib.reload(provisioning_module)

    assert calls == []

    reloaded.render_domain_xml(_SYS, _profile(), disk_path=_DISK)
    reloaded.render_domain_xml(_SYS, _profile(), disk_path=_DISK)

    # Rendering registers both the kdive metadata prefix and the qemu passthrough prefix
    # (the latter is needed for the gdbstub <qemu:commandline>), each once.
    assert calls == [("kdive", KDIVE_METADATA_NS), ("qemu", QEMU_NS)]


def test_render_carries_name_memory_vcpu_machine_and_rootfs() -> None:
    root = _safe_fromstring(_render())
    assert root.findtext("name") == "kdive-11111111-1111-1111-1111-111111111111"
    assert root.findtext("memory") == "4096"
    assert root.findtext("vcpu") == "4"
    os_type = root.find("os/type")
    assert os_type is not None
    assert os_type.get("arch") == "x86_64"
    assert os_type.get("machine") == "pc-q35-9.0"
    source = root.find("devices/disk/source")
    assert source is not None
    assert source.get("file") == "/var/lib/kdive/rootfs/fedora-40.qcow2"


def test_render_returns_a_unicode_string() -> None:
    # encoding="unicode" yields a str; a byte-string would break the defineXML seam that expects
    # text and the test parsers that call _safe_fromstring on a str.
    assert isinstance(_render(), str)


def test_render_root_is_a_kvm_domain() -> None:
    # The root must be <domain type="kvm">; a wrong tag or hypervisor type makes libvirt reject
    # the XML or run the guest under the wrong driver.
    root = _safe_fromstring(_render())
    assert root.tag == "domain"
    assert root.get("type") == "kvm"


def test_render_memory_uses_mib_unit() -> None:
    # libvirt reads <memory unit="MiB"> case-sensitively; a wrong-case unit makes it fall back to
    # KiB and the guest boots with ~1024x too little RAM.
    root = _safe_fromstring(_render())
    memory = root.find("memory")
    assert memory is not None
    assert memory.get("unit") == "MiB"
    assert memory.text == "4096"


def test_render_os_type_is_hvm() -> None:
    root = _safe_fromstring(_render())
    os_type = root.find("os/type")
    assert os_type is not None
    assert os_type.text == "hvm"


def test_render_disk_is_file_backed_disk_device() -> None:
    root = _safe_fromstring(_render())
    disk = root.find("devices/disk")
    assert disk is not None
    assert disk.get("type") == "file"
    assert disk.get("device") == "disk"


def test_render_disk_target_uses_virtio_bus() -> None:
    root = _safe_fromstring(_render())
    target = root.find("devices/disk/target")
    assert target is not None
    assert target.get("dev") == "vda"
    assert target.get("bus") == "virtio"


def test_render_serial_target_is_port_zero() -> None:
    root = _safe_fromstring(_render())
    target = root.find("devices/serial/target")
    assert target is not None
    assert target.get("port") == "0"


def test_render_declares_qcow2_disk_driver() -> None:
    # The rootfs images are qcow2; a driver-less disk makes libvirt default to raw, so the guest
    # reads the qcow2 header instead of the ext4 filesystem and panics unable to mount root.
    root = _safe_fromstring(_render())
    driver = root.find("devices/disk/driver")
    assert driver is not None
    assert driver.get("name") == "qemu"
    assert driver.get("type") == "qcow2"


def test_render_emits_deterministic_uuid_for_idempotent_redefine() -> None:
    # defineXML redefines an existing domain only when the XML carries its uuid; a deterministic
    # uuid = system_id lets a provision retry redefine the running domain in place instead of
    # failing with "domain already exists with uuid ..." on the name collision.
    root = _safe_fromstring(_render())
    assert root.findtext("uuid") == str(_SYS)


def test_required_cmdline_root_matches_the_rendered_disk_target() -> None:
    # ADR-0061: the platform-injected root= must name the device provisioning attaches. These are
    # set independently in two modules; this guards them moving together.
    from kdive.domain.capture import CaptureMethod
    from kdive.services.runs.steps import system_required_cmdline

    target = _safe_fromstring(_render()).find("devices/disk/target")
    assert target is not None
    # local-libvirt's runtime injects this root device (platform_root_cmdline default).
    assert f"root=/dev/{target.get('dev')}" in system_required_cmdline(
        CaptureMethod.CONSOLE, "root=/dev/vda"
    )


def test_render_uses_disk_path_override_when_given() -> None:
    # provision() attaches a per-System overlay, not the shared base, by passing disk_path.
    root = _safe_fromstring(_render(disk_path="/var/lib/kdive/rootfs/ov.qcow2"))
    source = root.find("devices/disk/source")
    assert source is not None and source.get("file") == "/var/lib/kdive/rootfs/ov.qcow2"


def test_render_has_no_kernel_or_cmdline() -> None:
    # The kdump crashkernel reservation is the install/boot plane's job (#17), not provision's.
    root = _safe_fromstring(_render())
    assert root.find("os/kernel") is None
    assert root.find("os/cmdline") is None


def test_xml_module_render_domain_xml_exposes_kdive_metadata() -> None:
    root = _safe_fromstring(xml_module.render_domain_xml(_SYS, _profile(), disk_path=_DISK))
    tag = root.find(f"metadata/{{{KDIVE_METADATA_NS}}}system")

    assert tag is not None
    assert tag.text == str(_SYS)


def test_render_metadata_tag_round_trips_through_discovery() -> None:
    root = _safe_fromstring(_render())
    tag = root.find(f"metadata/{{{KDIVE_METADATA_NS}}}system")
    assert tag is not None
    assert parse_metadata_system_id(ET.tostring(tag, encoding="unicode")) == str(_SYS)


def test_render_defaults_machine_when_absent() -> None:
    root = _safe_fromstring(_render(profile=_profile(domain_xml_params={})))
    os_type = root.find("os/type")
    assert os_type is not None and os_type.get("machine") == "q35"


def test_render_emits_loopback_gdbstub_when_flag_set() -> None:
    xml = render_domain_xml(_SYS, _profile(debug={"gdbstub": True}), disk_path=_DISK, gdb_port=4444)
    # The recorded port round-trips through the shared parser, on loopback.
    assert recorded_gdb_port(xml) == 4444
    root = _safe_fromstring(xml)
    args = [
        arg.get("value") for arg in root.findall(f"./{{{QEMU_NS}}}commandline/{{{QEMU_NS}}}arg")
    ]
    assert args == ["-gdb", "tcp:127.0.0.1:4444"]


def test_render_omits_gdbstub_when_flag_unset() -> None:
    xml = _render()  # default profile has debug.gdbstub False
    assert recorded_gdb_port(xml) is None
    root = _safe_fromstring(xml)
    assert root.find(f"./{{{QEMU_NS}}}commandline") is None


def test_render_ignores_gdb_port_when_flag_unset() -> None:
    # A stray port with the flag off renders nothing — the flag is the gate, not the port.
    xml = render_domain_xml(_SYS, _profile(), disk_path=_DISK, gdb_port=4444)
    assert recorded_gdb_port(xml) is None


def test_render_rejects_gdbstub_flag_without_a_port() -> None:
    with pytest.raises(CategorizedError) as caught:
        render_domain_xml(_SYS, _profile(debug={"gdbstub": True}), disk_path=_DISK, gdb_port=None)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_profile_rejects_unknown_domain_xml_param() -> None:
    with pytest.raises(CategorizedError) as caught:
        LocalLibvirtProfilePolicy().validate_profile(
            _profile(domain_xml_params={"machine": "q35", "bogus": "x"})
        )
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_render_rejects_unknown_domain_xml_param() -> None:
    # render re-checks at the worker boundary (a hand-built jsonb that bypassed the tool).
    with pytest.raises(CategorizedError):
        _render(profile=_profile(domain_xml_params={"nope": "x"}))


@dataclass
class _ProvDomain:
    domain_name: str
    created: bool = False
    destroyed: bool = False
    undefined: bool = False
    create_error: int | None = None
    destroy_error: int | None = None
    undefine_error: int | None = None
    xml_desc: str | None = None  # XMLDesc() result; gdbstub port reuse reads it back

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - mirrors the libvirt binding name
        return (
            self.xml_desc
            if self.xml_desc is not None
            else f"<domain><name>{self.domain_name}</name></domain>"
        )

    def create(self) -> int:
        if self.create_error is not None:
            raise libvirt_error(self.create_error)
        self.created = True
        return 0

    def destroy(self) -> int:
        if self.destroy_error is not None:
            raise libvirt_error(self.destroy_error)
        self.destroyed = True
        return 0

    def undefine(self) -> int:
        if self.undefine_error is not None:
            raise libvirt_error(self.undefine_error)
        self.undefined = True
        return 0


@dataclass
class _ProvConn:
    defined: dict[str, _ProvDomain] = field(default_factory=dict)
    define_error: int | None = None
    lookup_error: int | None = None  # raised by lookupByName (e.g. NO_DOMAIN)
    closed: int = 0
    recorded_xml: list[str] = field(default_factory=list)  # each defineXML payload, in order

    def defineXML(self, xml: str) -> _ProvDomain:
        if self.define_error is not None:
            raise libvirt_error(self.define_error)
        self.recorded_xml.append(xml)
        name = _safe_fromstring(xml).findtext("name")
        assert name is not None
        return self.defined.setdefault(name, _ProvDomain(name))

    def lookupByName(self, name: str) -> _ProvDomain:
        if self.lookup_error is not None:
            raise libvirt_error(self.lookup_error)
        if name not in self.defined:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN)
        return self.defined[name]

    def close(self) -> int:
        self.closed += 1
        return 0


def _prov(
    conn: _ProvConn,
    *,
    make_overlay: Callable[[str, str], None] = lambda _base, _overlay: None,
    remove_overlay: Callable[[str], None] = lambda _overlay: None,
    overlay_exists: Callable[[str], bool] = lambda _overlay: False,
) -> LocalLibvirtProvisioning:
    # The overlay seams default to no-ops so the libvirt-only tests never spawn qemu-img; the
    # console-log seam is also a no-op so they never depend on host /var/lib/kdive permissions.
    # The default "overlay absent" makes provision create one, matching a fresh provision.
    return LocalLibvirtProvisioning(
        connect=lambda: conn,
        files=ProvisioningFiles(
            make_overlay=make_overlay,
            remove_overlay=remove_overlay,
            overlay_exists=overlay_exists,
            prepare_console_log=lambda _path: None,
        ),
        materialize_rootfs=lambda rootfs, _system_id: (
            rootfs.path if rootfs.kind == "local" else "/var/lib/kdive/rootfs/upload.qcow2"
        ),
    )


def test_prov_helper_does_not_prepare_host_console_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_mkdir(self: object, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise AssertionError("unit helper must not touch host console log paths")

    monkeypatch.setattr(provisioning_module.Path, "mkdir", fail_mkdir)

    _prov(_ProvConn()).provision(_SYS, _profile())


def test_provision_defines_and_starts_returns_name() -> None:
    conn = _ProvConn()
    name = _prov(conn).provision(_SYS, _profile())
    assert name == "kdive-11111111-1111-1111-1111-111111111111"
    assert conn.defined[name].created is True
    assert conn.closed == 1  # the connection is closed after use (no leak)


def test_provision_define_error_is_provisioning_failure() -> None:
    conn = _ProvConn(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).provision(_SYS, _profile())
    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_provision_create_error_is_provisioning_failure() -> None:
    name = domain_name_for(_SYS)
    conn = _ProvConn(defined={name: _ProvDomain(name, create_error=libvirt.VIR_ERR_INTERNAL_ERROR)})
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).provision(_SYS, _profile())
    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_provision_real_create_failure_undefines_domain() -> None:
    # A real start failure (not "already running") must undefine the domain `defineXML` just
    # registered, so provision is transactional — no defined-but-unstarted domain is leaked.
    name = domain_name_for(_SYS)
    dom = _ProvDomain(name, create_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    conn = _ProvConn(defined={name: dom})
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).provision(_SYS, _profile())
    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert dom.undefined is True  # the defined-but-unstarted domain was cleaned up
    assert conn.closed == 1


def test_provision_already_running_domain_does_not_undefine() -> None:
    # "already running" (OPERATION_INVALID) is the desired post-state — the live domain must
    # NOT be undefined.
    name = domain_name_for(_SYS)
    dom = _ProvDomain(name, create_error=libvirt.VIR_ERR_OPERATION_INVALID)
    conn = _ProvConn(defined={name: dom})
    _prov(conn).provision(_SYS, _profile())
    assert dom.undefined is False  # kept the running domain


# --- gdbstub port allocation (ADR-0210 §1) -------------------------------------------------


def _gdb_profile() -> ProvisioningProfile:
    return _profile(debug={"gdbstub": True})


def _prov_with_port(conn: _ProvConn, *, free_port: Callable[[], int]) -> LocalLibvirtProvisioning:
    return LocalLibvirtProvisioning(
        connect=lambda: conn,
        files=ProvisioningFiles(
            make_overlay=lambda _base, _overlay: None,
            remove_overlay=lambda _overlay: None,
            overlay_exists=lambda _overlay: False,
            prepare_console_log=lambda _path: None,
        ),
        materialize_rootfs=lambda rootfs, _system_id: rootfs.path,
        free_port=free_port,
    )


def test_provision_gdbstub_allocates_a_fresh_port_when_no_prior_domain() -> None:
    conn = _ProvConn()  # lookupByName raises NO_DOMAIN (empty `defined`)
    _prov_with_port(conn, free_port=lambda: 5555).provision(_SYS, _gdb_profile())
    # The fresh port was recorded into the defined domain XML on loopback.
    assert recorded_gdb_port(conn.recorded_xml[-1]) == 5555


def test_provision_gdbstub_reuses_the_recorded_port_on_retry() -> None:
    name = domain_name_for(_SYS)
    recorded = render_domain_xml(_SYS, _gdb_profile(), disk_path=_DISK, gdb_port=6666)
    conn = _ProvConn(defined={name: _ProvDomain(name, xml_desc=recorded)})

    def fail_free_port() -> int:
        raise AssertionError("must reuse the recorded port, not allocate a fresh one")

    _prov_with_port(conn, free_port=fail_free_port).provision(_SYS, _gdb_profile())
    assert recorded_gdb_port(conn.recorded_xml[-1]) == 6666


def test_provision_non_gdbstub_does_not_allocate_a_port() -> None:
    conn = _ProvConn()

    def fail_free_port() -> int:
        raise AssertionError("a non-gdbstub provision must not allocate a port")

    _prov_with_port(conn, free_port=fail_free_port).provision(_SYS, _profile())
    assert recorded_gdb_port(conn.recorded_xml[-1]) is None


def test_provision_gdbstub_port_lookup_infra_error_is_infrastructure_failure() -> None:
    # A non-NO_DOMAIN libvirt error during the reuse lookup is an infrastructure fault, not a
    # silent fall-through to a fresh port (which would drift from the live domain).
    conn = _ProvConn(lookup_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError) as caught:
        _prov_with_port(conn, free_port=lambda: 5555).provision(_SYS, _gdb_profile())
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_provision_already_running_domain_is_idempotent() -> None:
    # A retry after a partial provision: defineXML redefines, create() reports "already
    # running" (OPERATION_INVALID) — the desired post-state, not a failure.
    name = domain_name_for(_SYS)
    conn = _ProvConn(
        defined={name: _ProvDomain(name, create_error=libvirt.VIR_ERR_OPERATION_INVALID)}
    )
    assert _prov(conn).provision(_SYS, _profile()) == name  # no raise
    assert conn.closed == 1


def test_teardown_destroys_and_undefines() -> None:
    name = domain_name_for(_SYS)
    dom = _ProvDomain(name)
    conn = _ProvConn(defined={name: dom})
    _prov(conn).teardown(name)
    assert dom.destroyed is True and dom.undefined is True
    assert conn.closed == 1  # the connection is closed after use (no leak)


def test_provision_creates_overlay_over_base_and_attaches_it() -> None:
    # The disk attached to the domain is a per-System overlay backed by the resolved base, so two
    # Systems never contend for the base's qcow2 write lock and guest state does not bleed.
    made: list[tuple[str, str]] = []
    conn = _ProvConn()
    _prov(conn, make_overlay=lambda base, ov: made.append((base, ov))).provision(_SYS, _profile())
    base, overlay = made[0]
    assert base == "/var/lib/kdive/rootfs/fedora-40.qcow2"  # the _VALID base
    assert overlay == overlay_path(_SYS)
    disk = _safe_fromstring(conn.recorded_xml[0]).find("devices/disk/source")
    assert disk is not None and disk.get("file") == overlay  # the domain boots the overlay


def test_provision_prepares_console_log_before_define() -> None:
    calls: list[tuple[str, str]] = []

    def prepare(path: Path) -> None:
        calls.append(("prepare", path.name))

    class RecordingConn(_ProvConn):
        def defineXML(self, xml: str) -> _ProvDomain:  # noqa: N802 - mirrors libvirt binding
            calls.append(("define", "xml"))
            return super().defineXML(xml)

    conn = RecordingConn()
    LocalLibvirtProvisioning(
        connect=lambda: conn,
        files=ProvisioningFiles(
            make_overlay=lambda _base, _overlay: None,
            overlay_exists=lambda _overlay: False,
            prepare_console_log=prepare,
        ),
        materialize_rootfs=lambda _rootfs, _system_id: "/var/lib/kdive/rootfs/base.qcow2",
    ).provision(_SYS, _profile())

    assert calls == [("prepare", f"{_SYS}.log"), ("define", "xml")]


def test_prepare_overlay_reuses_existing_overlay_without_creation() -> None:
    made: list[tuple[str, str]] = []
    files = ProvisioningFiles(
        make_overlay=lambda base, overlay: made.append((base, overlay)),
        overlay_exists=lambda overlay: overlay == overlay_path(_SYS),
    )

    overlay = files.prepare_overlay(_SYS, base="/base.qcow2")

    assert overlay.path == overlay_path(_SYS)
    assert overlay.created is False
    assert made == []


def test_prepare_overlay_creates_missing_overlay() -> None:
    made: list[tuple[str, str]] = []
    files = ProvisioningFiles(
        make_overlay=lambda base, overlay: made.append((base, overlay)),
        overlay_exists=lambda _overlay: False,
    )

    overlay = files.prepare_overlay(_SYS, base="/base.qcow2")

    assert overlay.created is True
    assert made == [("/base.qcow2", overlay_path(_SYS))]


def test_cleanup_overlay_if_created_removes_only_created_overlay() -> None:
    removed: list[str] = []
    files = ProvisioningFiles(remove_overlay=removed.append)

    files.cleanup_overlay_if_created(storage_module.PreparedOverlay(overlay_path(_SYS), True))
    files.cleanup_overlay_if_created(storage_module.PreparedOverlay("/existing.qcow2", False))

    assert removed == [overlay_path(_SYS)]


def test_real_make_overlay_timeout_is_provisioning_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _timeout(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["qemu-img"], timeout=storage_module._QEMU_IMG_TIMEOUT_S)

    monkeypatch.setattr(storage_module.subprocess, "run", _timeout)
    monkeypatch.setattr(storage_module.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    with pytest.raises(CategorizedError) as caught:
        storage_module._real_make_overlay("/base.qcow2", "/overlay.qcow2")

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert str(caught.value) == "qemu-img exceeded the overlay creation timeout"
    assert caught.value.details == {
        "op": "create_overlay",
        "overlay": "overlay.qcow2",
        "tool": "qemu-img",
        "timeout_s": storage_module._QEMU_IMG_TIMEOUT_S,
    }


def test_real_make_overlay_missing_qemu_img_is_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("qemu-img")

    monkeypatch.setattr(storage_module.subprocess, "run", _missing)

    with pytest.raises(CategorizedError) as caught:
        storage_module._real_make_overlay("/base.qcow2", "/overlay.qcow2")

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert caught.value.details == {
        "op": "create_overlay",
        "overlay": "overlay.qcow2",
        "tool": "qemu-img",
    }


def test_real_make_overlay_uses_resolved_qemu_img_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    kwargs_seen: list[dict[str, object]] = []
    monkeypatch.setattr(storage_module.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    def _record(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        kwargs_seen.append(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(storage_module.subprocess, "run", _record)

    storage_module._real_make_overlay("/base.qcow2", "/overlay.qcow2")

    # The full argv must create a qcow2 overlay backed by the qcow2 base: a wrong format flag
    # (e.g. raw backing) would make the guest read the qcow2 header as raw and fail to mount.
    assert calls[0] == [
        "/usr/bin/qemu-img",
        "create",
        "-q",
        "-f",
        "qcow2",
        "-F",
        "qcow2",
        "-b",
        "/base.qcow2",
        "/overlay.qcow2",
    ]
    # Output must be captured as text for the nonzero-return stderr tail, and check must stay
    # False so the function classifies failures itself instead of raising CalledProcessError.
    assert kwargs_seen[0]["capture_output"] is True
    assert kwargs_seen[0]["text"] is True
    assert kwargs_seen[0]["check"] is False
    assert kwargs_seen[0]["timeout"] == storage_module._QEMU_IMG_TIMEOUT_S


def test_real_make_overlay_unresolvable_qemu_img_is_missing_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When qemu-img is not on PATH (which returns None) the function fails fast with a
    # MISSING_DEPENDENCY before ever invoking subprocess.run.
    monkeypatch.setattr(storage_module.shutil, "which", lambda _tool: None)

    def _must_not_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess.run must not be reached when qemu-img is unresolvable")

    monkeypatch.setattr(storage_module.subprocess, "run", _must_not_run)

    with pytest.raises(CategorizedError) as caught:
        storage_module._real_make_overlay("/base.qcow2", "/overlay.qcow2")

    assert caught.value.category is ErrorCategory.MISSING_DEPENDENCY
    assert str(caught.value) == (
        "qemu-img is not installed; cannot create the per-System rootfs overlay"
    )
    assert caught.value.details == {
        "op": "create_overlay",
        "overlay": "overlay.qcow2",
        "tool": "qemu-img",
    }


def test_real_make_overlay_nonzero_return_is_provisioning_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "x" * (storage_module._QEMU_IMG_ERROR_TAIL_CHARS + 10)

    def _failed(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr(storage_module.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(storage_module.subprocess, "run", _failed)

    with pytest.raises(CategorizedError) as caught:
        storage_module._real_make_overlay("/base.qcow2", "/overlay.qcow2")

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert str(caught.value) == "qemu-img failed to create the per-System rootfs overlay"
    assert caught.value.details == {
        "op": "create_overlay",
        "overlay": "overlay.qcow2",
        "tool": "qemu-img",
        "stderr": stderr[-storage_module._QEMU_IMG_ERROR_TAIL_CHARS :],
    }


def test_real_make_overlay_launch_oserror_is_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fork_failed(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise OSError("fork failed")

    monkeypatch.setattr(storage_module.subprocess, "run", _fork_failed)
    monkeypatch.setattr(storage_module.shutil, "which", lambda tool: f"/usr/bin/{tool}")

    with pytest.raises(CategorizedError) as caught:
        storage_module._real_make_overlay("/base.qcow2", "/overlay.qcow2")

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(caught.value) == "failed to launch qemu-img to create the per-System rootfs overlay"
    assert caught.value.details == {
        "op": "create_overlay",
        "overlay": "overlay.qcow2",
        "tool": "qemu-img",
        "error": "OSError",
    }


def test_real_remove_overlay_oserror_is_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unlink_failed(self: object, *, missing_ok: bool = False) -> None:
        del self, missing_ok
        raise PermissionError("permission denied")

    monkeypatch.setattr(storage_module.Path, "unlink", _unlink_failed)

    with pytest.raises(CategorizedError) as caught:
        storage_module._real_remove_overlay("/rootfs/overlay.qcow2")

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(caught.value) == "failed to remove the per-System rootfs overlay"
    assert caught.value.details == {
        "op": "remove_overlay",
        "overlay": "overlay.qcow2",
        "error": "PermissionError",
    }


def test_real_remove_overlay_absent_file_is_noop(tmp_path: Path) -> None:
    # An absent overlay is the achieved post-state: unlink(missing_ok=True) must not raise.
    storage_module._real_remove_overlay(str(tmp_path / "gone-overlay.qcow2"))


def test_teardown_removes_the_overlay() -> None:
    removed: list[str] = []
    name = domain_name_for(_SYS)
    conn = _ProvConn(defined={name: _ProvDomain(name)})
    _prov(conn, remove_overlay=removed.append).teardown(name)
    assert removed == [overlay_path(_SYS)]


def test_teardown_removes_overlay_even_when_domain_already_gone() -> None:
    # The overlay must be reclaimed regardless of whether the domain still exists.
    removed: list[str] = []
    conn = _ProvConn(lookup_error=libvirt.VIR_ERR_NO_DOMAIN)
    _prov(conn, remove_overlay=removed.append).teardown(domain_name_for(_SYS))
    assert removed == [overlay_path(_SYS)]


def test_provision_skips_overlay_create_when_it_already_exists() -> None:
    # Idempotent retry of an already-running System: the overlay QEMU still holds open must NOT
    # be recreated (qemu-img would fail the lock or truncate the live disk). provision skips the
    # create when the overlay is present and still reaches the already-running success post-state.
    def _boom(_base: str, _overlay: str) -> None:
        raise AssertionError("make_overlay must not run when the overlay already exists")

    name = domain_name_for(_SYS)
    conn = _ProvConn(
        defined={name: _ProvDomain(name, create_error=libvirt.VIR_ERR_OPERATION_INVALID)}
    )
    prov = _prov(conn, make_overlay=_boom, overlay_exists=lambda _overlay: True)
    assert prov.provision(_SYS, _profile()) == name  # no raise, no overlay recreate


def test_provision_create_failure_removes_the_overlay() -> None:
    # A real start failure must reclaim the overlay it just created, mirroring the domain undefine,
    # so a failed provision leaks neither a defined domain nor an overlay file.
    removed: list[str] = []
    name = domain_name_for(_SYS)
    conn = _ProvConn(defined={name: _ProvDomain(name, create_error=libvirt.VIR_ERR_INTERNAL_ERROR)})
    with pytest.raises(CategorizedError):
        _prov(conn, remove_overlay=removed.append).provision(_SYS, _profile())
    assert removed == [overlay_path(_SYS)]


def test_provision_cleanup_failure_preserves_start_failure_category() -> None:
    name = domain_name_for(_SYS)
    conn = _ProvConn(defined={name: _ProvDomain(name, create_error=libvirt.VIR_ERR_INTERNAL_ERROR)})

    def fail_remove(_overlay: str) -> None:
        raise CategorizedError(
            "synthetic overlay cleanup failure",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        )

    with pytest.raises(CategorizedError) as caught:
        _prov(conn, remove_overlay=fail_remove).provision(_SYS, _profile())

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_provision_console_log_failure_removes_the_overlay() -> None:
    removed: list[str] = []
    conn = _ProvConn()

    def fail_prepare(_path: Path) -> None:
        raise CategorizedError(
            "synthetic console log failure",
            category=ErrorCategory.PROVISIONING_FAILURE,
        )

    with pytest.raises(CategorizedError) as caught:
        LocalLibvirtProvisioning(
            connect=lambda: conn,
            files=ProvisioningFiles(
                make_overlay=lambda _base, _overlay: None,
                remove_overlay=removed.append,
                overlay_exists=lambda _overlay: False,
                prepare_console_log=fail_prepare,
            ),
            materialize_rootfs=lambda _rootfs, _system_id: "/var/lib/kdive/rootfs/base.qcow2",
        ).provision(_SYS, _profile())

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert removed == [overlay_path(_SYS)]
    assert conn.recorded_xml == []


def test_provision_failure_keeps_preexisting_overlay() -> None:
    # A retry can fail after finding an existing overlay. That overlay may belong to a live or
    # recoverable previous attempt, so this call must not remove a file it did not create.
    removed: list[str] = []
    conn = _ProvConn(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError):
        _prov(
            conn,
            remove_overlay=removed.append,
            overlay_exists=lambda _overlay: True,
        ).provision(_SYS, _profile())
    assert removed == []


def test_provision_failure_still_closes_connection() -> None:
    conn = _ProvConn(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError):
        _prov(conn).provision(_SYS, _profile())
    assert conn.closed == 1  # closed even on a libvirt failure


def test_teardown_absent_domain_closes_connection() -> None:
    conn = _ProvConn()
    _prov(conn).teardown(domain_name_for(_SYS))  # NO_DOMAIN -> early return
    assert conn.closed == 1  # the finally still closes


def test_teardown_absent_domain_is_noop() -> None:
    _prov(_ProvConn()).teardown(domain_name_for(_SYS))  # no raise


def test_teardown_not_running_domain_still_undefines() -> None:
    name = domain_name_for(_SYS)
    dom = _ProvDomain(name, destroy_error=libvirt.VIR_ERR_OPERATION_INVALID)
    conn = _ProvConn(defined={name: dom})
    _prov(conn).teardown(name)
    assert dom.undefined is True  # OPERATION_INVALID on destroy is ignored


def test_teardown_other_libvirt_error_is_infrastructure_failure() -> None:
    conn = _ProvConn(lookup_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).teardown(domain_name_for(_SYS))
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_reprovision_tears_down_then_redefines_same_name() -> None:
    # Reprovision-in-place (ADR-0038): destroy+undefine the current domain, then define+start
    # the new profile under the SAME deterministic domain name (same system_id).
    name = domain_name_for(_SYS)
    old = _ProvDomain(name)
    conn = _ProvConn(defined={name: old})
    result = _prov(conn).reprovision(_SYS, _profile())
    assert result == name  # same domain name (same system_id)
    assert old.destroyed is True and old.undefined is True  # prior install wiped (destructive)
    assert conn.defined[name].created is True  # the new domain is defined and started


def test_reprovision_on_absent_domain_still_provisions() -> None:
    # A reprovision whose prior domain is already gone (e.g. a retry after a partial wipe)
    # tears down idempotently (NO_DOMAIN swallowed) and provisions the new install.
    conn = _ProvConn()
    name = _prov(conn).reprovision(_SYS, _profile())
    assert conn.defined[name].created is True


def test_reprovision_define_failure_is_provisioning_failure() -> None:
    name = domain_name_for(_SYS)
    conn = _ProvConn(defined={name: _ProvDomain(name)}, define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).reprovision(_SYS, _profile())
    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_from_env_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu:///system")
    prov = LocalLibvirtProvisioning.from_env()  # building must not open a connection
    assert isinstance(prov, LocalLibvirtProvisioning)


def test_from_env_connect_callable_opens_the_configured_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # from_env wires the configured URI into a lazy connect callable; invoking it must call
    # libvirt.open with that exact URI (not None and not a different value).
    monkeypatch.setenv("KDIVE_LIBVIRT_URI", "qemu+ssh://host/system")
    opened: list[object] = []

    def fake_open(uri: object) -> _ProvConn:
        opened.append(uri)
        return _ProvConn()

    monkeypatch.setattr(provisioning_module.libvirt, "open", fake_open)

    prov = LocalLibvirtProvisioning.from_env()
    conn = prov._connect()

    assert opened == ["qemu+ssh://host/system"]
    assert isinstance(conn, _ProvConn)


def test_provision_failure_message_and_details_carry_system_id() -> None:
    # A define/start failure surfaces a PROVISIONING_FAILURE whose human message names the
    # operation and whose details carry the System id for triage.
    conn = _ProvConn(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).provision(_SYS, _profile())

    assert str(caught.value) == "libvirt failed to define/start the domain"
    assert caught.value.details == {"system_id": str(_SYS)}


def test_provision_passes_real_system_id_to_materialize_and_define() -> None:
    # provision threads the caller's system_id (not None or a placeholder) through both the
    # rootfs materialization seam and the define/start failure details.
    seen: list[UUID] = []
    conn = _ProvConn(define_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    prov = LocalLibvirtProvisioning(
        connect=lambda: conn,
        files=ProvisioningFiles(
            make_overlay=lambda _base, _overlay: None,
            remove_overlay=lambda _overlay: None,
            overlay_exists=lambda _overlay: False,
            prepare_console_log=lambda _path: None,
        ),
        materialize_rootfs=lambda rootfs, system_id: (
            seen.append(system_id) or rootfs.path  # type: ignore[func-returns-value]
        ),
    )

    with pytest.raises(CategorizedError) as caught:
        prov.provision(_SYS, _profile())

    assert seen == [_SYS]
    assert caught.value.details == {"system_id": str(_SYS)}


def test_teardown_lookup_error_message_and_details_name_the_domain() -> None:
    # A non-NO_DOMAIN lookup failure is an INFRASTRUCTURE_FAILURE whose message names the
    # "looking up" verb and whose details carry the domain name.
    name = domain_name_for(_SYS)
    conn = _ProvConn(lookup_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).teardown(name)

    assert str(caught.value) == "libvirt error looking up domain"
    assert caught.value.details == {"domain": name}


def test_validate_rootfs_ref_local_uses_default_allowed_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A provisioning built without explicit allowed_roots validates a local rootfs against the
    # default ROOTFS_DIR root; the default must be a usable list, not None.
    seen_roots: list[list[Path]] = []

    def fake_materialize(rootfs: object, *, context: RootfsMaterializationContext) -> Path:
        del rootfs
        seen_roots.append(list(context.allowed_roots))
        return Path("/var/lib/kdive/rootfs/fedora-40.qcow2")

    monkeypatch.setattr(provisioning_module, "materialize_rootfs_base", fake_materialize)

    prov = LocalLibvirtProvisioning(connect=lambda: _ProvConn())
    prov.validate_rootfs_ref(_profile().provider.local_libvirt.rootfs)

    assert seen_roots == [[Path(provisioning_module.ROOTFS_DIR)]]


def test_domain_xml_has_serial_console_with_log() -> None:
    # Parse with defusedxml (XXE-safe), matching install.py's _safe_fromstring; stdlib ET
    # parsing is vulnerable to XXE/billion-laughs even on self-rendered strings in tests.
    sid = UUID("00000000-0000-0000-0000-0000000000aa")
    root = _safe_fromstring(_render(system_id=sid))
    serial = root.find("./devices/serial[@type='pty']")
    assert serial is not None
    log = serial.find("log")
    assert log is not None
    assert log.get("file") == str(console_log_path(sid))
    # The paired <console> redirect is what makes the serial device usable.
    console = root.find("./devices/console[@type='pty']")
    assert console is not None
    target = console.find("target")
    assert target is not None
    assert target.get("type") == "serial"
    assert target.get("port") == "0"
