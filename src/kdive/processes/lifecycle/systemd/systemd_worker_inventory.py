"""Metadata-only trust check for the operator inventory path (ADR-0574 amendment, #3086).

The root witness writes the operator-named ``systems.toml`` path into each slot's environment. It
inspects that path with ``lstat`` only and never opens, reads, or parses it, so root cannot be used
to read a file on the operator's behalf. Readability by the slot accounts is the operator-side
launcher's check; this one keeps any slot principal from rewriting what another slot loads.
"""

from __future__ import annotations

import grp
import os
import pwd
import stat
from collections.abc import Callable
from pathlib import Path

_SLOT_ACCOUNTS = tuple(f"kdive-worker-{slot}" for slot in range(1, 9))
_LIBVIRT_GROUP = "kdive-live-libvirt"

Principals = tuple[frozenset[int], frozenset[int]]


class UntrustedInventory(ValueError):
    """The inventory path fails the fixed-worker trust rules."""


def slot_principals() -> Principals:
    """Return the UIDs of the slot accounts and every group any of them holds."""
    accounts = [pwd.getpwnam(name) for name in _SLOT_ACCOUNTS]
    gids = {grp.getgrnam(_LIBVIRT_GROUP).gr_gid}
    for account in accounts:
        gids.update(os.getgrouplist(account.pw_name, account.pw_gid))
    return frozenset(account.pw_uid for account in accounts), frozenset(gids)


def require_trusted_inventory(
    path: str, *, principals: Callable[[], Principals] = slot_principals
) -> None:
    """Reject a path a slot principal could rewrite, or that is not a plain regular file.

    Raises:
        UntrustedInventory: the path is not absolute and normalized, traverses a symlink or a
            missing component, does not name a regular file, or is writable by a slot principal
            at the file or any ancestor directory.
    """
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\x00" in path
        or os.path.normpath(path) != path
    ):
        raise UntrustedInventory("worker inventory path must be absolute and normalized")
    uids, gids = principals()
    parts = Path(path).parts
    for depth in range(1, len(parts) + 1):
        try:
            metadata = os.lstat(Path(*parts[:depth]))
        except OSError as exc:
            raise UntrustedInventory("worker inventory path must exist") from exc
        _require_kind(metadata.st_mode, leaf=depth == len(parts))
        if _writable_by(metadata, uids, gids):
            raise UntrustedInventory(
                "worker inventory path must not be writable by a fixed worker account"
            )


def _require_kind(mode: int, *, leaf: bool) -> None:
    if stat.S_ISLNK(mode):
        raise UntrustedInventory("worker inventory path must not traverse a symlink")
    if leaf and not stat.S_ISREG(mode):
        raise UntrustedInventory("worker inventory path must name a regular file")
    if not leaf and not stat.S_ISDIR(mode):
        raise UntrustedInventory("worker inventory path must exist")


def _writable_by(metadata: os.stat_result, uids: frozenset[int], gids: frozenset[int]) -> bool:
    mode = metadata.st_mode
    if metadata.st_uid in uids:
        return True
    if mode & stat.S_IWGRP and metadata.st_gid in gids:
        return True
    # A sticky directory stops non-owners from replacing an entry they do not own (/tmp).
    return bool(mode & stat.S_IWOTH) and not (stat.S_ISDIR(mode) and mode & stat.S_ISVTX)
