"""Shared libvirt XML contract helpers."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from kdive.providers.shared.libvirt_xml import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    parse_capabilities_arch,
    parse_metadata_system_id,
    recorded_gdb_port,
    recorded_ssh_port,
    register_kdive_namespace,
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
