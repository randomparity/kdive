"""Durable fixed-path state for systemd worker slots."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import kdive.processes.lifecycle.systemd_worker_state as worker_state
from kdive.processes.lifecycle.systemd_worker_contract import LifecycleRequest, SlotPhase
from kdive.processes.lifecycle.systemd_worker_state import SlotState, SlotStore, StateConflict
from tests.processes.lifecycle.test_systemd_worker_contract import start_payload


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
    store: SlotStore, started_state: SlotState, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.prepare(LifecycleRequest.model_validate(start_payload()).settings)
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

    store.persist(started_state)

    assert events == ["fsync", "replace", "fsync"]
    assert store.load() == started_state


def test_publish_release_is_root_owned_and_contains_exact_binding(
    store: SlotStore, started_state: SlotState, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.persist(started_state.model_copy(update={"phase": SlotPhase.REGISTERED}))
    ownership: list[tuple[int, int]] = []
    monkeypatch.setattr(
        worker_state.os, "fchown", lambda _fd, uid, gid: ownership.append((uid, gid))
    )

    store.publish_release(started_state.model_copy(update={"phase": SlotPhase.REGISTERED}))

    assert store.release_path.read_text(encoding="utf-8") == "a" * 32 + "\ninvocation-id\n"
    assert store.release_path.stat().st_mode & 0o777 == 0o440
    assert ownership[-1] == (0, 2345)


def test_cleanup_requires_terminated_state(store: SlotStore, started_state: SlotState) -> None:
    with pytest.raises(StateConflict, match="terminated"):
        store.cleanup_terminated(started_state)


def test_cleanup_removes_only_the_fixed_terminated_generation(
    store: SlotStore, started_state: SlotState
) -> None:
    registered = started_state.model_copy(update={"phase": SlotPhase.REGISTERED})
    store.persist(registered)
    store.publish_release(registered)
    terminated = started_state.model_copy(
        update={"phase": SlotPhase.TERMINATED, "outcome": "killed"}
    )
    store.persist(terminated)

    store.cleanup_terminated(terminated)

    assert store.load() is None
    assert not store.environment_path.exists()
    assert not store.credential_path.exists()
    assert not store.release_path.exists()


def test_environment_rejects_newline_or_nul_values(store: SlotStore) -> None:
    payload = start_payload()
    settings = cast(dict[str, object], payload["settings"])
    settings["build_user"] = "builder\nother"

    with pytest.raises(ValueError, match="newlines or NUL"):
        store.prepare(LifecycleRequest.model_validate(payload).settings)
