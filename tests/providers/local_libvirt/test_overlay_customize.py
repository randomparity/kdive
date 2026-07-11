"""Tests for the provision-time overlay-customizer seam (ADR-0289, #963)."""

from __future__ import annotations

from kdive.providers.local_libvirt.lifecycle.rootfs.overlay_customize import (
    inject_authorized_key_argv,
)


def test_inject_authorized_key_argv_uses_ssh_inject_root() -> None:
    argv = inject_authorized_key_argv("/var/lib/kdive/rootfs/s-overlay.qcow2", "/tmp/k.pub")
    j = " ".join(argv)
    assert argv[0] == "virt-customize"
    assert "-a" in argv and "/var/lib/kdive/rootfs/s-overlay.qcow2" in argv
    assert "--ssh-inject" in argv and "root:file:/tmp/k.pub" in j
