"""Pin the local-libvirt provider's co-located ``KDIVE_LIBVIRT_*`` settings (ADR-0087)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kdive.config.registry import never_required
from kdive.providers.local_libvirt import settings

_RT = frozenset({"worker", "reconciler"})


def test_uri_setting_fields() -> None:
    s = settings.LIBVIRT_URI
    assert s.name == "KDIVE_LIBVIRT_URI"
    assert s.default == "qemu:///system"
    assert s.group == "local-libvirt"
    assert s.processes == _RT
    assert s.secret is False


def test_allocation_cap_setting_fields() -> None:
    s = settings.LIBVIRT_ALLOCATION_CAP
    assert s.name == "KDIVE_LIBVIRT_ALLOCATION_CAP"
    assert s.default == "1"
    assert s.group == "local-libvirt"
    assert s.processes == _RT
    assert s.secret is False


def test_tcg_multiplier_setting_fields() -> None:
    s = settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER
    assert s.name == "KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER"
    assert s.default == "10.0"
    assert s.group == "local-libvirt"
    assert s.processes == _RT
    assert s.secret is False


def test_tcg_multiplier_default_parses_to_ten() -> None:
    s = settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER
    assert s.default is not None
    assert s.parse(s.default) == 10.0


def test_tcg_multiplier_accepts_one_as_opt_out() -> None:
    assert settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER.parse("1") == 1.0


def test_tcg_multiplier_rejects_below_one() -> None:
    # A multiplier < 1 would make a TCG deadline tighter than the KVM baseline (ADR-0341).
    with pytest.raises(ValueError):
        settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER.parse("0.5")


def test_tcg_multiplier_rejects_non_float() -> None:
    with pytest.raises(ValueError):
        settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER.parse("abc")


def test_customization_boot_window_setting_fields() -> None:
    s = settings.LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S
    assert s.name == "KDIVE_LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S"
    assert s.default == "1800"
    assert s.group == "local-libvirt"
    assert s.processes == _RT
    assert s.secret is False


def test_customization_boot_window_default_parses_to_1800() -> None:
    s = settings.LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S
    assert s.default is not None
    assert s.parse(s.default) == 1800


def test_customization_boot_window_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        settings.LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S.parse("0")
    with pytest.raises(ValueError):
        settings.LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S.parse("-1")


def test_boot_window_setting_fields() -> None:
    s = settings.LIBVIRT_BOOT_WINDOW_S
    assert s.name == "KDIVE_LIBVIRT_BOOT_WINDOW_S"
    assert s.default == "900"
    assert s.group == "local-libvirt"
    assert s.processes == _RT
    assert s.secret is False


def test_boot_window_default_parses_to_900() -> None:
    s = settings.LIBVIRT_BOOT_WINDOW_S
    assert s.default is not None
    assert s.parse(s.default) == 900


def test_boot_window_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        settings.LIBVIRT_BOOT_WINDOW_S.parse("0")
    with pytest.raises(ValueError):
        settings.LIBVIRT_BOOT_WINDOW_S.parse("-1")


def test_recovery_root_setting_fields() -> None:
    s = settings.LIBVIRT_RECOVERY_ROOT
    assert s.name == "KDIVE_LIBVIRT_RECOVERY_ROOT"
    # No default is load-bearing twice over: Registry.get returns None (so require() can
    # reject absence by name) only when the setting has no default, and Registry.validate
    # parses only settings whose name is in the environment, so a default would escape the
    # startup preflight as well.
    assert s.default is None
    assert s.group == "local-libvirt"
    assert s.processes == _RT
    assert s.secret is False
    # Identity, not equality: gen_config_reference.py compares `required_when is
    # never_required` against the sentinel, so a locally defined always-false predicate
    # would publish "Required: conditional" for a setting that is never required.
    assert s.required_when is never_required


def test_recovery_root_rejects_a_relative_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        settings.LIBVIRT_RECOVERY_ROOT.parse("relative/recovery")


def test_recovery_root_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="existing directory"):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(tmp_path / "absent"))


def test_recovery_root_rejects_a_non_directory(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("", encoding="utf-8")
    target.chmod(0o700)
    with pytest.raises(ValueError, match="must be a directory"):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(target))


def test_recovery_root_rejects_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    # lstat, not stat: the link is judged as itself, matching the stores' O_NOFOLLOW open.
    with pytest.raises(ValueError, match="symlink"):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(link))


@pytest.mark.parametrize("mode", [0o750, 0o755, 0o770, 0o600])
def test_recovery_root_rejects_a_mode_other_than_0700(tmp_path: Path, mode: int) -> None:
    root = tmp_path / "recovery"
    # mkdir() then chmod(), never mkdir(mode=...): the mode argument is masked by the
    # process umask, so mkdir(mode=0o711) yields 0o700 under umask 077 and the assertion
    # would pass or fail by environment rather than by behaviour.
    root.mkdir()
    root.chmod(mode)
    with pytest.raises(ValueError, match="0o700"):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(root))


def test_recovery_root_rejects_a_foreign_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recovery"
    root.mkdir()
    root.chmod(0o700)
    # geteuid is the comparison the real store makes; move it rather than the directory's
    # owner, which an unprivileged test cannot change.
    owner = os.stat(root).st_uid
    monkeypatch.setattr(settings.os, "geteuid", lambda: owner + 1)
    with pytest.raises(ValueError, match="owned by the running user"):
        settings.LIBVIRT_RECOVERY_ROOT.parse(str(root))


def test_recovery_root_accepts_a_provisioned_shape_directory(tmp_path: Path) -> None:
    root = tmp_path / "recovery"
    root.mkdir()
    root.chmod(0o700)
    assert settings.LIBVIRT_RECOVERY_ROOT.parse(str(root)) == root


def test_settings_list_is_the_declared_settings_in_order() -> None:
    assert settings.SETTINGS == [
        settings.LIBVIRT_URI,
        settings.LIBVIRT_ALLOCATION_CAP,
        settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER,
        settings.LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S,
        settings.LIBVIRT_BOOT_WINDOW_S,
        settings.LIBVIRT_RECOVERY_ROOT,
    ]
