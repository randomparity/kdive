"""Operator-run e2e proving an EL9 remote guest boots past init with the host-model CPU (#975).

``live_vm``-gated and preflighted to a clean skip: it needs an operator-provided, **running**
remote-libvirt domain that was provisioned through KDIVE's remote renderer (ADR-0297) from an
EL9/RHEL-family base image and has a connected qemu-guest-agent. CI deselects ``live_vm``
(``just test`` runs ``-m "not live_vm and not live_stack"``), so this is safe on any host; an
operator runs it on the two-host remote-libvirt HW with ``just test-live``.

It proves the non-unit-provable half of ADR-0297 against a **real** EL9 domain. The unit tests
already assert both remote renderers emit ``<cpu mode='host-model'>``; this asserts the fix
survives all the way to the **live** defined domain and that an EL9 guest actually reaches
userspace with it:

1. The live domain XML (``XMLDesc``) carries ``<cpu mode='host-model'>`` — libvirt kept the
   element the renderer emitted, so the guest was started with a v2-capable CPU, not the
   ``qemu64`` (x86-64-v1) default that aborts EL9 glibc at PID 1.
2. The qemu-guest-agent answers a trivial command — the agent is a userspace daemon, so a
   response is only possible if the guest cleared the glibc x86-64-v2 barrier and booted past
   init. Under the pre-fix ``qemu64`` default an EL9 guest panics before the agent starts, so
   this drain would fail. It is read-only: the test creates and tears down nothing (the
   operator owns the domain lifecycle), mirroring the #966 SSH-parity e2e.

Required env: ``KDIVE_EL9_REACHABILITY_DOMAIN`` (a running, agent-ready EL9 remote domain name)
and ``KDIVE_EL9_REACHABILITY_URI`` (its ``qemu+tls://`` connect URI).
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import libvirt
import pytest
from defusedxml.ElementTree import fromstring as safe_fromstring

from kdive.providers.remote_libvirt.guest.agent import GuestAgentExec, qemu_agent_command

pytestmark = pytest.mark.live_vm

_DOMAIN_ENV = "KDIVE_EL9_REACHABILITY_DOMAIN"
_URI_ENV = "KDIVE_EL9_REACHABILITY_URI"
_SHELL = "/bin/sh"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set; the EL9 CPU-reachability e2e needs an operator guest")
    return value


def test_el9_remote_guest_boots_past_init_with_host_model_cpu() -> None:
    domain_name = _require_env(_DOMAIN_ENV)
    uri = _require_env(_URI_ENV)

    conn = libvirt.open(uri)
    try:
        domain = conn.lookupByName(domain_name)
        live_xml = domain.XMLDesc(0)
        root: ET.Element = safe_fromstring(live_xml)
        cpu = root.find("./cpu")
        assert cpu is not None, f"live domain {domain_name} carries no <cpu> element (ADR-0297)"
        assert cpu.get("mode") == "host-model", (
            f"live domain {domain_name} <cpu> mode is {cpu.get('mode')!r}, not 'host-model'"
        )
        # Guest-agent liveness == the guest reached userspace. An EL9 guest on the pre-fix
        # x86-64-v1 default panics at PID 1 before the agent starts, so a clean exit here is the
        # load-bearing "booted past the glibc x86-64-v2 barrier" proof.
        agent = GuestAgentExec(
            agent_command=qemu_agent_command, allowed_programs=frozenset({_SHELL})
        )
        result = agent.run(domain, [_SHELL, "-c", "true"])
    finally:
        conn.close()

    assert result.exit_status == 0, (
        f"EL9 guest-agent on {domain_name} did not answer (exit={result.exit_status}); "
        "the guest may not have booted past init"
    )
