"""Remote-libvirt runtime composition assertions (ADR-0183)."""

from __future__ import annotations

from kdive.providers.remote_libvirt import composition
from kdive.security.secrets.secret_registry import SecretRegistry


def test_remote_runtime_owns_no_platform_root_cmdline() -> None:
    # The remote base image is partitioned and boots via in-guest GRUB (root=UUID, inherited by
    # grubby --copy-default). The platform must not inject a root device or it overrides that — so
    # the remote runtime carries platform_root_cmdline=None, unlike local's "root=/dev/vda" (#587).
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.platform_root_cmdline is None
