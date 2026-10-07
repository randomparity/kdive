"""Layout-driven staging, publication and recovery of the guest DWARF vmlinux (#3130)."""

from __future__ import annotations

import hashlib
import posixpath
import stat
from typing import cast

import pytest

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    GuestDebuginfoFile,
    debuginfo_file_state,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import InactiveGuest
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ComponentState,
    ExternalBootActivationBinding,
    PresentComponentState,
)

_BINDING = ExternalBootActivationBinding(
    system_id="11111111-1111-1111-1111-111111111111",
    run_id="22222222-2222-2222-2222-222222222222",
    activation_id="33333333-3333-3333-3333-333333333333",
)
_RELEASE = "7.0.0-kdive"
_DIRECTORY = f"/usr/lib/debug/lib/modules/{_RELEASE}"
_LIVE = f"{_DIRECTORY}/vmlinux"
_STAGING = f"{_DIRECTORY}/.kdive-{_BINDING.activation_id}-vmlinux-staging"
_OLD = f"{_DIRECTORY}/.kdive-{_BINDING.activation_id}-vmlinux-old"
_NEW = b"new dwarf vmlinux"
_PRIOR = b"prior vmlinux from another install"
_STANDARD_DIRECTORIES = ("/", "/usr", "/usr/lib", "/usr/lib/debug", "/usr/lib/debug/lib")


class _File:
    def __init__(self, data: bytes, mode: int = 0o644, uid: int = 0, gid: int = 0) -> None:
        self.data, self.mode, self.uid, self.gid = data, mode, uid, gid


class _Guest:
    """An in-memory guest filesystem with directories, regular files and symlinks."""

    def __init__(self) -> None:
        self.directories = set(_STANDARD_DIRECTORIES)
        self.files: dict[str, _File] = {}
        self.symlinks: dict[str, str] = {}
        self.mutations: list[tuple[str, ...]] = []
        self.fail_upload = False

    def _resolve(self, path: str, *, follow_last: bool = True) -> str:
        parts = [part for part in path.split("/") if part]
        resolved = "/"
        for index, part in enumerate(parts):
            candidate = posixpath.join(resolved, part)
            last = index == len(parts) - 1
            while candidate in self.symlinks and (follow_last or not last):
                candidate = posixpath.normpath(
                    posixpath.join(posixpath.dirname(candidate), self.symlinks[candidate])
                )
            resolved = candidate
        return resolved

    def exists(self, path: str) -> int:
        resolved = self._resolve(path)
        return int(resolved in self.directories or resolved in self.files)

    def is_dir(self, path: str, *, followsymlinks: bool) -> int:
        return int(self._resolve(path, follow_last=followsymlinks) in self.directories)

    def lstatns(self, path: str) -> dict[str, int]:
        resolved = self._resolve(path, follow_last=False)
        if resolved in self.symlinks:
            return {"st_mode": stat.S_IFLNK | 0o777, "st_size": 1, "st_uid": 0, "st_gid": 0}
        file = self.files[resolved]
        return {
            "st_mode": stat.S_IFREG | file.mode,
            "st_size": len(file.data),
            "st_uid": file.uid,
            "st_gid": file.gid,
        }

    def checksum(self, csumtype: str, path: str) -> str:
        assert csumtype == "sha256"
        return hashlib.sha256(self.files[self._resolve(path)].data).hexdigest()

    def mkdir(self, path: str) -> None:
        resolved = self._resolve(path)
        assert posixpath.dirname(resolved) in self.directories
        self.mutations.append(("mkdir", path))
        self.directories.add(resolved)

    def upload(self, data: bytes, path: str) -> None:
        self.mutations.append(("upload", path))
        if self.fail_upload:
            raise RuntimeError("write: No space left on device")
        self.files[self._resolve(path)] = _File(data, mode=0o600, uid=7, gid=7)

    def chmod(self, mode: int, path: str) -> None:
        self.mutations.append(("chmod", path))
        self.files[self._resolve(path)].mode = mode

    def chown(self, owner: int, group: int, path: str) -> None:
        self.mutations.append(("chown", path))
        file = self.files[self._resolve(path)]
        file.uid, file.gid = owner, group

    def mv(self, source: str, destination: str) -> None:
        self.mutations.append(("mv", source, destination))
        self.files[self._resolve(destination)] = self.files.pop(self._resolve(source))

    def rm_rf(self, path: str) -> None:
        self.mutations.append(("rm", path))
        self.files.pop(self._resolve(path), None)

    def sync(self) -> None:
        self.mutations.append(("sync",))

    def with_release_directory(self) -> _Guest:
        self.directories.update({"/usr/lib/debug/lib/modules", _DIRECTORY})
        return self


def _state(data: bytes, mode: int = 0o644, uid: int = 0, gid: int = 0) -> PresentComponentState:
    return debuginfo_file_state(
        size=len(data),
        sha256="sha256:" + hashlib.sha256(data).hexdigest(),
        mode=mode,
        uid=uid,
        gid=gid,
    )


_TARGET = _state(_NEW)
_PRIOR_STATE = _state(_PRIOR, mode=0o600, uid=5, gid=6)
_ABSENT = AbsentComponentState()


def _file(guest: _Guest) -> GuestDebuginfoFile:
    return GuestDebuginfoFile(cast(InactiveGuest, guest), binding=_BINDING, release=_RELEASE)


def _upload(guest: _Guest):
    return lambda path: guest.upload(_NEW, path)


def _names(guest: _Guest) -> dict[str, bytes]:
    return {path: file.data for path, file in sorted(guest.files.items())}


def _with_prior(guest: _Guest) -> _Guest:
    guest.with_release_directory().files[_LIVE] = _File(_PRIOR, mode=0o600, uid=5, gid=6)
    return guest


def _activate(guest: _Guest, source: ComponentState) -> None:
    file = _file(guest)
    file.stage(source, _TARGET, _upload(guest))
    file.publish(source, _TARGET)


def test_activation_creates_directories_and_publishes_over_an_absent_file() -> None:
    guest = _Guest()

    assert _file(guest).observe_live() == _ABSENT
    _activate(guest, _ABSENT)

    assert _names(guest) == {_LIVE: _NEW}
    assert ("mkdir", _DIRECTORY) in guest.mutations
    live = guest.files[_LIVE]
    assert (live.mode, live.uid, live.gid) == (0o644, 0, 0)
    assert _file(guest).observe_live() == _TARGET


def test_activation_keeps_the_prior_file_aside_and_recovery_renames_it_back() -> None:
    guest = _with_prior(_Guest())
    assert _file(guest).observe_live() == _PRIOR_STATE

    _activate(guest, _PRIOR_STATE)
    assert _names(guest) == {_OLD: _PRIOR, _LIVE: _NEW}

    _file(guest).restore(_PRIOR_STATE, _TARGET)
    assert _names(guest) == {_LIVE: _PRIOR}
    restored = guest.files[_LIVE]
    assert (restored.mode, restored.uid, restored.gid) == (0o600, 5, 6)


def test_recovery_removes_a_file_that_was_absent_before_activation() -> None:
    guest = _Guest()
    _activate(guest, _ABSENT)

    _file(guest).restore(_ABSENT, _TARGET)

    assert _names(guest) == {}


def test_staging_is_a_no_op_once_the_target_is_staged() -> None:
    guest = _with_prior(_Guest())
    guest.files[_STAGING] = _File(_NEW)

    _activate(guest, _PRIOR_STATE)

    assert _names(guest) == {_OLD: _PRIOR, _LIVE: _NEW}
    assert ("upload", _STAGING) not in guest.mutations


@pytest.mark.parametrize(
    "files",
    [
        {_LIVE: _PRIOR, _STAGING: _NEW},
        {_STAGING: _NEW, _OLD: _PRIOR},
        {_LIVE: _NEW, _OLD: _PRIOR},
    ],
    ids=["staged", "prior-aside", "published"],
)
def test_publication_resumes_from_each_restart_layout(files: dict[str, bytes]) -> None:
    # After the record leaves pre-stop-intent, activation only publishes (#3130).
    guest = _Guest().with_release_directory()
    for path, data in files.items():
        guest.files[path] = _File(data) if data == _NEW else _File(data, mode=0o600, uid=5, gid=6)

    _file(guest).publish(_PRIOR_STATE, _TARGET)

    assert _names(guest) == {_OLD: _PRIOR, _LIVE: _NEW}
    assert ("upload", _STAGING) not in guest.mutations


@pytest.mark.parametrize(
    "files",
    [
        {_LIVE: _PRIOR, _STAGING: _NEW},
        {_LIVE: _PRIOR, _STAGING: b"partial"},
        {_STAGING: _NEW, _OLD: _PRIOR},
        {_LIVE: _NEW, _OLD: _PRIOR},
        {_LIVE: _PRIOR},
    ],
    ids=["staged", "partial-staging", "prior-aside", "published", "terminal"],
)
def test_recovery_restores_the_prior_file_from_each_restart_layout(
    files: dict[str, bytes],
) -> None:
    guest = _Guest().with_release_directory()
    for path, data in files.items():
        guest.files[path] = _File(data) if data != _PRIOR else _File(data, mode=0o600, uid=5, gid=6)

    _file(guest).restore(_PRIOR_STATE, _TARGET)

    assert _names(guest) == {_LIVE: _PRIOR}


def test_recovery_removes_staging_left_over_an_absent_file() -> None:
    guest = _Guest().with_release_directory()
    guest.files[_STAGING] = _File(b"partial")

    _file(guest).restore(_ABSENT, _TARGET)

    assert _names(guest) == {}


def test_staging_replaces_a_partial_upload() -> None:
    guest = _with_prior(_Guest())
    guest.files[_STAGING] = _File(b"partial")

    _activate(guest, _PRIOR_STATE)

    assert ("rm", _STAGING) in guest.mutations
    assert _names(guest) == {_OLD: _PRIOR, _LIVE: _NEW}


def test_a_prior_identical_to_the_target_is_kept_aside_and_restored() -> None:
    guest = _Guest().with_release_directory()
    guest.files[_LIVE] = _File(_NEW)

    _activate(guest, _TARGET)
    assert _names(guest) == {_OLD: _NEW, _LIVE: _NEW}
    _file(guest).restore(_TARGET, _TARGET)

    assert _names(guest) == {_LIVE: _NEW}


def test_a_distro_symlink_above_the_release_directory_is_followed() -> None:
    # Fedora, RHEL and Rocky ship /usr/lib/debug/lib -> usr/lib in the filesystem package.
    guest = _Guest()
    guest.directories.discard("/usr/lib/debug/lib")
    guest.directories.add("/usr/lib/debug/usr")
    guest.directories.add("/usr/lib/debug/usr/lib")
    guest.symlinks["/usr/lib/debug/lib"] = "usr/lib"

    _activate(guest, _ABSENT)
    assert _names(guest) == {f"/usr/lib/debug/usr/lib/modules/{_RELEASE}/vmlinux": _NEW}
    _file(guest).restore(_ABSENT, _TARGET)

    assert _names(guest) == {}


def test_a_failed_upload_leaves_the_live_name_untouched_and_recovery_settles() -> None:
    guest = _with_prior(_Guest())
    guest.fail_upload = True

    with pytest.raises(RuntimeError, match="No space"):
        _file(guest).stage(_PRIOR_STATE, _TARGET, _upload(guest))
    _file(guest).restore(_PRIOR_STATE, _TARGET)

    assert _names(guest) == {_LIVE: _PRIOR}


def test_a_staged_file_that_does_not_read_back_as_the_target_stops_before_any_move() -> None:
    guest = _with_prior(_Guest())

    with pytest.raises(ValueError, match="does not match the materialized vmlinux"):
        _file(guest).stage(
            _PRIOR_STATE, _TARGET, lambda path: guest.upload(b"corrupted in transit", path)
        )

    assert not any(mutation[0] == "mv" for mutation in guest.mutations)
    assert guest.files[_LIVE].data == _PRIOR


def _no_mutation(guest: _Guest, action) -> None:
    before = (_names(guest), list(guest.mutations))
    with pytest.raises(ValueError, match="conflict|not a directory|not a regular file"):
        action()
    assert (_names(guest), guest.mutations) == before


def test_a_live_file_changed_by_the_guest_is_a_conflict_for_every_step() -> None:
    guest = _Guest()
    _activate(guest, _ABSENT)
    guest.files[_LIVE] = _File(b"edited by guest root")
    file = _file(guest)

    _no_mutation(guest, lambda: file.publish(_ABSENT, _TARGET))
    _no_mutation(guest, lambda: file.restore(_ABSENT, _TARGET))


def test_staging_refuses_when_the_live_name_is_not_the_recorded_prior() -> None:
    guest = _with_prior(_Guest())
    file = _file(guest)

    _no_mutation(guest, lambda: file.stage(_ABSENT, _TARGET, _upload(guest)))


def test_publication_refuses_when_staging_disappeared() -> None:
    guest = _with_prior(_Guest())

    _no_mutation(guest, lambda: _file(guest).publish(_PRIOR_STATE, _TARGET))


def test_a_symlink_at_the_live_name_is_refused() -> None:
    guest = _Guest().with_release_directory()
    guest.files["/etc/passwd"] = _File(b"root")
    guest.symlinks[_LIVE] = "/etc/passwd"

    _no_mutation(guest, lambda: _file(guest).observe_live())


def test_a_symlinked_release_directory_is_refused() -> None:
    guest = _Guest()
    guest.directories.update({"/usr/lib/debug/lib/modules", "/elsewhere"})
    guest.symlinks[_DIRECTORY] = "/elsewhere"

    _no_mutation(guest, lambda: _file(guest).stage(_ABSENT, _TARGET, _upload(guest)))


def test_the_release_must_be_one_path_segment() -> None:
    with pytest.raises(ValueError, match="release is invalid"):
        GuestDebuginfoFile(cast(InactiveGuest, _Guest()), binding=_BINDING, release="../etc")
