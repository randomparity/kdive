"""Metadata-only trust check for the operator inventory path (ADR-0574, #3086)."""

from __future__ import annotations

import builtins
import io
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import kdive.processes.lifecycle.systemd.systemd_worker_inventory as inventory
from kdive.processes.lifecycle.systemd.systemd_worker_inventory import (
    UntrustedInventory,
    require_trusted_inventory,
)

_FOREIGN_UID = 4_000_000
_FOREIGN_GID = 4_000_001


def _none_forbidden() -> tuple[frozenset[int], frozenset[int]]:
    return frozenset({_FOREIGN_UID}), frozenset({_FOREIGN_GID})


def _forbid(*, uid: int | None = None, gid: int | None = None):
    def principals() -> tuple[frozenset[int], frozenset[int]]:
        return (
            frozenset({_FOREIGN_UID} if uid is None else {uid}),
            frozenset({_FOREIGN_GID} if gid is None else {gid}),
        )

    return principals


@pytest.fixture
def operator_tree(tmp_path: Path) -> Path:
    """A private, operator-owned directory holding a 0644 inventory."""
    root = tmp_path / "etc"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    target = root / "systems.toml"
    target.write_bytes(b"schema_version = 2\n")
    target.chmod(0o644)
    return target


def test_trusted_operator_file_is_accepted_without_opening_it(
    operator_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root witness must never open the operator-named file (confused deputy)."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the inventory check opened a file")

    for owner, name in ((os, "open"), (io, "open"), (builtins, "open")):
        monkeypatch.setattr(owner, name, refuse)

    require_trusted_inventory(str(operator_tree), principals=_none_forbidden)


@pytest.mark.parametrize("raw", ["etc/systems.toml", "/etc/../etc/systems.toml", "/etc/./x", "//x"])
def test_non_absolute_or_non_normal_paths_are_rejected(raw: str) -> None:
    with pytest.raises(UntrustedInventory, match="absolute and normalized"):
        require_trusted_inventory(raw, principals=_none_forbidden)


def test_trailing_slash_is_rejected(operator_tree: Path) -> None:
    with pytest.raises(UntrustedInventory, match="absolute and normalized"):
        require_trusted_inventory(f"{operator_tree}/", principals=_none_forbidden)


def test_symlinked_leaf_is_rejected(operator_tree: Path) -> None:
    link = operator_tree.with_name("link.toml")
    link.symlink_to(operator_tree)

    with pytest.raises(UntrustedInventory, match="symlink"):
        require_trusted_inventory(str(link), principals=_none_forbidden)


def test_symlinked_ancestor_is_rejected(operator_tree: Path) -> None:
    alias = operator_tree.parent.with_name("alias")
    alias.symlink_to(operator_tree.parent)

    with pytest.raises(UntrustedInventory, match="symlink"):
        require_trusted_inventory(str(alias / "systems.toml"), principals=_none_forbidden)


def test_directory_fifo_and_missing_targets_are_rejected(operator_tree: Path) -> None:
    fifo = operator_tree.with_name("fifo")
    os.mkfifo(fifo, 0o644)

    for target, reason in (
        (operator_tree.parent, "regular file"),
        (fifo, "regular file"),
        (operator_tree.with_name("absent.toml"), "must exist"),
    ):
        with pytest.raises(UntrustedInventory, match=reason):
            require_trusted_inventory(str(target), principals=_none_forbidden)


def test_ancestor_that_is_not_a_directory_is_rejected(operator_tree: Path) -> None:
    with pytest.raises(UntrustedInventory, match="must exist"):
        require_trusted_inventory(f"{operator_tree}/child", principals=_none_forbidden)


def test_file_owned_by_a_slot_account_is_rejected(operator_tree: Path) -> None:
    with pytest.raises(UntrustedInventory, match="writable by a fixed worker"):
        require_trusted_inventory(str(operator_tree), principals=_forbid(uid=os.getuid()))


def test_ancestor_owned_by_a_slot_account_is_rejected(
    operator_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _present(monkeypatch, operator_tree.parent, uid=_FOREIGN_UID)

    with pytest.raises(UntrustedInventory, match="writable by a fixed worker"):
        require_trusted_inventory(str(operator_tree), principals=_none_forbidden)


def test_group_write_is_rejected_only_for_a_slot_group(operator_tree: Path) -> None:
    operator_tree.chmod(0o664)
    gid = operator_tree.stat().st_gid

    require_trusted_inventory(str(operator_tree), principals=_none_forbidden)
    with pytest.raises(UntrustedInventory, match="writable by a fixed worker"):
        require_trusted_inventory(str(operator_tree), principals=_forbid(gid=gid))


def test_slot_group_write_on_an_ancestor_is_rejected(operator_tree: Path) -> None:
    operator_tree.parent.chmod(0o775)
    gid = operator_tree.parent.stat().st_gid

    with pytest.raises(UntrustedInventory, match="writable by a fixed worker"):
        require_trusted_inventory(str(operator_tree), principals=_forbid(gid=gid))


def test_world_writable_file_is_rejected(operator_tree: Path) -> None:
    operator_tree.chmod(0o646)

    with pytest.raises(UntrustedInventory, match="writable by a fixed worker"):
        require_trusted_inventory(str(operator_tree), principals=_none_forbidden)


def test_world_writable_ancestor_is_rejected_unless_sticky(
    operator_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _present(monkeypatch, operator_tree.parent, mode=stat.S_IFDIR | 0o777)
    with pytest.raises(UntrustedInventory, match="writable by a fixed worker"):
        require_trusted_inventory(str(operator_tree), principals=_none_forbidden)

    _present(monkeypatch, operator_tree.parent, mode=stat.S_IFDIR | stat.S_ISVTX | 0o777)
    require_trusted_inventory(str(operator_tree), principals=_none_forbidden)


def _present(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    *,
    uid: int | None = None,
    mode: int | None = None,
) -> None:
    """Report altered metadata for one component without needing root to create it."""
    real = os.lstat

    def lstat(candidate: os.PathLike[str] | str) -> object:
        metadata = real(candidate)
        if Path(candidate) != path:
            return metadata
        return SimpleNamespace(
            st_mode=metadata.st_mode if mode is None else mode,
            st_uid=metadata.st_uid if uid is None else uid,
            st_gid=metadata.st_gid,
        )

    monkeypatch.setattr(inventory.os, "lstat", lstat)


def test_slot_principals_cover_every_slot_account_group_and_the_libvirt_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def account(name: str) -> SimpleNamespace:
        slot = int(name.rsplit("-", 1)[1])
        return SimpleNamespace(pw_name=name, pw_uid=2000 + slot, pw_gid=3000 + slot)

    monkeypatch.setattr(inventory.pwd, "getpwnam", account)
    monkeypatch.setattr(inventory.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=900))
    monkeypatch.setattr(inventory.os, "getgrouplist", lambda _name, gid: [gid, 901])

    uids, gids = inventory.slot_principals()

    assert uids == frozenset(range(2001, 2009))
    assert gids == frozenset({900, 901, *range(3001, 3009)})
