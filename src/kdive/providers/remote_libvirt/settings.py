"""Operational ``KDIVE_REMOTE_LIBVIRT_*`` host knobs (ADR-0087, ADR-0112).

A dependency-light module (imports only :class:`Setting`). The connection identity (URI, TLS cert
refs, gdbstub address/range, base image, allocation cap) moved to the declarative ``systems.toml``
``[[remote_libvirt]]`` inventory instance (M2.6 Phase 3, #395); only the libvirt host topology
knobs the v2 model does not carry — storage pool, network, and QEMU machine type — remain env
settings here.
"""

from __future__ import annotations

from kdive.config.registry import Setting

_RT = frozenset({"worker", "reconciler"})
# The reaper connect gate is read wherever a reaper runs, which is both processes that run a
# reconcile pass: the reconciler on its loop, and the server through `ops.reconcile_now`, whose
# `ReconcileRepairPorts` builds the same dump-volume and infra reapers. Not the worker — it opens
# remote-libvirt connections too, but against one host its caller already selected and under its own
# job lease, so ADR-0565 leaves the worker planes' failure timing unchanged. `processes` does not
# gate resolution — the gate reads the value at call time either way — but declaring both
# makes `config validate` reject a malformed value at server startup instead of letting it raise
# from inside a fan-out, where `_enter_host` would log it as one more unreachable host every pass.
_REAPER_HOSTS = frozenset({"reconciler", "server"})


def _positive_int(raw: str) -> int:
    """Parse a duration that is meaningless at zero or below.

    Declared here rather than imported from ``core_settings`` to keep this module's dependency-light
    property: it imports :class:`Setting` and nothing else.
    """
    value = int(raw)
    if value < 1:
        raise ValueError(f"must be >= 1, got {value}")
    return value


REMOTE_LIBVIRT_STORAGE_POOL = Setting(
    name="KDIVE_REMOTE_LIBVIRT_STORAGE_POOL",
    parse=str,
    default="default",
    group="remote-libvirt",
    processes=_RT,
    help="libvirt storage pool for guest disks.",
)
REMOTE_LIBVIRT_NETWORK = Setting(
    name="KDIVE_REMOTE_LIBVIRT_NETWORK",
    parse=str,
    default="default",
    group="remote-libvirt",
    processes=_RT,
    help="libvirt network for guests.",
)
REMOTE_LIBVIRT_MACHINE = Setting(
    name="KDIVE_REMOTE_LIBVIRT_MACHINE",
    parse=str,
    default="pc",
    group="remote-libvirt",
    processes=_RT,
    help="QEMU machine type (pc/i440fx by default; q35 opt-in).",
)

REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS = Setting(
    name="KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS",
    parse=_positive_int,
    default="5",
    group="remote-libvirt",
    processes=_REAPER_HOSTS,
    help=(
        "Seconds a reconciler reaper waits for one libvirt host to accept a TCP connection before "
        "treating it as unreachable (ADR-0565). Measured on the reconciler's monotonic clock, "
        "shared by every address the host resolves to, per host per reaper connection attempt: a "
        "fan-out spends it once per unreachable host it walks, so the all-hosts-down worst case "
        "for one provider call is this value times the number of declared hosts. On violation that "
        "host is logged and skipped and the fan-out continues to the next declared host — the "
        "capture lane defers its row behind the usual backoff, the dump-volume lane leaves the "
        "volume for the next pass, and neither is counted as a fault. No caller recovery is "
        "needed; raise this for a slow-but-reachable fleet, or remove a down host from the "
        "declared inventory. Two things sit outside it: name resolution, because getaddrinfo takes "
        "no timeout (declare hosts by IP literal to remove that), and a host that accepts and then "
        "stalls, which only the lane budget caps (#1981). libvirt honours no connect-timeout URI "
        "parameter, which is why this is a separate probe."
    ),
    suggest="a positive integer number of seconds, e.g. 5",
)

SETTINGS = [
    REMOTE_LIBVIRT_STORAGE_POOL,
    REMOTE_LIBVIRT_NETWORK,
    REMOTE_LIBVIRT_MACHINE,
    REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS,
]
