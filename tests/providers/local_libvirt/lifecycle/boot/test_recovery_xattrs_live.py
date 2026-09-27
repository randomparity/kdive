"""Actual guestfs binding and appliance proof for recovery xattrs (#2852)."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    LibguestfsAuthenticatedGuestTree,
)
from kdive.providers.local_libvirt.lifecycle.boot.recovery import (
    ModuleArchiveCapture,
    RealGuestRecoveryWriter,
)
from tests.providers.local_libvirt.lifecycle.boot.test_recovery import (
    BINDING,
    RELEASE,
    FakeTree,
    _entry,
    _sink,
    _source,
)

pytestmark = pytest.mark.live_vm


@pytest.fixture(scope="module")
def appliance() -> Iterator[Any]:
    # Explicit selection requires the real installed binding and appliance: no skip fallback.
    guestfs = importlib.import_module("guestfs")
    guest = guestfs.GuestFS(python_return_dict=True)
    try:
        guest.add_drive_scratch(128 * 1024 * 1024)
        guest.launch()
        guest.part_disk("/dev/sda", "mbr")
        guest.mkfs("ext4", "/dev/sda1")
        guest.mount("/dev/sda1", "/")
        guest.mkdir_p("/lib/modules")
        guest.write("/marker", b"unchanged")
        yield guest
    finally:
        guest.close()


@pytest.mark.parametrize(
    "name,value",
    [
        ("user.test", b""),
        ("user.test", b"plain"),
        ("user.test", "é".encode()),
        ("security.selinux", b"system_u:object_r:modules_object_t:s0\0"),
    ],
)
def test_live_restore_supported_xattr_bytes(
    appliance: Any, tmp_path: Path, name: str, value: bytes
) -> None:
    writer = RealGuestRecoveryWriter()
    capture = writer.capture(
        FakeTree([_entry("entry", xattrs={name: value})]), RELEASE, _sink(tmp_path)
    )
    assert isinstance(capture, ModuleArchiveCapture)
    root = f"/lib/modules/.kdive-{BINDING.activation_id}-{uuid4().hex}"
    tree = LibguestfsAuthenticatedGuestTree(
        appliance, binding=BINDING, release=RELEASE, root=root, mutable=True
    )
    assert writer.restore(tree, RELEASE, capture, _source(tmp_path, capture)) == capture.manifest
    assert appliance.lgetxattr(f"{root}/entry", name) == value


@pytest.mark.parametrize("value", [b"a\0b", b"a\0", b"\xff"])
def test_live_binary_xattr_refuses_before_guest_mutation(
    appliance: Any, tmp_path: Path, value: bytes
) -> None:
    with pytest.raises(TypeError, match="must be str, not bytes"):
        appliance.lsetxattr("user.test", value, len(value), "/marker")
    writer = RealGuestRecoveryWriter()
    capture = writer.capture(
        FakeTree([_entry("a"), _entry("z", xattrs={"user.test": value})]),
        RELEASE,
        _sink(tmp_path),
    )
    assert isinstance(capture, ModuleArchiveCapture)
    root = f"/lib/modules/.kdive-{BINDING.activation_id}-{uuid4().hex}"
    tree = LibguestfsAuthenticatedGuestTree(
        appliance, binding=BINDING, release=RELEASE, root=root, mutable=True
    )
    with pytest.raises(ValueError, match="operator-assisted recovery"):
        writer.restore(tree, RELEASE, capture, _source(tmp_path, capture))
    assert not appliance.exists(root)
    assert appliance.read_file("/marker") == b"unchanged"
