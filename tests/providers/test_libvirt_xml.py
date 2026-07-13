"""Shared libvirt XML contract helpers."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from kdive.providers.shared.libvirt_xml import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    parse_capabilities_arch,
    parse_guest_arches,
    parse_metadata_system_id,
    recorded_gdb_port,
    recorded_ssh_port,
    register_kdive_namespace,
)

_SUPPORTED = frozenset({"x86_64", "ppc64le"})


def _guest(
    arch: str, emulator: str | None, domains: tuple[str, ...], *, os_type: str = "hvm"
) -> str:
    domain_xml = "".join(f"<domain type='{d}'/>" for d in domains)
    emulator_xml = f"<emulator>{emulator}</emulator>" if emulator is not None else ""
    return (
        f"<guest><os_type>{os_type}</os_type>"
        f"<arch name='{arch}'><wordsize>64</wordsize>{emulator_xml}"
        f"<machine maxCpus='255'>somemachine</machine>{domain_xml}</arch></guest>"
    )


def _caps(host_arch: str, guests: str) -> str:
    return f"<capabilities><host><cpu><arch>{host_arch}</arch></cpu></host>{guests}</capabilities>"


# The x86_64 dev host with qemu-system-ppc installed, verified live: six guest arches, only the
# native x86_64 carrying a kvm domain; ppc64le is TCG-only via /usr/bin/qemu-system-ppc64.
_X86_HOST_CAPS = _caps(
    "x86_64",
    _guest("i686", "/usr/bin/qemu-system-i386", ("qemu", "kvm"))
    + _guest("ppc", "/usr/bin/qemu-system-ppc", ("qemu",))
    + _guest("ppc64", "/usr/bin/qemu-system-ppc64", ("qemu",))
    + _guest("ppc64le", "/usr/bin/qemu-system-ppc64", ("qemu",))
    + _guest("s390x", "/usr/bin/qemu-system-s390x", ("qemu",))
    + _guest("x86_64", "/usr/bin/qemu-system-x86_64", ("qemu", "kvm")),
)

# The real POWER10 host, verified live: /dev/kvm present but no kvm domain for any arch, so the
# native ppc64le falls back to tcg. Note the distinct emulator binary (qemu-system-ppc64le).
_PPC_HOST_CAPS = _caps(
    "ppc64le",
    _guest("ppc64le", "/usr/bin/qemu-system-ppc64le", ("qemu",))
    + _guest("x86_64", "/usr/bin/qemu-system-x86_64", ("qemu",)),
)

_GDB_DOMAIN = (
    f"<domain xmlns:qemu='{QEMU_NS}'>"
    "<qemu:commandline>"
    "<qemu:arg value='-gdb'/>"
    "<qemu:arg value='tcp:127.0.0.1:4444'/>"
    "</qemu:commandline>"
    "</domain>"
)


def _ssh_arg(port: int) -> str:
    return f"user,id=kdivessh,restrict=on,hostfwd=tcp:127.0.0.1:{port}-:22"


def _ssh_domain(port: int) -> str:
    return (
        f"<domain xmlns:qemu='{QEMU_NS}'><qemu:commandline>"
        "<qemu:arg value='-netdev'/>"
        f"<qemu:arg value='{_ssh_arg(port)}'/>"
        "<qemu:arg value='-device'/>"
        "<qemu:arg value='virtio-net-pci,netdev=kdivessh,addr=0x10'/>"
        "</qemu:commandline></domain>"
    )


def test_parse_capabilities_arch_reads_host_cpu_arch() -> None:
    xml = "<capabilities><host><cpu><arch>x86_64</arch></cpu></host></capabilities>"
    assert parse_capabilities_arch(xml) == "x86_64"


def test_parse_capabilities_arch_returns_unknown_for_missing_or_malformed() -> None:
    assert parse_capabilities_arch("<capabilities><host /></capabilities>") == "unknown"
    assert parse_capabilities_arch("<not-xml") == "unknown"


def test_parse_capabilities_arch_returns_unknown_for_defused_xml_exception() -> None:
    xml = "<!DOCTYPE x [<!ENTITY boom SYSTEM 'file:///etc/passwd'>]><capabilities>&boom;</capabilities>"
    assert parse_capabilities_arch(xml) == "unknown"


def test_parse_guest_arches_x86_host_filters_to_supported_with_native_kvm() -> None:
    # Six libvirt arches filtered to the two kdive supports; native x86_64 has a kvm domain -> kvm;
    # foreign ppc64le is TCG-only. i686 (has a kvm domain but unsupported), ppc, ppc64-BE, s390x
    # are dropped.
    assert parse_guest_arches(_X86_HOST_CAPS, _SUPPORTED) == {
        "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"},
        "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64"},
    }


def test_parse_guest_arches_x86_host_without_ppc_binary_is_x86_only() -> None:
    caps = _caps("x86_64", _guest("x86_64", "/usr/bin/qemu-system-x86_64", ("qemu", "kvm")))
    assert parse_guest_arches(caps, _SUPPORTED) == {
        "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"}
    }


def test_parse_guest_arches_ppc_host_no_kvm_domain_is_all_tcg() -> None:
    # Real POWER10 host: KVM present but no kvm domain advertised, so the native ppc64le is tcg.
    assert parse_guest_arches(_PPC_HOST_CAPS, _SUPPORTED) == {
        "ppc64le": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-ppc64le"},
        "x86_64": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-x86_64"},
    }


def test_parse_guest_arches_synthetic_kvm_hv_ppc_host_is_kvm() -> None:
    # Synthetic (no real KVM-HV POWER host available): the ppc64le arch gains a kvm domain.
    caps = _caps(
        "ppc64le",
        _guest("ppc64le", "/usr/bin/qemu-system-ppc64le", ("qemu", "kvm"))
        + _guest("x86_64", "/usr/bin/qemu-system-x86_64", ("qemu",)),
    )
    assert parse_guest_arches(caps, _SUPPORTED) == {
        "ppc64le": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-ppc64le"},
        "x86_64": {"accel": "tcg", "emulator": "/usr/bin/qemu-system-x86_64"},
    }


def test_parse_guest_arches_skips_non_hvm_os_type() -> None:
    caps = _caps(
        "x86_64", _guest("x86_64", "/usr/bin/qemu-system-x86_64", ("qemu",), os_type="xen")
    )
    assert parse_guest_arches(caps, _SUPPORTED) == {}


def test_parse_guest_arches_skips_arch_without_emulator() -> None:
    # A supported arch with a missing or empty <emulator> is not bootable -> dropped.
    missing = _caps("x86_64", _guest("x86_64", None, ("qemu",)))
    empty = _caps(
        "x86_64",
        "<guest><os_type>hvm</os_type><arch name='x86_64'><emulator></emulator>"
        "<domain type='qemu'/></arch></guest>",
    )
    assert parse_guest_arches(missing, _SUPPORTED) == {}
    assert parse_guest_arches(empty, _SUPPORTED) == {}


def test_parse_guest_arches_filter_narrows_to_supplied_set() -> None:
    assert parse_guest_arches(_X86_HOST_CAPS, frozenset({"x86_64"})) == {
        "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"}
    }


def test_parse_guest_arches_returns_empty_for_malformed_or_defused_xml() -> None:
    assert parse_guest_arches("<capabilities", _SUPPORTED) == {}
    xxe = (
        "<!DOCTYPE x [<!ENTITY boom SYSTEM 'file:///etc/passwd'>]>"
        "<capabilities>&boom;</capabilities>"
    )
    assert parse_guest_arches(xxe, _SUPPORTED) == {}


def test_parse_guest_arches_first_occurrence_of_a_duplicate_arch_wins() -> None:
    caps = _caps(
        "x86_64",
        _guest("x86_64", "/usr/bin/qemu-system-x86_64", ("qemu", "kvm"))
        + _guest("x86_64", "/opt/other/qemu-system-x86_64", ("qemu",)),
    )
    assert parse_guest_arches(caps, _SUPPORTED) == {
        "x86_64": {"accel": "kvm", "emulator": "/usr/bin/qemu-system-x86_64"}
    }


def test_parse_metadata_system_id_trims_text_and_rejects_empty_or_malformed() -> None:
    assert parse_metadata_system_id(f"<system xmlns='{KDIVE_METADATA_NS}'> sid </system>") == "sid"
    assert parse_metadata_system_id(f"<system xmlns='{KDIVE_METADATA_NS}' />") is None
    assert parse_metadata_system_id("<system") is None


def test_recorded_gdb_port_reads_the_loopback_gdb_arg() -> None:
    assert recorded_gdb_port(_GDB_DOMAIN) == 4444


def test_recorded_gdb_port_is_none_without_a_gdb_arg() -> None:
    no_gdb = f"<domain xmlns:qemu='{QEMU_NS}'><qemu:commandline></qemu:commandline></domain>"
    assert recorded_gdb_port(no_gdb) is None
    assert recorded_gdb_port("<domain/>") is None


def test_recorded_gdb_port_is_none_for_non_integer_port() -> None:
    bad = (
        f"<domain xmlns:qemu='{QEMU_NS}'><qemu:commandline>"
        "<qemu:arg value='-gdb'/><qemu:arg value='tcp:127.0.0.1:notaport'/>"
        "</qemu:commandline></domain>"
    )
    assert recorded_gdb_port(bad) is None


def test_recorded_gdb_port_is_none_for_malformed_xml() -> None:
    assert recorded_gdb_port("<domain") is None


def test_recorded_ssh_port_reads_the_loopback_hostfwd_arg() -> None:
    assert recorded_ssh_port(_ssh_domain(40022)) == 40022


def test_recorded_ssh_port_is_none_without_a_netdev_arg() -> None:
    no_ssh = f"<domain xmlns:qemu='{QEMU_NS}'><qemu:commandline></qemu:commandline></domain>"
    assert recorded_ssh_port(no_ssh) is None
    assert recorded_ssh_port("<domain/>") is None


def test_recorded_ssh_port_matches_any_bind_host_to_guest_22() -> None:
    # ADR-0291 generalized the regex: any bind host forwarded to guest :22 is a kdive SSH forward
    # (local binds 127.0.0.1, remote binds the ACL'd ssh_addr). Only a non-:22 guest port is not.
    other_host = (
        f"<domain xmlns:qemu='{QEMU_NS}'><qemu:commandline>"
        "<qemu:arg value='-netdev'/>"
        "<qemu:arg value='user,id=x,hostfwd=tcp:10.0.0.1:40022-:22'/>"
        "</qemu:commandline></domain>"
    )
    other_guest = (
        f"<domain xmlns:qemu='{QEMU_NS}'><qemu:commandline>"
        "<qemu:arg value='-netdev'/>"
        "<qemu:arg value='user,id=x,hostfwd=tcp:127.0.0.1:40022-:80'/>"
        "</qemu:commandline></domain>"
    )
    assert recorded_ssh_port(other_host) == 40022
    assert recorded_ssh_port(other_guest) is None


def test_recorded_ssh_port_is_none_for_malformed_xml() -> None:
    assert recorded_ssh_port("<domain") is None


def test_recorded_ssh_port_coexists_with_a_gdb_arg() -> None:
    # A System with both gdbstub and SSH carries both args in one commandline element; each
    # reader reads only its own.
    both = (
        f"<domain xmlns:qemu='{QEMU_NS}'><qemu:commandline>"
        "<qemu:arg value='-gdb'/>"
        "<qemu:arg value='tcp:127.0.0.1:4444'/>"
        "<qemu:arg value='-netdev'/>"
        f"<qemu:arg value='{_ssh_arg(40022)}'/>"
        "<qemu:arg value='-device'/>"
        "<qemu:arg value='virtio-net-pci,netdev=kdivessh,addr=0x10'/>"
        "</qemu:commandline></domain>"
    )
    assert recorded_gdb_port(both) == 4444
    assert recorded_ssh_port(both) == 40022


def test_register_kdive_namespace_is_idempotent(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_register_namespace(prefix: str, uri: str) -> None:
        calls.append((prefix, uri))

    monkeypatch.setattr("kdive.providers.shared.libvirt_xml._kdive_namespace_registered", False)
    monkeypatch.setattr(ET, "register_namespace", fake_register_namespace)
    register_kdive_namespace()
    register_kdive_namespace()
    assert calls == [("kdive", KDIVE_METADATA_NS)]
