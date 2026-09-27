"""Guest-policy evaluation for the local external-boot target (ADR-0691)."""

from __future__ import annotations

import io
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from kdive.providers.local_libvirt.lifecycle.boot.selinux_policy import (
    ModuleLabelPolicy,
    guest_policy,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import InactiveGuest

_SPEC = (
    b"/lib/modules(/.*)? system_u:object_r:modules_object_t:s0\n"
    b"/lib/modules/[^/]+/modules\\..* -- system_u:object_r:modules_dep_t:s0\n"
)
_SPEC_PATH = "/etc/selinux/targeted/contexts/files/file_contexts"


class _Guest:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def exists(self, path: str) -> int:
        return int(path in self.files)

    def lstatns(self, path: str) -> dict[str, int]:
        return {"st_mode": stat.S_IFREG | 0o644, "st_size": len(self.files[path])}

    @contextmanager
    def open_regular(self, path: str, *, size: int) -> Iterator[BinaryIO]:
        assert len(self.files[path]) == size
        yield io.BytesIO(self.files[path])


def test_guest_policy_uses_final_path_and_file_type() -> None:
    guest = _Guest(
        {
            "/etc/selinux/config": b"SELINUX=permissive\nSELINUXTYPE=targeted\n",
            _SPEC_PATH: _SPEC,
        }
    )
    with guest_policy(cast(InactiveGuest, guest)) as policy:
        assert policy is not None
        assert policy.label("/lib/modules/6.12/kernel/a.ko", stat.S_IFREG | 0o644) == (
            b"system_u:object_r:modules_object_t:s0\0"
        )
        assert policy.label("/lib/modules/6.12/modules.dep", stat.S_IFREG | 0o644) == (
            b"system_u:object_r:modules_dep_t:s0\0"
        )
        assert policy.label("/lib/modules/6.12/kernel", stat.S_IFDIR | 0o755).endswith(b"\0")


def test_guest_policy_keeps_local_context_overrides() -> None:
    guest = _Guest(
        {
            "/etc/selinux/config": b"SELINUX=enforcing\nSELINUXTYPE=targeted\n",
            _SPEC_PATH: _SPEC,
            _SPEC_PATH + ".local": (
                b"/lib/modules/[^/]+/kernel/a\\.ko -- system_u:object_r:custom_module_t:s0\n"
            ),
        }
    )
    with guest_policy(cast(InactiveGuest, guest)) as policy:
        assert policy is not None
        assert policy.label("/lib/modules/6.12/kernel/a.ko", stat.S_IFREG | 0o644) == (
            b"system_u:object_r:custom_module_t:s0\0"
        )


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"/etc/selinux/config": b"SELINUX=disabled\n"},
    ],
)
def test_guest_policy_is_absent_for_non_selinux_guests(files: dict[str, bytes]) -> None:
    with guest_policy(cast(InactiveGuest, _Guest(files))) as policy:
        assert policy is None


@pytest.mark.parametrize(
    "files, message",
    [
        (
            {"/etc/selinux/config": b"SELINUX=permissive\nSELINUXTYPE=targeted\n"},
            "missing",
        ),
        ({"/etc/selinux/config": b"SELINUX=permissive\nSELINUXTYPE=../bad\n"}, "valid"),
        ({"/etc/selinux/config": b"SELINUX=permissive\nSELINUX=disabled\n"}, "duplicate"),
    ],
)
def test_guest_policy_fails_closed_on_invalid_enabled_policy(
    files: dict[str, bytes], message: str
) -> None:
    with pytest.raises(ValueError, match=message), guest_policy(cast(InactiveGuest, _Guest(files))):
        pass


def test_guest_policy_rejects_an_unmatched_target(tmp_path: Path) -> None:
    specfile = tmp_path / "file_contexts"
    specfile.write_bytes(b"/var/lib(/.*)? system_u:object_r:var_lib_t:s0\n")
    policy = ModuleLabelPolicy(specfile)
    try:
        with pytest.raises(ValueError, match="no context"):
            policy.label("/lib/modules/6.12/kernel/a.ko", stat.S_IFREG | 0o644)
    finally:
        policy.close()


def test_guest_policy_rejects_oversized_config() -> None:
    guest = _Guest({"/etc/selinux/config": b"x" * (64 * 1024 + 1)})
    with pytest.raises(ValueError, match="bounded"), guest_policy(cast(InactiveGuest, guest)):
        pass


def test_missing_host_library_is_actionable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    specfile = tmp_path / "file_contexts"
    specfile.write_bytes(_SPEC)

    def missing_library(_name: str, *, use_errno: bool) -> object:
        del use_errno
        raise OSError("missing library")

    monkeypatch.setattr("ctypes.CDLL", missing_library)
    with pytest.raises(RuntimeError, match="install the local worker host prerequisites"):
        ModuleLabelPolicy(specfile)
