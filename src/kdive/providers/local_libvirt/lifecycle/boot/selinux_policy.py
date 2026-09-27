"""Evaluate an inactive guest's SELinux file contexts on the worker (ADR-0691)."""

from __future__ import annotations

import ctypes
import errno
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kdive.providers.local_libvirt.lifecycle.boot.session import InactiveGuest

_CONFIG = "/etc/selinux/config"
_CONFIG_LIMIT = 64 * 1024
_SPEC_LIMIT = 32 * 1024 * 1024
_TOTAL_SPEC_LIMIT = 64 * 1024 * 1024
_SPEC_SUFFIXES = ("", ".local", ".homedirs", ".subs", ".subs_dist")
_POLICY_TYPE = re.compile(r"[A-Za-z0-9_-]+\Z")
_SELABEL_CTX_FILE = 0
_SELABEL_OPT_PATH = 3


class _SelabelOption(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("value", ctypes.c_char_p)]


class ModuleLabelPolicy:
    """One open libselinux handle bound to copied guest policy files."""

    def __init__(self, specfile: Path) -> None:
        try:
            lib = ctypes.CDLL("libselinux.so.1", use_errno=True)
        except OSError as exc:
            raise RuntimeError(
                "host libselinux is unavailable; install the local worker host prerequisites"
            ) from exc
        lib.selabel_open.argtypes = [ctypes.c_uint, ctypes.POINTER(_SelabelOption), ctypes.c_uint]
        lib.selabel_open.restype = ctypes.c_void_p
        lib.selabel_lookup_raw.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        lib.selabel_lookup_raw.restype = ctypes.c_int
        lib.selabel_close.argtypes = [ctypes.c_void_p]
        lib.freecon.argtypes = [ctypes.c_char_p]
        option = _SelabelOption(_SELABEL_OPT_PATH, str(specfile).encode())
        handle = lib.selabel_open(_SELABEL_CTX_FILE, ctypes.byref(option), 1)
        if not handle:
            raise OSError(ctypes.get_errno(), "cannot open guest SELinux file contexts")
        self._lib = lib
        self._handle = handle

    def label(self, path: str, mode: int) -> bytes:
        """Return the exact on-disk security.selinux value for one final guest path."""
        value = ctypes.c_char_p()
        if (
            self._lib.selabel_lookup_raw(self._handle, ctypes.byref(value), path.encode(), mode)
            != 0
        ):
            error = ctypes.get_errno()
            if error == errno.ENOENT:
                raise ValueError("guest SELinux policy has no context for a target module path")
            raise OSError(error, "guest SELinux context lookup failed")
        try:
            if not value.value:
                raise ValueError("guest SELinux policy returned an empty context")
            return value.value + b"\0"
        finally:
            self._lib.freecon(value)

    def close(self) -> None:
        self._lib.selabel_close(self._handle)


def _read_guest_file(guest: InactiveGuest, path: str, limit: int) -> bytes:
    info = guest.lstatns(path)
    size = info["st_size"]
    if not stat.S_ISREG(info["st_mode"]) or size < 0 or size > limit:
        raise ValueError("guest SELinux policy file is not a bounded regular file")
    with guest.open_regular(path, size=size) as content:
        data = content.read(limit + 1)
    if len(data) != size:
        raise ValueError("guest SELinux policy file changed during read")
    return data


def _config_values(data: bytes) -> dict[str, str]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("guest SELinux configuration is not UTF-8") from exc
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("guest SELinux configuration contains a malformed assignment")
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in {"SELINUX", "SELINUXTYPE"}:
            continue
        if key in values:
            raise ValueError("guest SELinux configuration contains a duplicate setting")
        values[key] = value.strip("\"'")
    return values


@contextmanager
def guest_policy(guest: InactiveGuest) -> Iterator[ModuleLabelPolicy | None]:
    """Copy a bounded, inactive guest policy and evaluate it with host libselinux."""
    if not guest.exists(_CONFIG):
        yield None
        return
    values = _config_values(_read_guest_file(guest, _CONFIG, _CONFIG_LIMIT))
    if values.get("SELINUX") == "disabled":
        yield None
        return
    policy_type = values.get("SELINUXTYPE", "")
    if values.get("SELINUX") not in {"enforcing", "permissive"} or not _POLICY_TYPE.fullmatch(
        policy_type
    ):
        raise ValueError("guest SELinux configuration has no valid enabled policy")
    guest_spec = f"/etc/selinux/{policy_type}/contexts/files/file_contexts"
    with tempfile.TemporaryDirectory(prefix="kdive-guest-policy-") as directory:
        specfile = Path(directory) / "file_contexts"
        total = 0
        for suffix in _SPEC_SUFFIXES:
            path = guest_spec + suffix
            if not suffix and not guest.exists(path):
                raise ValueError("guest SELinux policy file is missing")
            if suffix and not guest.exists(path):
                continue
            data = _read_guest_file(guest, path, _SPEC_LIMIT)
            total += len(data)
            if total > _TOTAL_SPEC_LIMIT:
                raise ValueError("guest SELinux policy exceeds the total byte bound")
            (specfile.parent / (specfile.name + suffix)).write_bytes(data)
        policy = ModuleLabelPolicy(specfile)
        try:
            yield policy
        finally:
            policy.close()
