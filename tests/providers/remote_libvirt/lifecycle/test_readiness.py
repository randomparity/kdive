"""Unit tests for the build-VM network-readiness poll loop (ADR-0144) and the active
guest-ping agent-responsiveness gate (ADR-0168)."""

from __future__ import annotations

from collections.abc import Callable

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.readiness import (
    AGENT_READINESS_DETAIL_KEY,
    AGENT_UNRESPONSIVE,
    wait_for_agent,
    wait_for_agent_responsive,
    wait_for_network,
)
from tests.providers.remote_libvirt.conftest import libvirt_error

_CONNECTED_XML = (
    "<domain><devices><channel type='unix'>"
    "<target type='virtio' name='org.qemu.guest_agent.0' state='connected'/>"
    "</channel></devices></domain>"
)
_DISCONNECTED_XML = _CONNECTED_XML.replace("connected", "disconnected")


class _FakeDomain:
    """A minimal readiness Domain: scripted active flag, XML, and optional XMLDesc error."""

    def __init__(
        self,
        *,
        active: int = 1,
        xml: str = _CONNECTED_XML,
        xml_error: BaseException | None = None,
    ) -> None:
        self._active = active
        self._xml = xml
        self._xml_error = xml_error

    def isActive(self) -> int:  # noqa: N802 - libvirt binding name
        return self._active

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - libvirt binding name
        if self._xml_error is not None:
            raise self._xml_error
        return self._xml


class _FakeConn:
    def __init__(self, domain: _FakeDomain) -> None:
        self._domain = domain

    def lookupByName(self, name: str) -> _FakeDomain:  # noqa: N802 - libvirt binding name
        return self._domain


def _ticker(step: float = 1.0) -> Callable[[], float]:
    now = {"t": 0.0}

    def _monotonic() -> float:
        current = now["t"]
        now["t"] += step
        return current

    return _monotonic


def _sequence_probe(returns: list[bool]) -> Callable[[], bool]:
    calls = {"i": 0}

    def _probe() -> bool:
        value = returns[min(calls["i"], len(returns) - 1)]
        calls["i"] += 1
        return value

    return _probe


def test_returns_when_probe_true_on_first_call() -> None:
    wait_for_network(
        lambda: True,
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=lambda _s: None,
        timeout_s=10.0,
        poll_s=1.0,
    )


def test_polls_until_probe_flips_true() -> None:
    probe = _sequence_probe([False, False, True])
    wait_for_network(
        probe,
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=lambda _s: None,
        timeout_s=10.0,
        poll_s=1.0,
    )


def test_raises_provisioning_failure_past_deadline() -> None:
    sleeps: list[float] = []
    with pytest.raises(CategorizedError) as exc:
        wait_for_network(
            lambda: False,
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=sleeps.append,
            timeout_s=3.0,
            poll_s=0.25,
        )
    assert exc.value.category == ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details["domain"] == "kdive-build-x"
    assert exc.value.details["timeout_s"] == 3.0
    # The deadline is inclusive (>=): with a 1s tick and a 3s timeout it sleeps exactly
    # twice (t=1, t=2) and raises at t=3. A `>` boundary would sleep one extra time, and
    # each sleep must be the configured poll interval.
    assert sleeps == [0.25, 0.25]


def test_timeout_error_carries_timeout_detail_keys() -> None:
    def _detail() -> dict[str, object]:
        return {"probe_stderr": "cut: not found", "probe_stdout": ""}

    with pytest.raises(CategorizedError) as exc:
        wait_for_network(
            lambda: False,
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=3.0,
            poll_s=1.0,
            timeout_detail=_detail,
        )
    assert exc.value.details["probe_stderr"] == "cut: not found"


def test_propagates_categorized_error_raised_by_probe() -> None:
    def _broken_probe() -> bool:
        raise CategorizedError("agent gone", category=ErrorCategory.TRANSPORT_FAILURE)

    with pytest.raises(CategorizedError) as exc:
        wait_for_network(
            _broken_probe,
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=10.0,
            poll_s=1.0,
        )
    assert exc.value.category == ErrorCategory.TRANSPORT_FAILURE


# --- guest-ping agent-responsiveness gate (ADR-0168) --------------------------------


def _ping_agent(behaviours: list[BaseException | None]) -> Callable[..., str]:
    """A guest-ping agent fake: the i-th call raises ``behaviours[i]`` or returns a ping reply.

    The list is consumed; a shorter list than calls reuses its last element (so an
    always-raising agent is ``[exc]`` and an always-answering one is ``[None]``).
    """
    calls = {"i": 0}

    def _agent(domain: object, command: str, timeout: int, flags: int) -> str:
        index = min(calls["i"], len(behaviours) - 1)
        calls["i"] += 1
        outcome = behaviours[index]
        if outcome is not None:
            raise outcome
        return '{"return": {}}'

    return _agent


def test_agent_responsive_returns_when_first_ping_answers() -> None:
    wait_for_agent_responsive(
        _ping_agent([None]),
        object(),
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=lambda _s: None,
        timeout_s=10.0,
        poll_s=1.0,
    )


def test_agent_responsive_polls_past_transient_code_86_then_returns() -> None:
    # Code 86 during the readiness window is the mid-boot transient: keep polling, then succeed.
    agent = _ping_agent(
        [
            libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE),
            libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE),
            None,
        ]
    )
    wait_for_agent_responsive(
        agent,
        object(),
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=lambda _s: None,
        timeout_s=30.0,
        poll_s=1.0,
    )


class _NamedDomain:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


def test_agent_responsive_raises_immediately_on_deterministic_config_code() -> None:
    # "Agent not configured" cannot be cleared by polling: fail at once, non-retryable.
    agent = _ping_agent([libvirt_error(libvirt.VIR_ERR_ARGUMENT_UNSUPPORTED)])
    with pytest.raises(CategorizedError) as exc:
        wait_for_agent_responsive(
            agent,
            _NamedDomain("dom-deterministic"),
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=30.0,
            poll_s=1.0,
        )
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    # An immediate deterministic-config failure is NOT the unresponsive-readiness marker.
    assert AGENT_READINESS_DETAIL_KEY not in exc.value.details
    # The classifier was handed the real domain, so its details name it (not "<unknown>").
    assert exc.value.details["domain"] == "dom-deterministic"


def test_agent_responsive_deadline_raises_non_retryable_with_marker() -> None:
    # The agent never answers (always code 86): on the deadline, fail non-retryable with the
    # agent_readiness marker the diagnostic keys on.
    agent = _ping_agent([libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE)])
    sleeps: list[float] = []
    with pytest.raises(CategorizedError) as exc:
        wait_for_agent_responsive(
            agent,
            object(),
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=sleeps.append,
            timeout_s=3.0,
            poll_s=0.25,
        )
    assert exc.value.category == ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details[AGENT_READINESS_DETAIL_KEY] == AGENT_UNRESPONSIVE
    assert exc.value.details["domain"] == "kdive-build-x"
    assert exc.value.details["timeout_s"] == 3.0
    # Inclusive deadline (>=): two sleeps at the configured interval, then raise at t=3.
    assert sleeps == [0.25, 0.25]


def test_agent_responsive_pings_with_guest_ping_command_and_call_timeout() -> None:
    """The active gate sends the fixed guest-ping command at the configured call timeout."""
    seen: list[tuple[object, str, int, int]] = []

    def _agent(domain: object, command: str, timeout: int, flags: int) -> str:
        seen.append((domain, command, timeout, flags))
        return '{"return": {}}'

    sentinel = object()
    wait_for_agent_responsive(
        _agent,
        sentinel,
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=lambda _s: None,
        timeout_s=10.0,
        poll_s=1.0,
        call_timeout_s=9,
    )
    assert len(seen) == 1
    domain_arg, command, timeout, flags = seen[0]
    assert domain_arg is sentinel
    assert '"execute": "guest-ping"' in command
    assert timeout == 9
    assert flags == 0


def test_agent_responsive_sleeps_poll_interval_between_transient_pings() -> None:
    """A transient (code 86) ping sleeps exactly poll_s before retrying."""
    sleeps: list[float] = []
    agent = _ping_agent([libvirt_error(libvirt.VIR_ERR_AGENT_UNRESPONSIVE), None])
    wait_for_agent_responsive(
        agent,
        object(),
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=sleeps.append,
        timeout_s=30.0,
        poll_s=0.5,
    )
    assert sleeps == [0.5]


# --- XML guest-agent channel gate (wait_for_agent) ----------------------------------


def test_wait_for_agent_returns_once_channel_connected() -> None:
    domain = _FakeDomain(active=1, xml=_CONNECTED_XML)
    sleeps: list[float] = []
    wait_for_agent(
        _FakeConn(domain),
        "kdive-build-x",
        monotonic=_ticker(),
        sleep=sleeps.append,
        timeout_s=10.0,
        poll_s=0.25,
    )
    assert sleeps == []  # connected on the first poll, no sleep


def test_wait_for_agent_domain_exit_names_domain_in_details() -> None:
    domain = _FakeDomain(active=0)
    with pytest.raises(CategorizedError) as exc:
        wait_for_agent(
            _FakeConn(domain),
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=10.0,
            poll_s=0.25,
        )
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details == {"domain": "kdive-build-x"}


def test_wait_for_agent_deadline_carries_domain_and_timeout_details() -> None:
    domain = _FakeDomain(active=1, xml=_DISCONNECTED_XML)
    with pytest.raises(CategorizedError) as exc:
        wait_for_agent(
            _FakeConn(domain),
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=3.0,
            poll_s=1.0,
        )
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details == {"domain": "kdive-build-x", "timeout_s": 3.0}


def test_wait_for_agent_libvirt_error_maps_to_infra_with_domain_detail() -> None:
    domain = _FakeDomain(active=1, xml_error=libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
    with pytest.raises(CategorizedError) as exc:
        wait_for_agent(
            _FakeConn(domain),
            "kdive-build-x",
            monotonic=_ticker(),
            sleep=lambda _s: None,
            timeout_s=10.0,
            poll_s=0.25,
        )
    assert exc.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert exc.value.details == {"domain": "kdive-build-x"}
