"""Live proof for #747 (ADR-0233): real libvirt accepts kdive's gdbstub+preserve domain XML,
and the gdbstub is reachable (kdive's own ``rsp_reachable``) on a preserved early-boot panic.

`live_vm`-gated (bzimage family, ADR-0392). The operator points ``KDIVE_LIVE_VM_BZIMAGE`` at a
kernel image that panics early in boot when it cannot mount its root (a bare bzImage with no
usable rootfs), optionally overriding ``KDIVE_LIBVIRT_URI`` (default ``qemu:///session`` so it
needs no root). The test renders the real provisioning XML (its SUT), adds the direct-kernel
``<os>`` the install step adds in the full pipeline, and hands the finished XML to
``boot_gdbstub_domain`` — which starts the domain against a deliberately empty disk to
force the panic, waits for it, and tears the transient domain down. The test then asserts the stub
answers ``rsp_reachable``.

The rendered XML is stripped of its production ownership claim first (#1968); the unmarked tests
below hold that contract so the gated proof cannot silently regain it.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from kdive.providers.shared.debug_common.rsp import rsp_reachable
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS, QEMU_NS
from kdive.providers.shared.runtime_paths import system_id_from_domain_name
from kdive.testing.live_vm import boot_gdbstub_domain
from tests.live_vm import require_live_vm_bzimage
from tests.support.domain_ownership import drop_ownership_metadata

_GDB_PORT = 51234
# The SSH forward is rendered on every domain now (ADR-0281); this panic-boot test never reaches
# sshd, but render requires the port. Pinned distinct from the gdbstub port.
_SSH_PORT = 51235
# Non-convention name prefix, so ``system_id_from_domain_name`` rejects it — see
# ``_render_preserve_domain``. Matches the ``kdive-<purpose>-live-`` form the other live throwaway
# domains use, which keeps the domain recognisable as kdive test debris to an operator.
_DOMAIN_NAME_PREFIX = "kdive-preserve-live-"


@pytest.mark.live_vm
@pytest.mark.live_vm_throwaway
def test_live_vm_preserve_crash_stub_is_reachable(tmp_path: Path) -> None:  # pragma: no cover
    contract = require_live_vm_bzimage()
    try:
        import libvirt  # noqa: F401, PLC0415  # operator-provided; presence gates the live boot
    except ImportError:
        pytest.skip("libvirt-python unavailable")

    garbage_disk = tmp_path / "garbage.qcow2"
    console = tmp_path / "console.log"
    _make_empty_qcow2(garbage_disk)
    console.write_text("")

    final_xml = _render_preserve_domain(
        bzimage=contract.bzimage, disk=garbage_disk, console=console
    )

    # The harness boot both proves libvirt accepts the new pvpanic + <on_crash>preserve</on_crash>
    # + -gdb passthrough XML (createXML raising is a failure) and waits for the early-boot panic.
    with boot_gdbstub_domain(
        final_xml,
        uri=contract.libvirt_uri,
        wait_for="panic",
        console_log=console,
    ):
        # The crash signal is the console panic; the stub stays reachable on the halted vCPU
        # (domain may remain RUNNING with panic=0, so this does NOT assert VIR_DOMAIN_CRASHED).
        assert rsp_reachable("127.0.0.1", _GDB_PORT), "gdbstub not reachable on the halted panic"


def test_preserve_domain_claims_no_production_ownership(tmp_path: Path) -> None:
    """The transient preserve domain must not self-identify as a kdive-owned System (#1968).

    ``repair_leaked_domains`` resolves ownership as ``domain.system_id or
    system_id_from_domain_name(domain.name)``, so a concurrently running production reconciler
    destroys this domain mid-boot unless *both* signals are gone: the ``<metadata>`` ownership
    tag ``render_domain_xml`` stamps with the throwaway ``uuid4()``, and the rendered
    ``kdive-<uuid>`` name the reaper falls back to when the tag is absent. #1930 closed only the
    metadata path for the two live-debug helpers, which is enough for them because they also
    rename; this domain kept its convention-shaped name, so the fallback still matched.
    """
    root = ET.fromstring(_rendered(tmp_path))  # noqa: S314 - kdive-rendered, trusted

    assert root.find("metadata") is None, "transient domain still claims kdive ownership"
    assert KDIVE_METADATA_NS not in ET.tostring(root, encoding="unicode")
    name = root.findtext("name")
    assert name is not None and name.startswith(_DOMAIN_NAME_PREFIX), name
    assert system_id_from_domain_name(name) is None, "name still reaps by convention"


def test_preserve_domain_keeps_production_gdbstub_xml(tmp_path: Path) -> None:
    """Disowning the domain must remove only the claim, not the production XML under test."""
    root = ET.fromstring(_rendered(tmp_path))  # noqa: S314 - kdive-rendered, trusted

    arg_path = f"./{{{QEMU_NS}}}commandline/{{{QEMU_NS}}}arg"
    args = [arg.get("value") for arg in root.findall(arg_path)]
    assert "-gdb" in args
    assert f"tcp:127.0.0.1:{_GDB_PORT}" in args
    # The preserve half of the SUT: pvpanic notifies the host, <on_crash> holds the vCPUs.
    assert root.find("./devices/panic[@model='pvpanic']") is not None
    assert root.findtext("on_crash") == "preserve"
    assert root.findtext("./os/kernel")
    assert root.findtext("./os/cmdline")
    assert root.find("./devices/disk/source") is not None
    assert root.find("./devices/serial/log") is not None


def test_preserve_domain_name_is_unique_per_render(tmp_path: Path) -> None:
    """Two concurrent live runs must not collide on the libvirt domain name."""
    assert _name_of(_rendered(tmp_path)) != _name_of(_rendered(tmp_path))


def _rendered(tmp_path: Path) -> str:
    """The finished XML for the unmarked contract tests; the paths need not exist to render."""
    return _render_preserve_domain(
        bzimage=tmp_path / "bzImage",
        disk=tmp_path / "garbage.qcow2",
        console=tmp_path / "console.log",
    )


def _name_of(xml: str) -> str:
    name = ET.fromstring(xml).findtext("name")  # noqa: S314 - kdive-rendered, trusted
    assert name is not None
    return name


def _render_preserve_domain(*, bzimage: Path, disk: Path, console: Path) -> str:
    """Render the production preserve+gdbstub XML, then adapt it for a transient live boot.

    Everything libvirt is being asked to accept stays production output: the pvpanic device,
    ``<on_crash>preserve</on_crash>``, the ``-gdb`` passthrough, the SSH forward, and the disk.
    Only the ownership claim and the boot-specific paths are rewritten — the direct-kernel
    ``<os>`` install.py adds in the full pipeline, and a writable serial log.
    """
    profile = ProvisioningProfile.parse(_profile_data(disk))
    base_xml = render_domain_xml(
        uuid4(),
        profile,
        disk_path=str(disk),
        gdb_port=_GDB_PORT,
        ssh_port=_SSH_PORT,
        kernel_path=bzimage,
    )
    root = ET.fromstring(base_xml)  # noqa: S314 - kdive-rendered, trusted
    drop_ownership_metadata(root)
    # The second ownership signal: with the metadata gone, repair_leaked_domains falls back to
    # system_id_from_domain_name, which the rendered kdive-<uuid> name still satisfies. Nothing
    # looks this domain up by name — boot_gdbstub_domain reads it only for LiveDomain.name and
    # its timeout message — so a per-render unique, non-convention name is free to use.
    name = root.find("name")
    assert name is not None
    name.text = f"{_DOMAIN_NAME_PREFIX}{uuid4().hex[:12]}"
    os_el = root.find("os")
    assert os_el is not None
    ET.SubElement(os_el, "kernel").text = str(bzimage)
    # No usable rootfs in the empty disk -> VFS panic; panic=0 halts (does not reboot).
    ET.SubElement(os_el, "cmdline").text = "console=ttyS0 panic=0 root=/dev/vda"
    serial_log = root.find("./devices/serial/log")
    assert serial_log is not None
    serial_log.set("file", str(console))
    return ET.tostring(root, encoding="unicode")


def _profile_data(disk: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 2,
        "memory_mb": 1024,
        "disk_gb": 5,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
        "provider": {
            "local-libvirt": {
                "domain_xml_params": {"machine": "pc-q35-9.0"},
                "rootfs": {"kind": "local", "path": str(disk)},
                "debug": {"gdbstub": True, "preserve_on_crash": True},
            }
        },
    }


def _make_empty_qcow2(path: Path) -> None:
    import subprocess  # noqa: PLC0415

    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", str(path), "1G"],
        check=True,
        capture_output=True,
    )
