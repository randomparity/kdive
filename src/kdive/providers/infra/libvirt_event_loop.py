"""Process-wide libvirt event loop for the reconciler (ADR-0182).

A libvirt non-blocking stream's incoming buffer is filled by libvirt's event loop. The remote
console collector polls such a stream, so the reconciler must register the default event-loop
implementation and run it for the process lifetime, or every console capture would-blocks forever
and persists 0 bytes. Registration is idempotent; the run-thread is durable (a transient error is
logged and retried, not fatal) and observable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import libvirt

_log = logging.getLogger(__name__)

# Back-off between run-loop iterations after a transient error, so a persistent fault logs and
# retries rather than busy-spinning.
_RETRY_BACKOFF_S = 1.0


class _StopRunLoop(Exception):
    """Test-only sentinel to end the run loop deterministically (never raised in production)."""


@dataclass
class _State:
    registered: bool = False


_STATE = _State()
_LOCK = threading.Lock()


def _run_loop_body(run: Callable[[], object], *, sleep: Callable[[float], None]) -> None:
    """Run libvirt's event loop forever; log + retry a transient error instead of dying.

    ``run`` normally blocks (``virEventRunDefaultImpl`` polls until the next event/timeout), so
    the loop does not busy-spin. A raised error would otherwise kill the thread and silently stop
    all console capture, so it is logged and retried after a short back-off.
    """
    while True:
        try:
            run()
        except _StopRunLoop:
            return
        except Exception:  # noqa: BLE001 - a dead loop silently stops all console capture
            _log.warning("libvirt event loop iteration failed; retrying", exc_info=True)
            sleep(_RETRY_BACKOFF_S)


def _default_spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="libvirt-event-loop", daemon=True).start()


def ensure_libvirt_event_loop(
    *,
    register: Callable[[], object] = libvirt.virEventRegisterDefaultImpl,
    run: Callable[[], object] = libvirt.virEventRunDefaultImpl,
    spawn: Callable[[Callable[[], None]], None] = _default_spawn,
) -> None:
    """Register libvirt's default event loop and start its run-thread, once per process.

    Must be called before any libvirt connection whose stream events matter is opened — libvirt
    services only connections opened after registration (ADR-0182). Idempotent: a second call is a
    no-op. The ``register``/``run``/``spawn`` seams are injectable for tests.
    """
    with _LOCK:
        if _STATE.registered:
            return
        register()
        _STATE.registered = True
    spawn(lambda: _run_loop_body(run, sleep=time.sleep))
    _log.info("libvirt event loop registered and running")
