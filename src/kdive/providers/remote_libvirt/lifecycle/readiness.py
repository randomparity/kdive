"""Remote-libvirt guest-agent readiness polling."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.guest.agent import (
    _DETERMINISTIC_CONFIG_CODES,
    AgentCommand,
    classify_agent_libvirt_error,
)
from kdive.providers.remote_libvirt.lifecycle.xml import agent_channel_connected_strict

type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]
type NetworkProbe = Callable[[], bool]
type TimeoutDetail = Callable[[], dict[str, object]]

# Detail-marker contract shared with the build-host agent diagnostic (ADR-0167/0168): the
# responsiveness gate's deadline error carries AGENT_READINESS_DETAIL_KEY=AGENT_UNRESPONSIVE so the
# diagnostic can tell "agent connected but never answered" (an agent/image FAIL) apart from a
# pool/base-image config error (a host ERROR). One source of truth — both the raise site here and
# the read site in diagnostics import these, so a literal cannot drift.
AGENT_READINESS_DETAIL_KEY = "agent_readiness"
AGENT_UNRESPONSIVE = "unresponsive"

# A single guest-ping round-trip is fast; bound each call so a wedged channel surfaces as a
# libvirtError the loop classifies instead of blocking the worker thread (never libvirt's -2).
_PING_CALL_TIMEOUT_S = 5
_GUEST_PING_COMMAND = json.dumps({"execute": "guest-ping"})


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


def wait_for_agent_responsive(
    agent_command: AgentCommand,
    domain: Any,
    domain_name: str,
    *,
    monotonic: Monotonic,
    sleep: Sleep,
    timeout_s: float,
    poll_s: float,
    call_timeout_s: int = _PING_CALL_TIMEOUT_S,
) -> None:
    """Poll ``guest-ping`` until the qemu-guest-agent answers a command (ADR-0168).

    The XML ``wait_for_agent`` gate only proves the guest opened the virtio-serial port, which
    happens before the agent daemon answers commands. This active gate closes that window so the
    build path does not exec into it. A returned ping means the agent answered; a ``libvirtError``
    whose code is a deterministic-config condition (agent not configured / denied / unsupported —
    the ADR-0159 base set, deliberately **without** ``AGENT_UNRESPONSIVE``) is raised at once
    because polling cannot clear it; any other ``libvirtError`` — including ``AGENT_UNRESPONSIVE``
    (the mid-boot transient) and a bare drop — means "not ready, keep polling".

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` (non-retryable) immediately for a
            deterministic-config code, or on the deadline for an agent that never answered. The
            deadline error carries ``AGENT_READINESS_DETAIL_KEY=AGENT_UNRESPONSIVE`` so the
            build-host diagnostic can surface it as an agent (not host) failure.
    """
    deadline = monotonic() + timeout_s
    while True:
        try:
            agent_command(domain, _GUEST_PING_COMMAND, call_timeout_s, 0)
            return
        except libvirt.libvirtError as exc:
            classified = classify_agent_libvirt_error(
                domain, exc, deterministic_codes=_DETERMINISTIC_CONFIG_CODES
            )
            if classified.category is ErrorCategory.CONFIGURATION_ERROR:
                raise classified from exc
            # Transient (incl. AGENT_UNRESPONSIVE / bare drop): keep polling until the deadline.
        if monotonic() >= deadline:
            raise CategorizedError(
                f"build VM guest agent did not become responsive within {timeout_s:g}s; "
                "the build image's qemu-guest-agent is not usable",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "domain": domain_name,
                    "timeout_s": timeout_s,
                    AGENT_READINESS_DETAIL_KEY: AGENT_UNRESPONSIVE,
                },
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
