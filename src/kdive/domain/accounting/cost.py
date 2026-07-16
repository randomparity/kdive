"""The kcu cost model: size-weighted rate, time-scaled cost, fail-closed coeff (ADR-0007 §1-2).

Cost is **size × time** in a dimensionless reference unit (the kcu), so a local-VM Run
and a future cloud Run sum on one axis:

``rate(kcu/hr) = coeff(cost_class) × (W_CPU × vcpus + W_MEM × memory_gb)`` and
``cost(kcu) = rate × hours``. ``W_CPU``/``W_MEM`` are global reference weights pinned by
the ADR (one vcpu-hour ≈ four GB-hours); ``coeff`` is the only per-class number,
resolved from ``cost_class_coefficients`` and **failing closed** (`configuration_error`)
on a missing row — a class with no coefficient is never "free".

All arithmetic uses :class:`~decimal.Decimal` so kcu values stay exact, and every
kcu value the system records or reports passes through :func:`quantize_kcu` — one shared
quantizer so an estimate and the ledger ``reserved``/``reconciled`` deltas that price
the same selector cannot drift by rounding.

:func:`validate_size` / :func:`validate_window` are the fail-closed input guards used by
**both** ``accounting.estimate`` and admission: a ``vcpus < 1``, ``memory_gb < 0``, or
``window ≤ 0`` (or a non-finite / out-of-column-domain) input is rejected as
``configuration_error`` so ``rate`` and ``estimate`` are always ``≥ 0`` — a negative-size
or negative-window request cannot mint budget via a negative ``reserved`` row (ADR-0007
§2). The ``≤ resource-caps`` check is admission-only (it needs a chosen Resource) and
lives with the admission gate, not here.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException, InvalidOperation
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.sizing import MB_PER_GB

if TYPE_CHECKING:
    from psycopg import AsyncConnection

    from kdive.domain.catalog.resources import Resource

# Resource-capabilities keys advertising the host's billable size ceiling (the discovery
# provider populates them). A selector may not exceed these — the admission-only
# ≤ resource-caps check (ADR-0007 §2): you cannot be billed for more than the host has.

# Global reference weights (ADR-0007 §1): one vcpu-hour costs 1.0 kcu, one GB-hour 0.25.
W_CPU = Decimal("1.0")
W_MEM = Decimal("0.25")

# Global accelerator reference weights (ADR-0362, amending ADR-0007 §1). Pinned and
# fleet-uniform like W_CPU/W_MEM: a property of the accelerator technology, not a per-host
# knob. `kvm` is the native baseline; `tcg` (full emulation of a foreign guest arch) reserves
# host compute at a large multiple of native, priced at 4× (the same reference scale as the
# "one vcpu-hour ≈ four GB-hours" ratio). A `None`/unknown accel fails OPEN to the native
# baseline — a resource that advertises no `guest_arches` (remote-libvirt, fault-inject, a host
# not re-discovered since ADR-0338) resolves no accel and is priced exactly as before ADR-0362.
ACCEL_WEIGHT = {
    "kvm": Decimal("1.0"),
    "tcg": Decimal("4.0"),
}
_ACCEL_BASELINE = Decimal("1.0")

# Every recorded/reported kcu value quantizes to this scale with banker's rounding, so
# estimate / reserve / reconcile that price the same selector agree to the last place.
KCU_QUANTUM = Decimal("0.0001")

# requested_vcpus / requested_memory_gb persist as Postgres `integer`; the read-side
# estimate rejects anything admission could not store so they share one acceptance domain.
_INT32_MAX = 2**31 - 1


class Selector(BaseModel):
    """The desired size (and cost class) a request or estimate prices.

    ``vcpus`` and ``memory_gb`` are the rate inputs; ``cost_class`` selects the
    coefficient. ``accounting.estimate`` prices a hypothetical selector with no target
    host, so the class is carried here (defaulting to the local baseline) rather than
    read from a Resource — admission instead resolves the class from the chosen Resource.
    """

    model_config = ConfigDict(extra="forbid")

    vcpus: int
    memory_gb: int
    cost_class: str = "local"
    accel: str | None = None


def quantize_kcu(value: Decimal) -> Decimal:
    """Quantize a kcu value to :data:`KCU_QUANTUM` with banker's rounding.

    The single rounding point for every kcu the system records or reports, so the
    estimate and the ledger deltas that price one selector cannot diverge by a rounding
    rule (ADR-0007 §2).

    ``validate_window`` deliberately has no upper bound (clamping is admission-only), so a
    read-side estimate can price an arbitrarily large finite window. A product whose
    quantized form would exceed the default decimal precision (``> ~24`` integer digits)
    would raise :class:`~decimal.InvalidOperation`; that is mapped to
    ``configuration_error`` so the value-too-large case fails closed in-category rather
    than escaping as an unhandled exception on a ``viewer``-callable tool.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``value`` is too large to quantize.
    """
    try:
        return value.quantize(KCU_QUANTUM)
    except InvalidOperation:
        raise CategorizedError(
            f"kcu value {value} is too large to price",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"value": str(value)},
        ) from None


def accel_factor(accel: str | None) -> Decimal:
    """Return the pinned kcu rate multiplier for an accelerator (ADR-0362).

    ``kvm`` is the native baseline (``1.0``); ``tcg`` prices at :data:`ACCEL_WEIGHT`'s
    emulation multiplier. A ``None`` or unknown accelerator fails OPEN to the native
    baseline (``1.0``) — a resource that advertises no ``guest_arches`` resolves no accel and
    must price exactly as before ADR-0362, never fail closed on a stale/hand-edited value.
    """
    if accel is None:
        return _ACCEL_BASELINE
    return ACCEL_WEIGHT.get(accel, _ACCEL_BASELINE)


def rate(coeff: Decimal, *, vcpus: int, memory_gb: int, accel: str | None = None) -> Decimal:
    """Return the exact (unquantized) kcu/hr rate for ``coeff``, a size, and an accelerator.

    ``rate = coeff × A(accel) × (W_CPU × vcpus + W_MEM × memory_gb)`` (ADR-0007 §1 as amended
    by ADR-0362). ``accel`` defaults to ``None`` — a ``1.0`` factor — so a caller that does not
    price an accelerator gets the pre-ADR-0362 rate byte-identically. Exact so callers quantize
    once at the reporting/recording boundary; never rounds here.
    """
    return coeff * accel_factor(accel) * (W_CPU * vcpus + W_MEM * memory_gb)


def cost(rate_kcu_per_hr: Decimal, hours: Decimal) -> Decimal:
    return rate_kcu_per_hr * hours


def parse_window_hours(window: object) -> Decimal:
    """Parse a request ``window`` into a positive number of hours (Decimal).

    The wire ``window`` is a number of hours; it is carried as :class:`~decimal.Decimal`
    so the estimate and the admission reservation that price the same window agree
    exactly. A value that is not a finite number (``None``, a non-numeric string, ``NaN``,
    ``Infinity``) is a ``configuration_error`` — the same fail-closed discipline as
    :func:`validate_window`, applied at the wire boundary.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``window`` is not a finite number.
    """
    try:
        parsed = Decimal(str(window))
    except (InvalidOperation, DecimalException, ValueError, TypeError) as _exc:
        raise CategorizedError(
            f"window {window!r} is not a number",
            category=ErrorCategory.CONFIGURATION_ERROR,
        ) from None
    return parsed


def validate_size(selector: Selector) -> None:
    """Reject a selector that would price a negative or unstorable rate (fail closed).

    Rejects ``vcpus < 1``, ``memory_gb < 0``, and any size outside the persisted
    ``integer`` column domain, so ``rate ≥ 0`` and the read-side estimate never accepts a
    size admission could not store (ADR-0007 §2).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for any rejected size.
    """
    if selector.vcpus < 1:
        raise _size_error("vcpus", selector.vcpus, "must be ≥ 1")
    if selector.memory_gb < 0:
        raise _size_error("memory_gb", selector.memory_gb, "must be ≥ 0")
    if selector.vcpus > _INT32_MAX:
        raise _size_error("vcpus", selector.vcpus, f"must be ≤ {_INT32_MAX}")
    if selector.memory_gb > _INT32_MAX:
        raise _size_error("memory_gb", selector.memory_gb, f"must be ≤ {_INT32_MAX}")


def validate_window(window: Decimal) -> None:
    """Reject a non-positive or non-finite window (fail closed).

    Guards ``window ≤ 0`` **and** ``NaN``/``Infinity`` — ``NaN ≤ 0`` is ``False``, so a
    naive sign check would let a ``NaN`` window through and yield a ``NaN`` estimate that
    the budget compare mishandles (ADR-0007 §2).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``window`` is not a finite ``> 0``.
    """
    if not window.is_finite():
        raise _window_error(window, "must be a finite number")
    if window <= 0:
        raise _window_error(window, "must be > 0")


def validate_against_resource(selector: Selector, resource: Resource) -> None:
    """Reject a selector that exceeds the chosen Resource's advertised size (fail closed).

    The admission-only ≤ resource-caps check (ADR-0007 §2): ``accounting.estimate`` has
    no target host, so this lives off the read path. The Resource advertises its host's
    ``vcpus`` (count) and ``memory_mb`` ceiling under ``capabilities``; a selector asking
    for more than the host has — or a host that advertises no valid ceiling — is a
    ``configuration_error``, never silently admitted (you cannot be billed for more
    capacity than exists).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the resource has no valid
            ``vcpus`` / ``memory_mb`` capability, or the selector exceeds either.
    """
    cap_vcpus, cap_memory_mb = resource.capability_view.require_size_ceiling(
        resource_id=resource.id, resource_name=resource.name
    )
    if selector.vcpus > cap_vcpus:
        raise _caps_error("vcpus", selector.vcpus, cap_vcpus, resource)
    requested_mb = selector.memory_gb * MB_PER_GB
    if requested_mb > cap_memory_mb:
        raise _caps_error("memory_mb", requested_mb, cap_memory_mb, resource)


def validate_disk_against_resource(disk_gb: int | None, resource: Resource) -> None:
    """Reject a disk request exceeding the chosen Resource's advertised disk ceiling.

    The admission-only ≤ resource-caps check for disk (ADR-0007 §2, ADR-0312). disk is not a
    kcu input, so it is validated here beside the priced selector rather than on it.

    Enforced only where a ceiling is advertised: local-libvirt always advertises one
    (live-derived from host storage at discovery), so a local request is always bounded; a
    provider that sizes no disk from host storage (remote-libvirt's disk-image, fault-inject)
    advertises none and is not bounded here — unlike vcpus/memory, an absent disk ceiling is
    "this provider does not allocate host disk", not a registration gap. ``disk_gb`` is ``None``
    only for a request that carries no disk (the ADR-0067 shape-XOR-triple rule makes a sized
    request always carry one).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``disk_gb`` exceeds an advertised ceiling.
    """
    if disk_gb is None:
        return
    ceiling = resource.capability_view.disk_ceiling()
    if ceiling is None:
        return
    if disk_gb > ceiling:
        raise _caps_error("disk_gb", disk_gb, ceiling, resource)


def _caps_error(field: str, requested: int, ceiling: int, resource: Resource) -> CategorizedError:
    return CategorizedError(
        f"selector {field}={requested} exceeds resource {resource.id} ceiling {ceiling}",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"field": field, "requested": str(requested), "ceiling": str(ceiling)},
    )


async def resolve_coeff(conn: AsyncConnection, cost_class: str) -> Decimal:
    """Resolve the coefficient for ``cost_class`` from ``cost_class_coefficients``.

    Fails closed: a class with no row is a ``configuration_error``, never "free"
    (ADR-0007 §1). Reads the persisted class, never request data.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``cost_class`` has no coefficient row.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT coeff FROM cost_class_coefficients WHERE cost_class = %s", (cost_class,)
        )
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError(
            f"cost_class {cost_class!r} has no coefficient row",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"cost_class": cost_class},
        )
    return Decimal(row[0])


def _size_error(field: str, value: int, requirement: str) -> CategorizedError:
    return CategorizedError(
        f"selector {field}={value} {requirement}",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"field": field, "value": str(value)},
    )


def _window_error(window: Decimal, requirement: str) -> CategorizedError:
    return CategorizedError(
        f"window={window} {requirement}",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"window": str(window)},
    )
