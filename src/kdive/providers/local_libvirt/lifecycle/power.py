"""Clean power-off of a local-libvirt domain, ``destroy`` as the fallback (ADR-0679, ADR-0681).

A guest killed by ``destroy`` loses writes still in its page cache (#2757). ``install.py`` (boot and
the module-injecting install) and the external-boot session (``boot/session.py``) both stop a
running domain through :func:`power_off`; each caller chooses the bound.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

import libvirt

from kdive.providers.local_libvirt.lifecycle.deadlines import tcg_deadline_multiplier

_log = logging.getLogger(__name__)

_CLEAN_SHUTDOWN_BASE_S = 60.0
_SHUTDOWN_POLL_S = 1.0
_SHUTDOWN_RESEND_S = 10.0
_HONOURS_SHUTDOWN = frozenset({libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_BLOCKED})


class PowerDomain(Protocol):
    def destroy(self) -> int: ...
    def shutdown(self) -> int: ...
    # The binding annotates ``state`` as ``str`` but returns ``[state, reason]``.
    def state(self, flags: int = 0) -> Sequence[object]: ...


def clean_shutdown_bound_s(accel: str | None) -> float:
    """The ADR-0679 wait: 60 s for KVM, scaled by ``tcg_deadline_multiplier`` otherwise."""
    return _CLEAN_SHUTDOWN_BASE_S * tcg_deadline_multiplier(accel)


def power_off(
    domain: PowerDomain,
    domain_name: str,
    bound_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Stop the domain, cleanly when the guest can honour a request, else by ``destroy``.

    A running guest is asked to shut down and given ``bound_s`` on ``clock`` to reach
    ``SHUTOFF``; the request is re-sent every 10 s in case the first arrived before the guest's
    handler was listening. A guest already shutting down is waited for without a request, which
    libvirt would refuse. A state that cannot honour a request (paused, crashed, suspended) is
    destroyed at once. The log line names the path taken.

    Raises:
        libvirt.libvirtError: from ``state()`` or ``destroy()``; the caller maps it.
    """
    state = domain.state()[0]
    if state == libvirt.VIR_DOMAIN_SHUTOFF:
        return
    stopping = state == libvirt.VIR_DOMAIN_SHUTDOWN
    if state not in _HONOURS_SHUTDOWN and not stopping:
        _log.warning("power-off %s: destroy-state (domain state %s)", domain_name, state)
        domain.destroy()
        return
    start = clock()
    requested_at: float | None = start if stopping else None
    while clock() - start < bound_s:
        if requested_at is None or clock() - requested_at >= _SHUTDOWN_RESEND_S:
            first = requested_at is None
            requested_at = clock()
            if not _request_shutdown(domain, domain_name, first=first):
                domain.destroy()
                return
        sleep(_SHUTDOWN_POLL_S)
        if domain.state()[0] == libvirt.VIR_DOMAIN_SHUTOFF:
            _log.info("power-off %s: clean after %.1f s", domain_name, clock() - start)
            return
    _log.warning("power-off %s: destroy-timeout after %.1f s", domain_name, clock() - start)
    domain.destroy()


def _request_shutdown(domain: PowerDomain, domain_name: str, *, first: bool) -> bool:
    """Send a shutdown request; only a refused first request is a failure."""
    try:
        domain.shutdown()
    except libvirt.libvirtError:
        if first:
            _log.warning("power-off %s: destroy-refused", domain_name, exc_info=True)
            return False
        _log.debug("power-off %s: shutdown re-send refused; still waiting", domain_name)
    return True
