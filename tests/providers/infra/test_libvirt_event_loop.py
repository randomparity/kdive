"""Tests for the idempotent libvirt event-loop registration (ADR-0182)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

import kdive.providers.infra.libvirt_event_loop as mod


def _reset() -> None:
    mod._STATE.registered = False  # test-only reset of the module guard


def test_registers_and_spawns_once_idempotent() -> None:
    _reset()
    registers: list[int] = []
    spawns: list[Callable[[], None]] = []

    def register() -> None:
        registers.append(1)

    def spawn(target: Callable[[], None]) -> None:
        spawns.append(target)

    mod.ensure_libvirt_event_loop(register=register, run=lambda: None, spawn=spawn)
    mod.ensure_libvirt_event_loop(register=register, run=lambda: None, spawn=spawn)

    assert registers == [1]  # registered exactly once
    assert len(spawns) == 1  # run-thread started exactly once
    _reset()


def test_run_thread_retries_on_error_until_stopped() -> None:
    # The run-thread must survive a transient libvirt error (log + back off), not die — a dead
    # loop silently stops all console capture. Drive the loop body directly (no real thread).
    calls = {"n": 0}

    def run() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient libvirt event error")
        raise mod._StopRunLoop  # test-only sentinel to end the loop deterministically

    mod._run_loop_body(run, sleep=lambda _s: None)
    assert calls["n"] == 2  # first call errored + retried, second ended the loop


def test_run_loop_body_backs_off_by_retry_constant() -> None:
    # The back-off after a transient error must be the configured duration, not an
    # arbitrary value: a wrong (e.g. None/zero) back-off either crashes the loop or
    # busy-spins.
    slept: list[float] = []
    calls = {"n": 0}

    def run() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        raise mod._StopRunLoop

    mod._run_loop_body(run, sleep=slept.append)
    assert slept == [mod._RETRY_BACKOFF_S]


def test_spawned_target_drives_the_run_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # ensure_libvirt_event_loop must hand spawn a callable that, when invoked, runs the
    # real event-loop body against the injected ``run`` (and the module's ``time.sleep``
    # for back-off). Invoking the captured target exercises that wiring.
    _reset()
    captured: list[Callable[[], None]] = []
    run_calls = {"n": 0}
    slept: list[float] = []

    monkeypatch.setattr(mod.time, "sleep", slept.append)

    def run() -> None:
        run_calls["n"] += 1
        if run_calls["n"] == 1:
            raise RuntimeError("transient")
        raise mod._StopRunLoop

    mod.ensure_libvirt_event_loop(register=lambda: None, run=run, spawn=captured.append)

    assert len(captured) == 1
    target = captured[0]
    target()  # would raise if run/sleep were not wired through correctly

    assert run_calls["n"] == 2  # the injected run was driven, errored once, then stopped
    assert slept == [mod._RETRY_BACKOFF_S]  # back-off used module time.sleep
    _reset()
