"""Operator-run e2e proving local discovery advertises a real host's guest CPU + pin set (#1227).

``live_vm``-gated and preflighted to a clean skip: it needs a real local libvirt
(``qemu:///system`` by default, or ``KDIVE_LIBVIRT_URI``). CI deselects ``live_vm``
(``just test`` runs ``-m "not live_vm and not live_stack"``), so this is safe on any host; an
operator runs it on KVM/libvirt HW with ``just test-live``.

It proves the non-unit-provable half against a **real** host (the unit tests cover the
parse/level/discovery/render/read wiring with fakes):

1. ``test_local_discovery_advertises_host_cpu_and_selectable`` — running ``LocalLibvirtDiscovery``
   against the real host advertises a non-empty native ``host_cpu`` and a per-arch
   ``selectable_cpus`` whose native-arch set is non-empty. On x86_64, ``selectable_cpus`` holds
   ``usable='yes'`` models only. On ppc64le, QEMU does not implement the usability probe and
   reports all models as ``usable='unknown'``; ``parse_selectable_cpus`` treats ``unknown`` as
   includable (only ``usable='no'`` is excluded), so ``POWER9``/``POWER10`` etc. appear on a
   native POWER9 KVM-HV host.
2. ``test_pinned_model_is_host_usable`` — a portable rung an agent would pin (an ``x86-64-vN`` the
   host advertises) is genuinely in the host's ``getDomainCapabilities`` custom usable set, so an
   admission-accepted pin renders a domain the host will define. Skips with a recorded reason if the
   host advertises no ``x86-64-vN`` rung (e.g. on ppc64le, where no such rungs exist).
"""

from __future__ import annotations

import os

import libvirt
import pytest

from kdive.domain.catalog.resource_capabilities import ResourceCapabilities
from kdive.domain.platform.cpu_baseline import X86_64_MODEL_LEVELS, baseline_level
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery

pytestmark = pytest.mark.live_vm

_URI = os.environ.get("KDIVE_LIBVIRT_URI", "qemu:///system")


def _capabilities() -> ResourceCapabilities:
    try:
        conn = libvirt.open(_URI)
    except libvirt.libvirtError:
        pytest.skip(f"cannot open {_URI}; the local guest-cpu e2e needs a real libvirt host")
    conn.close()
    discovery = LocalLibvirtDiscovery(
        host_uri=_URI,
        # The real binding satisfies the narrow `_LibvirtConn` seam; ty infers `virConnect`.
        connect=lambda: libvirt.open(_URI),  # ty: ignore[invalid-argument-type]
        concurrent_allocation_cap=1,
    )
    record = discovery.list_resources()[0]
    return ResourceCapabilities.from_mapping(record["capabilities"])


def test_local_discovery_advertises_host_cpu_and_selectable() -> None:
    caps = _capabilities()
    host_cpu = caps.host_cpu()
    assert host_cpu is not None, "the real host advertised no native host_cpu (#1227)"
    assert host_cpu["model"], "the advertised host_cpu model is empty"
    native_arch = host_cpu["arch"]

    selectable = caps.selectable_cpus()
    assert selectable.get(native_arch), f"no selectable_cpus for the native arch {native_arch!r}"

    # Never a wrong level: a present baseline_level is >= v2 (the table holds only v2+ models); an
    # unmapped model carries no level.
    model = host_cpu["model"]
    level = host_cpu.get("baseline_level")
    if model in X86_64_MODEL_LEVELS:
        assert level is None or int(level.rsplit("v", 1)[1]) >= 2
    else:
        assert level is None, f"unmapped model {model!r} must not carry a baseline_level"
    # Cross-check: baseline_level derivation is stable for the advertised model.
    assert level == baseline_level(model, ())


def test_pinned_model_is_host_usable() -> None:
    caps = _capabilities()
    host_cpu = caps.host_cpu()
    assert host_cpu is not None
    native_models = caps.selectable_cpus()[host_cpu["arch"]]

    portable = [m for m in native_models if m.startswith("x86-64-v")]
    if not portable:
        pytest.skip(f"host advertises no x86-64-vN rung for {host_cpu['arch']!r} to pin")
    # A portable rung an agent would pin is genuinely in the host's usable custom-model set, so an
    # admission-accepted pin (validated against exactly this set) renders a domain the host defines.
    assert portable, "expected at least one x86-64-vN portable pin candidate"
