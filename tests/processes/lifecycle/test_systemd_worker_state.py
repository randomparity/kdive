"""Durable fixed-path state for systemd worker slots."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import kdive.processes.lifecycle.systemd_worker_state as worker_state
from kdive.processes.lifecycle.systemd_worker_contract import LifecycleRequest, SlotPhase
from kdive.processes.lifecycle.systemd_worker_state import SlotState, SlotStore, StateConflict
from tests.processes.lifecycle.systemd_worker_support import start_payload


@pytest.fixture
def settings():
    return LifecycleRequest.model_validate(start_payload()).settings


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SlotStore:
    monkeypatch.setattr(worker_state.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        worker_state.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=1234, pw_gid=2345, pw_name=name),
    )
    monkeypatch.setattr(worker_state.os, "chown", lambda *_args: None)
    monkeypatch.setattr(worker_state.os, "fchown", lambda *_args: None)
    real_fstat = os.fstat

    def fstat(descriptor: int):
        metadata = real_fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            return SimpleNamespace(
                st_mode=metadata.st_mode,
                st_size=metadata.st_size,
                st_uid=0,
                st_gid=0 if target.name == "slots" else 2345,
                st_nlink=1,
            )
        return metadata

    monkeypatch.setattr(worker_state.os, "fstat", fstat)
    return SlotStore(root=tmp_path, slot=1)


@pytest.fixture
def started_state() -> SlotState:
    return SlotState(
        schema=1,
        slot=1,
        unit="kdive-live-worker@1.service",
        generation="a" * 32,
        incarnation="local-systemd:kdive-live-worker@1.service:" + "a" * 32,
        credential_hash="b" * 64,
        phase=SlotPhase.STARTED,
        boot_id="boot-id",
        invocation_id="invocation-id",
    )


def test_state_store_derives_every_slot_path(tmp_path: Path) -> None:
    store = SlotStore(root=tmp_path, slot=2)
    assert store.unit == "kdive-live-worker@2.service"
    assert store.state_path == tmp_path / "slots/2/state.json"
    assert store.credential_path == tmp_path / "slots/2/worker-incarnation.credential"


def test_prepare_writes_fixed_state_environment_and_credential(store: SlotStore, settings) -> None:
    state = store.prepare(settings)

    assert state.phase is SlotPhase.PREPARED
    assert state.incarnation == f"local-systemd:{store.unit}:{state.generation}"
    assert store.load() == state
    assert "KDIVE_WORKER_INCARNATION_ID" in store.environment_path.read_text(encoding="utf-8")
    assert store.credential_path.read_text(encoding="utf-8").strip()
    assert not store.release_path.exists()
    assert store.state_path.stat().st_mode & 0o777 == 0o600
    assert store.environment_path.stat().st_mode & 0o777 == 0o600
    assert store.credential_path.stat().st_mode & 0o777 == 0o400
    assert store.slot_path.stat().st_mode & 0o777 == 0o750


def test_persist_fsyncs_file_before_replace_and_directory_after(
    store: SlotStore, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = store.prepare(settings)
    gated = prepared.model_copy(
        update={"phase": SlotPhase.GATED, "boot_id": "boot-id", "invocation_id": "invocation-id"}
    )
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        events.append("replace")
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(worker_state.os, "fsync", fsync)
    monkeypatch.setattr(worker_state.os, "replace", replace)

    store.persist(gated)

    assert events == ["fsync", "replace", "fsync"]
    assert store.load() == gated


def test_publish_release_is_root_owned_and_contains_exact_binding(
    store: SlotStore, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = store.prepare(settings)
    gated = prepared.model_copy(
        update={"phase": SlotPhase.GATED, "boot_id": "boot-id", "invocation_id": "invocation-id"}
    )
    registered = gated.model_copy(update={"phase": SlotPhase.REGISTERED})
    store.persist(gated)
    store.persist(registered)
    ownership: list[tuple[int, int]] = []
    monkeypatch.setattr(
        worker_state.os, "fchown", lambda _fd, uid, gid: ownership.append((uid, gid))
    )

    store.publish_release(registered)

    assert store.release_path.read_text(encoding="utf-8") == (
        registered.generation + "\ninvocation-id\n"
    )
    assert store.release_path.stat().st_mode & 0o777 == 0o440
    assert ownership[-1] == (0, 2345)


def test_cleanup_requires_terminated_state(store: SlotStore, started_state: SlotState) -> None:
    with pytest.raises(StateConflict, match="terminated"):
        store.cleanup_terminated(started_state)


def test_cleanup_removes_only_the_fixed_terminated_generation(store: SlotStore, settings) -> None:
    prepared = store.prepare(settings)
    gated = prepared.model_copy(
        update={"phase": SlotPhase.GATED, "boot_id": "boot-id", "invocation_id": "invocation-id"}
    )
    registered = gated.model_copy(update={"phase": SlotPhase.REGISTERED})
    started = registered.model_copy(update={"phase": SlotPhase.STARTED})
    terminated = started.model_copy(update={"phase": SlotPhase.TERMINATED, "outcome": "killed"})
    store.persist(gated)
    store.persist(registered)
    store.publish_release(registered)
    store.persist(started)
    store.persist(terminated)

    store.cleanup_terminated(terminated)

    assert store.load() is None
    assert not store.environment_path.exists()
    assert not store.credential_path.exists()
    assert not store.release_path.exists()


def test_discard_prepared_removes_only_the_exact_unreleased_generation(
    store: SlotStore, settings
) -> None:
    prepared = store.prepare(settings)

    store.discard_prepared(prepared)

    assert store.load() is None
    assert not store.environment_path.exists()
    assert not store.credential_path.exists()
    assert not store.release_path.exists()


def test_discard_prepared_requires_prepared_phase(store: SlotStore, settings) -> None:
    prepared = store.prepare(settings)
    gated = prepared.model_copy(
        update={"phase": SlotPhase.GATED, "boot_id": "boot-id", "invocation_id": "invocation-id"}
    )
    store.persist(gated)

    with pytest.raises(StateConflict, match="prepared"):
        store.discard_prepared(gated)

    assert store.load() == gated


def test_discard_prepared_requires_the_exact_retained_state(store: SlotStore, settings) -> None:
    prepared = store.prepare(settings)
    generation = "f" * 32
    other = prepared.model_copy(
        update={
            "generation": generation,
            "incarnation": f"local-systemd:{store.unit}:{generation}",
        }
    )

    with pytest.raises(StateConflict, match="retained prepared"):
        store.discard_prepared(other)

    assert store.load() == prepared


def test_discard_prepared_refuses_a_release_marker(store: SlotStore, settings) -> None:
    prepared = store.prepare(settings)
    store.release_path.write_text("unexpected\n", encoding="ascii")
    store.release_path.chmod(0o440)

    with pytest.raises(StateConflict, match="release"):
        store.discard_prepared(prepared)

    assert store.load() == prepared
    assert store.environment_path.exists()
    assert store.credential_path.exists()
    assert store.release_path.exists()


def test_environment_rejects_newline_or_nul_values(store: SlotStore) -> None:
    payload = start_payload()
    settings = cast(dict[str, object], payload["settings"])
    settings["build_user"] = "builder\nother"

    with pytest.raises(ValueError, match="newlines or NUL"):
        store.prepare(LifecycleRequest.model_validate(payload).settings)


def test_persist_rejects_changed_credential_fence(store: SlotStore, settings) -> None:
    prepared = store.prepare(settings)
    gated = prepared.model_copy(
        update={"phase": SlotPhase.GATED, "boot_id": "boot-id", "invocation_id": "invocation-id"}
    )
    store.persist(gated)
    changed = gated.model_copy(update={"credential_hash": "c" * 64})

    with pytest.raises(StateConflict, match="immutable"):
        store.persist(changed)


def test_persist_rejects_illegal_phase_skip(store: SlotStore, settings) -> None:
    prepared = store.prepare(settings)
    skipped = prepared.model_copy(
        update={
            "phase": SlotPhase.REGISTERED,
            "boot_id": "boot-id",
            "invocation_id": "invocation-id",
        }
    )

    with pytest.raises(StateConflict, match="transition"):
        store.persist(skipped)


def test_registered_state_requires_complete_nonempty_binding() -> None:
    with pytest.raises(ValueError, match="boot and invocation"):
        SlotState(
            schema=1,
            slot=1,
            unit="kdive-live-worker@1.service",
            generation="a" * 32,
            incarnation="local-systemd:kdive-live-worker@1.service:" + "a" * 32,
            credential_hash="b" * 64,
            phase=SlotPhase.REGISTERED,
        )


def test_authority_binding_returns_the_exact_non_secret_registration() -> None:
    gated = SlotState(
        schema=1,
        slot=1,
        unit="kdive-live-worker@1.service",
        generation="a" * 32,
        incarnation="local-systemd:kdive-live-worker@1.service:" + "a" * 32,
        credential_hash="b" * 64,
        phase=SlotPhase.GATED,
        boot_id="01234567-89ab-cdef-0123-456789abcdef",
        invocation_id="c" * 32,
    )

    assert gated.authority_binding() == {
        "unit": "kdive-live-worker@1.service",
        "generation": "a" * 32,
        "boot_id": "01234567-89ab-cdef-0123-456789abcdef",
        "invocation_id": "c" * 32,
        "host": socket.gethostname(),
    }


def test_authority_binding_refuses_state_without_a_systemd_binding() -> None:
    prepared = SlotState(
        schema=1,
        slot=1,
        unit="kdive-live-worker@1.service",
        generation="a" * 32,
        incarnation="local-systemd:kdive-live-worker@1.service:" + "a" * 32,
        credential_hash="b" * 64,
        phase=SlotPhase.PREPARED,
    )

    with pytest.raises(StateConflict, match="registered state requires"):
        prepared.authority_binding()


def test_cleanup_keeps_terminal_state_when_ancillary_unlink_fails(
    store: SlotStore, settings
) -> None:
    prepared = store.prepare(settings)
    gated = prepared.model_copy(
        update={"phase": SlotPhase.GATED, "boot_id": "boot-id", "invocation_id": "invocation-id"}
    )
    registered = gated.model_copy(update={"phase": SlotPhase.REGISTERED})
    started = registered.model_copy(update={"phase": SlotPhase.STARTED})
    terminated = started.model_copy(update={"phase": SlotPhase.TERMINATED, "outcome": "killed"})
    for state in (gated, registered, started, terminated):
        store.persist(state)

    real_unlink = os.unlink

    def fail_credential(name: str, *, dir_fd: int) -> None:
        if name == "worker-incarnation.credential":
            raise OSError("simulated unlink failure")
        real_unlink(name, dir_fd=dir_fd)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(worker_state.os, "unlink", fail_credential)
        with pytest.raises(OSError, match="simulated"):
            store.cleanup_terminated(terminated)

    assert store.load() == terminated
    store.cleanup_terminated(terminated)
    assert store.load() is None


@pytest.mark.parametrize("failure", ["create", "write", "fsync", "replace"])
def test_failed_persist_cleans_up_temporary_state_file(
    store: SlotStore, settings, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    prepared = store.prepare(settings)
    gated = prepared.model_copy(
        update={"phase": SlotPhase.GATED, "boot_id": "boot-id", "invocation_id": "invocation-id"}
    )
    if failure == "create":
        real_open = os.open

        def fail_create(
            path: str | bytes | Path,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if str(path).startswith(".state.json."):
                raise OSError()
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(worker_state.os, "open", fail_create)
    elif failure == "write":
        monkeypatch.setattr(
            store, "_write_all", lambda _fd, _data: (_ for _ in ()).throw(OSError())
        )
    elif failure == "fsync":
        monkeypatch.setattr(worker_state.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError()))
    else:
        monkeypatch.setattr(
            worker_state.os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
        )

    with pytest.raises(OSError):
        store.persist(gated)

    assert not list(store.slot_path.glob(".state.json.*"))


def test_load_never_repairs_slot_metadata(
    store: SlotStore, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.prepare(settings)
    calls: list[str] = []
    monkeypatch.setattr(worker_state.os, "fchmod", lambda *_args: calls.append("chmod"))
    monkeypatch.setattr(worker_state.os, "fchown", lambda *_args: calls.append("chown"))

    store.load()

    assert calls == []


def test_load_refuses_fifo_without_blocking(
    store: SlotStore, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.prepare(settings)
    store.state_path.unlink()
    os.mkfifo(store.state_path)
    real_open = os.open

    def require_nonblocking(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "state.json" and not flags & os.O_NONBLOCK:
            raise AssertionError("state file was opened without O_NONBLOCK")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(worker_state.os, "open", require_nonblocking)

    with pytest.raises(StateConflict):
        store.load()


def test_first_slot_creation_fsyncs_each_parent_before_descending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings
) -> None:
    store = SlotStore(root=tmp_path, slot=1)
    events: list[str] = []
    real_mkdir = os.mkdir
    real_fsync = os.fsync
    monkeypatch.setattr(worker_state.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        worker_state.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=1234, pw_gid=2345, pw_name=name),
    )
    monkeypatch.setattr(worker_state.os, "fchown", lambda *_args: None)

    def mkdir(name: str, mode: int, *, dir_fd: int) -> None:
        events.append(f"mkdir:{name}")
        real_mkdir(name, mode, dir_fd=dir_fd)

    def fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    monkeypatch.setattr(worker_state.os, "mkdir", mkdir)
    monkeypatch.setattr(worker_state.os, "fsync", fsync)

    store.prepare(settings)

    assert events[:4] == ["mkdir:slots", "fsync", "mkdir:1", "fsync"]


def test_slots_directory_is_root_owned_traversal_only_for_all_worker_accounts(
    store: SlotStore, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ownership: list[tuple[int, int]] = []
    monkeypatch.setattr(
        worker_state.os, "fchown", lambda _fd, uid, gid: ownership.append((uid, gid))
    )

    store.prepare(settings)

    mode = store.slots_path.stat().st_mode & 0o777
    assert mode == 0o711
    assert mode & 0o040 == 0
    assert mode & 0o001
    assert ownership[0] == (0, 0)
    assert store.slot_path.stat().st_mode & 0o777 == 0o750


def test_observational_load_rejects_untrusted_slots_metadata(
    store: SlotStore, settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.prepare(settings)
    real_fstat = worker_state.os.fstat
    calls: list[str] = []

    def untrusted_slots(descriptor: int):
        metadata = real_fstat(descriptor)
        target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if target.name == "slots":
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o750,
                st_size=metadata.st_size,
                st_uid=0,
                st_gid=0,
                st_nlink=1,
            )
        return metadata

    monkeypatch.setattr(worker_state.os, "fstat", untrusted_slots)
    monkeypatch.setattr(worker_state.os, "fchmod", lambda *_args: calls.append("chmod"))
    monkeypatch.setattr(worker_state.os, "fchown", lambda *_args: calls.append("chown"))

    with pytest.raises(StateConflict, match="slots directory"):
        store.load()

    assert calls == []
