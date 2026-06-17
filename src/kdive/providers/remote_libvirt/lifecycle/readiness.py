"""Remote-libvirt guest-agent readiness polling."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.xml import agent_channel_connected_strict

type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]
type NetworkProbe = Callable[[], bool]
type TimeoutDetail = Callable[[], dict[str, object]]


class Domain(Protocol):
    """The domain slice readiness polling uses."""

    def isActive(self) -> int: ...  # noqa: N802
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802


class ReadinessConn(Protocol):
    """The connection slice readiness polling uses."""

    def lookupByName(self, name: str) -> Domain: ...  # noqa: N802


def wait_for_agent(
    conn: ReadinessConn,
    domain_name: str,
    *,
    monotonic: Monotonic,
    sleep: Sleep,
    timeout_s: float,
    poll_s: float,
) -> None:
    """Poll the live XML until the guest-agent channel reports connected."""
    deadline = monotonic() + timeout_s
    while True:
        try:
            domain = conn.lookupByName(domain_name)
            running = bool(domain.isActive())
            connected = running and agent_channel_connected_strict(
                domain.XMLDesc(),
                operation="polling the guest-agent channel",
                domain=domain_name,
            )
        except libvirt.libvirtError as exc:
            raise _infra("polling the guest-agent channel", domain=domain_name) from exc
        if connected:
            return
        if not running:
            raise CategorizedError(
                "domain exited during boot before the guest agent connected",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"domain": domain_name},
            )
        if monotonic() >= deadline:
            raise CategorizedError(
                f"guest agent did not connect within {timeout_s:g}s",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"domain": domain_name, "timeout_s": timeout_s},
            )
        sleep(poll_s)


def wait_for_network(
    probe: NetworkProbe,
    domain_name: str,
    *,
    monotonic: Monotonic,
    sleep: Sleep,
    timeout_s: float,
    poll_s: float,
    timeout_detail: TimeoutDetail | None = None,
) -> None:
    """Poll an in-guest network-readiness probe until it succeeds or the deadline passes.

    A ``False`` from *probe* means "not ready, keep polling"; a ``CategorizedError`` raised by
    *probe* (the agent dropped) propagates unchanged. On deadline, raise ``PROVISIONING_FAILURE``
    merged with ``timeout_detail()`` (the last probe output) so a broken probe is diagnosable
    rather than a bare timeout (ADR-0144).
    """
    deadline = monotonic() + timeout_s
    while True:
        if probe():
            return
        if monotonic() >= deadline:
            details: dict[str, object] = {"domain": domain_name, "timeout_s": timeout_s}
            if timeout_detail is not None:
                details.update(timeout_detail())
            raise CategorizedError(
                f"guest network did not come up within {timeout_s:g}s",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details=details,
            )
        sleep(poll_s)


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )
