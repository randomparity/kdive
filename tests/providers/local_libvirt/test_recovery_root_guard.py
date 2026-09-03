"""Hold the recovery-root setting's restated guard in step with the real one (#2210).

``settings._private_owned_directory`` restates the conditions
``external_boot._require_private_owned_directory`` enforces, because the settings module
must not import the provider (ADR-0087). These tests are what stop the two copies drifting:
they run a provisioned-shape directory through the **real** guard rather than re-asserting
the mode in isolation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kdive.config.registry import Registry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt import settings
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    RecoveryMetadataStore,
    _open_private_directory,
)


def _provisioned_root(tmp_path: Path) -> Path:
    """A directory of exactly the shape the live_vm_host role creates.

    Built with ``mkdir()`` + ``chmod()``, never ``mkdir(mode=...)``: that mode argument is
    masked by the process umask, so a 0o711 parent silently becomes 0o700 under umask 077
    and these assertions would pass or fail by environment rather than by behaviour.
    """
    parent = tmp_path / "external-boot-recovery"
    parent.mkdir()
    parent.chmod(0o711)
    root = parent / "1"
    root.mkdir()
    root.chmod(0o700)
    return root


def test_provisioned_shape_is_accepted_by_the_real_guard(tmp_path: Path) -> None:
    root = _provisioned_root(tmp_path)
    parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # The real guard, unchanged. A mode assertion in isolation would not prove the
        # provisioned shape is openable through the path the recovery lifecycle uses.
        fd = _open_private_directory(parent_fd, root.name)
        os.close(fd)
    finally:
        os.close(parent_fd)
    with RecoveryMetadataStore(root):
        pass


def test_the_setting_accepts_exactly_what_the_real_store_accepts(tmp_path: Path) -> None:
    root = _provisioned_root(tmp_path)
    assert settings.LIBVIRT_RECOVERY_ROOT.parse(str(root)) == root


@pytest.mark.parametrize("mode", [0o750, 0o755, 0o770, 0o600])
def test_the_setting_and_the_real_store_reject_the_same_modes(tmp_path: Path, mode: int) -> None:
    root = _provisioned_root(tmp_path)
    root.chmod(mode)
    with pytest.raises(ValueError):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(root))
    with pytest.raises(ValueError):
        RecoveryMetadataStore(root)


def test_the_setting_and_the_real_store_both_reject_a_symlinked_root(tmp_path: Path) -> None:
    # Both reject, but NOT with the same exception type, so this asserts rejection rather
    # than a shared type: the setting raises ValueError from its own lstat check, while the
    # store's O_NOFOLLOW open raises NotADirectoryError (ENOTDIR) before
    # _require_private_owned_directory is ever reached. Asserting a shared type here would
    # be green for the wrong reason and would encode a false claim about where the
    # rejection happens.
    root = _provisioned_root(tmp_path)
    link = root.parent / "2"
    link.symlink_to(root)
    with pytest.raises(ValueError):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(link))
    with pytest.raises(OSError):
        RecoveryMetadataStore(link)


def test_a_bad_root_fails_at_configuration_resolution(tmp_path: Path) -> None:
    """validate() is the startup preflight: a bad root fails there, not at first recovery."""
    root = _provisioned_root(tmp_path)
    root.chmod(0o755)
    registry = Registry([settings.LIBVIRT_RECOVERY_ROOT])
    registry.load({"KDIVE_LIBVIRT_RECOVERY_ROOT": str(root)})
    with pytest.raises(CategorizedError) as caught:
        registry.validate("worker")
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "KDIVE_LIBVIRT_RECOVERY_ROOT" in str(caught.value)


def test_an_absent_root_is_rejected_by_name(tmp_path: Path) -> None:
    registry = Registry([settings.LIBVIRT_RECOVERY_ROOT])
    registry.load({})
    with pytest.raises(CategorizedError) as caught:
        registry.require(settings.LIBVIRT_RECOVERY_ROOT)
    assert "KDIVE_LIBVIRT_RECOVERY_ROOT is not set" in str(caught.value)
    assert caught.value.details["suggest"]


def test_an_unconfigured_host_still_starts(tmp_path: Path) -> None:
    """Never-required is what keeps the dormant path off until #2212 consumes the root.

    A required_when that held would fail every worker host that has not provisioned a
    recovery root yet, turning the external-boot path on ahead of its consuming change.
    """
    registry = Registry([settings.LIBVIRT_RECOVERY_ROOT])
    registry.load({})
    registry.validate("worker")
    assert registry.get(settings.LIBVIRT_RECOVERY_ROOT) is None
