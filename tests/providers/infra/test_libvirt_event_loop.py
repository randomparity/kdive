"""Tests for the idempotent libvirt event-loop registration (ADR-0182)."""

from __future__ import annotations

from collections.abc import Callable

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
