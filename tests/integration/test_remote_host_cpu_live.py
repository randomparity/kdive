"""Operator-run e2e proving remote discovery advertises a real host's guest CPU baseline (#980).

``live_vm``-gated and preflighted to a clean skip: it needs an operator-provided remote-libvirt
host (and, for the reconcile proof, a running domain provisioned through KDIVE's renderer). CI
deselects ``live_vm`` (``just test`` runs ``-m "not live_vm and not live_stack"``), so this is
safe on any host; an operator runs it on the remote-libvirt HW with ``just test-live``.

It proves the non-unit-provable half of ADR-0368 against a **real** host:

1. ``test_remote_host_advertises_host_cpu`` — the host's ``getDomainCapabilities`` host-model block
   parses to a non-empty model, and the derived ``baseline_level`` is either ≥ ``x86-64-v2`` or
   absent (never a wrong level). The unit tests already cover the parse/level/discovery wiring with
   a fake; this asserts a real host produces a valid advertisement.
2. ``test_host_cpu_matches_running_domain`` — the reconcile proof (ADR-0368, spec AC#11): the model
   discovery advertises equals the concrete ``<cpu><model>`` the running domain resolves under
   ``VIR_DOMAIN_XML_UPDATE_CPU``. This is the falsifiable check that the ``getDomainCapabilities``
   arguments predict the configuration the renderer built — a mispinned machine/virttype/arch fails
   it. On a host/libvirt that does not expand host-model to a concrete model it **skips with a
   recorded reason** rather than passing or failing silently.

Reuses the existing ``KDIVE_EL9_REACHABILITY_URI`` (a ``qemu+tls://`` remote host) and
``KDIVE_EL9_REACHABILITY_DOMAIN`` (a running remote domain name) env vars.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import libvirt
import pytest
from defusedxml.ElementTree import fromstring as safe_fromstring

from kdive.domain.platform.cpu_baseline import X86_64_MODEL_LEVELS, baseline_level
from kdive.providers.shared.libvirt_xml import parse_capabilities_arch, parse_host_cpu

pytestmark = pytest.mark.live_vm

_URI_ENV = "KDIVE_EL9_REACHABILITY_URI"
_DOMAIN_ENV = "KDIVE_EL9_REACHABILITY_DOMAIN"
_DEFAULT_MACHINE = "pc"  # KDIVE's remote renderer default (config.machine); ADR-0368


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set; the host-cpu discovery e2e needs an operator host")
    return value


def _level_digit(level: str) -> int:
    return int(level.rsplit("v", 1)[1])


def test_remote_host_advertises_host_cpu() -> None:
    uri = _require_env(_URI_ENV)
    conn = libvirt.open(uri)
    try:
        arch = parse_capabilities_arch(conn.getCapabilities())
        # emulatorbin=None is libvirt's "default emulator"; the binding stub mistypes it as `str`
        # (its own default is None), and an empty string makes libvirt stat an empty binary path.
        dom_caps = conn.getDomainCapabilities(
            None,  # ty: ignore[invalid-argument-type]
            arch,
            _DEFAULT_MACHINE,
            "kvm",
        )
    finally:
        conn.close()

    parsed = parse_host_cpu(dom_caps)
    assert parsed is not None, "the real host advertised no host-model CPU model (ADR-0368)"
    assert parsed.model, "the advertised host-model CPU model is empty"

    level = baseline_level(parsed.model, parsed.disabled_features)
    # Never a wrong level: a present level is ≥ v2 (the table holds only v2+ models); an unmapped
    # model — or one whose level-defining feature host-model disabled — carries no level.
    if parsed.model in X86_64_MODEL_LEVELS:
        assert level is None or _level_digit(level) >= 2
    else:
        assert level is None, f"unmapped model {parsed.model!r} must not carry a baseline_level"


def test_host_cpu_matches_running_domain() -> None:
    uri = _require_env(_URI_ENV)
    domain_name = _require_env(_DOMAIN_ENV)
    conn = libvirt.open(uri)
    try:
        arch = parse_capabilities_arch(conn.getCapabilities())
        domain = conn.lookupByName(domain_name)
        # Read the running domain's CPU with the expand flag so libvirt resolves host-model to a
        # concrete <model>; read the domain's own machine so the prediction is apples-to-apples.
        live_xml = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_UPDATE_CPU)
        live_root: ET.Element = safe_fromstring(live_xml)
        os_type = live_root.find("./os/type")
        machine_name = os_type.get("machine") if os_type is not None else None
        # emulatorbin=None is libvirt's "default emulator" (the stub mistypes it as `str`).
        dom_caps = conn.getDomainCapabilities(
            None,  # ty: ignore[invalid-argument-type]
            arch,
            machine_name or _DEFAULT_MACHINE,
            "kvm",
        )
    finally:
        conn.close()

    advertised = parse_host_cpu(dom_caps)
    assert advertised is not None, "the real host advertised no host-model CPU model (ADR-0368)"

    model_el = live_root.find("./cpu/model")
    model_text = (model_el.text or "").strip() if model_el is not None else ""
    if not model_text:
        pytest.skip(
            f"running domain {domain_name} does not expand host-model to a concrete <cpu><model>; "
            "the prediction-vs-reality reconcile is not applicable on this host/libvirt"
        )
    assert model_text == advertised.model, (
        f"advertised host_cpu.model {advertised.model!r} != running domain model {model_text!r} — "
        "the getDomainCapabilities args do not predict the built domain"
    )
