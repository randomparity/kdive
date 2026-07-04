"""Cost-model unit tests — the kcu rate/cost math and input validation (ADR-0007 §1-2).

Pure, connection-free tests for the formula, the shared kcu quantizer, and the
fail-closed `validate_size`/`validate_window` guards. `resolve_coeff` (DB-backed) is
exercised in the DB-marked tests below and through `accounting.estimate`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from kdive.domain.accounting.cost import (
    KCU_QUANTUM,
    W_CPU,
    W_MEM,
    Selector,
    cost,
    quantize_kcu,
    rate,
    validate_against_resource,
    validate_disk_against_resource,
    validate_size,
    validate_window,
)
from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory

_DT = datetime(2026, 1, 1, tzinfo=UTC)


def _resource(capabilities: dict[str, Any], *, name: str | None = None) -> Resource:
    return Resource(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=ResourceKind.LOCAL_LIBVIRT,
        capabilities=capabilities,
        pool="local-libvirt",
        cost_class="local",
        status=ResourceStatus.AVAILABLE,
        host_uri="qemu:///system",
        name=name,
    )


def test_weights_match_adr() -> None:
    assert Decimal("1.0") == W_CPU
    assert Decimal("0.25") == W_MEM


def test_rate_matches_size_weighted_formula() -> None:
    # coeff=1.0, 2 vcpu, 4 GB → 1.0*(1.0*2 + 0.25*4) = 3.0 kcu/hr.
    assert rate(Decimal("1.0"), vcpus=2, memory_gb=4) == Decimal("3.0")


def test_rate_scales_with_coefficient() -> None:
    # A future cloud class (coeff 4.0) prices the same size 4x the local baseline.
    assert rate(Decimal("4.0"), vcpus=1, memory_gb=0) == Decimal("4.0")


def test_rate_is_exact_decimal_not_float() -> None:
    # 0.25 * 3 = 0.75 exactly; a float path would drift.
    assert rate(Decimal("1.0"), vcpus=0, memory_gb=3) == Decimal("0.75")


def test_cost_is_rate_times_hours() -> None:
    assert cost(Decimal("3.0"), Decimal("2")) == Decimal("6.0")


def test_cost_fractional_window() -> None:
    assert cost(Decimal("2.0"), Decimal("1.5")) == Decimal("3.00")


def test_quantize_rounds_half_even_to_quantum() -> None:
    assert Decimal("0.0001") == KCU_QUANTUM
    # Unbounded product is pinned to four places, banker's rounding.
    assert quantize_kcu(Decimal("0.123456789")) == Decimal("0.1235")
    assert quantize_kcu(Decimal("0.000050000")) == Decimal("0.0000")  # half to even
    assert quantize_kcu(Decimal("0.000150000")) == Decimal("0.0002")  # half to even


def test_quantize_too_large_fails_closed() -> None:
    # A value whose quantized form exceeds the default decimal precision would raise
    # InvalidOperation; quantize_kcu maps it to configuration_error instead of letting it
    # escape as an unhandled exception on the viewer-callable estimate tool.
    with pytest.raises(CategorizedError) as exc:
        quantize_kcu(Decimal("1e30"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # The offending value is echoed verbatim into the message and the structured details so
    # the caller can see which kcu product overflowed.
    assert exc.value.details == {"value": "1E+30"}
    assert str(exc.value) == "kcu value 1E+30 is too large to price"


def test_validate_size_accepts_minimum() -> None:
    validate_size(Selector(vcpus=1, memory_gb=0))


def test_validate_size_rejects_zero_vcpus() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_size(Selector(vcpus=0, memory_gb=1))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # The error names the offending field and value so an operator sees which input was
    # rejected; the message states the requirement.
    assert exc.value.details["field"] == "vcpus"
    assert exc.value.details["value"] == "0"
    assert str(exc.value) == "selector vcpus=0 must be ≥ 1"


def test_validate_size_rejects_negative_vcpus() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_size(Selector(vcpus=-1, memory_gb=1))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"field": "vcpus", "value": "-1"}


def test_validate_size_rejects_negative_memory() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_size(Selector(vcpus=1, memory_gb=-1))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # memory_gb is the offending field, carried verbatim with its value into details and
    # message — not the vcpus guard above it.
    assert exc.value.details == {"field": "memory_gb", "value": "-1"}
    assert str(exc.value) == "selector memory_gb=-1 must be ≥ 0"


def test_validate_size_accepts_int32_max_boundary() -> None:
    # The column-domain guard is a strict `>`: a selector exactly at INT32_MAX is the
    # largest admission could store, so it must be accepted (a `>=` guard would reject it).
    validate_size(Selector(vcpus=2_147_483_647, memory_gb=2_147_483_647))


def test_validate_size_rejects_over_column_domain() -> None:
    # requested_vcpus/memory_gb persist as Postgres `integer`; a value the read-side
    # estimate would price but admission could never store is rejected up front so the
    # two share one acceptance domain (challenge finding 3).
    with pytest.raises(CategorizedError) as exc:
        validate_size(Selector(vcpus=2_147_483_648, memory_gb=1))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"field": "vcpus", "value": "2147483648"}
    assert "must be ≤ 2147483647" in str(exc.value)


def test_validate_size_rejects_over_column_domain_memory() -> None:
    # The memory_gb upper-bound guard mirrors the vcpus one: a value past the integer
    # column ceiling names memory_gb (not vcpus) as the rejected field.
    with pytest.raises(CategorizedError) as exc:
        validate_size(Selector(vcpus=1, memory_gb=2_147_483_648))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"field": "memory_gb", "value": "2147483648"}
    assert "must be ≤ 2147483647" in str(exc.value)


def test_validate_window_accepts_positive() -> None:
    validate_window(Decimal("0.5"))


def test_validate_window_rejects_zero() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_window(Decimal("0"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # A non-positive (but finite) window fails the `> 0` guard, not the finiteness guard:
    # the rejected value and the positivity requirement are reported.
    assert exc.value.details == {"window": "0"}
    assert str(exc.value) == "window=0 must be > 0"


def test_validate_window_rejects_negative() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_window(Decimal("-1"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"window": "-1"}
    assert "must be > 0" in str(exc.value)


def test_validate_window_rejects_nan() -> None:
    # NaN <= 0 is False, so a naive sign check would let it through and yield a NaN
    # estimate; the guard must reject non-finite windows (challenge finding 3).
    with pytest.raises(CategorizedError) as exc:
        validate_window(Decimal("NaN"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # NaN is caught by the finiteness guard (which runs first), so the message states the
    # finite-number requirement and echoes the offending value.
    assert exc.value.details == {"window": "NaN"}
    assert str(exc.value) == "window=NaN must be a finite number"


def test_validate_window_rejects_infinity() -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_window(Decimal("Infinity"))
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"window": "Infinity"}
    assert "must be a finite number" in str(exc.value)


def test_validate_against_resource_accepts_within_caps() -> None:
    res = _resource({"vcpus": 4, "memory_mb": 8192})
    validate_against_resource(Selector(vcpus=4, memory_gb=8), res)  # exactly at the ceiling


def test_validate_against_resource_rejects_excess_vcpus() -> None:
    res = _resource({"vcpus": 2, "memory_mb": 8192})
    with pytest.raises(CategorizedError) as exc:
        validate_against_resource(Selector(vcpus=3, memory_gb=1), res)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # The caps error names vcpus, the requested count, and the host ceiling so the operator
    # can see exactly what was over-asked.
    assert exc.value.details == {"field": "vcpus", "requested": "3", "ceiling": "2"}
    assert f"resource {res.id} ceiling 2" in str(exc.value)


def test_validate_against_resource_rejects_excess_memory() -> None:
    res = _resource({"vcpus": 8, "memory_mb": 4096})  # 4 GB ceiling
    with pytest.raises(CategorizedError) as exc:
        validate_against_resource(Selector(vcpus=1, memory_gb=5), res)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # memory is reported in MB (5 GB → 5120 MB) against the 4096 MB ceiling.
    assert exc.value.details == {
        "field": "memory_mb",
        "requested": "5120",
        "ceiling": "4096",
    }
    assert "memory_mb=5120" in str(exc.value)


def test_validate_against_resource_memory_uses_1024_mb_per_gb() -> None:
    # 4096 MB ceiling = exactly 4 GB; a 4 GB selector fits, a fractional overflow is
    # impossible since memory_gb is integer — the boundary is exact, not 1000-based.
    res = _resource({"vcpus": 8, "memory_mb": 4096})
    validate_against_resource(Selector(vcpus=1, memory_gb=4), res)


@pytest.mark.parametrize("caps", [{}, {"vcpus": 4}, {"memory_mb": 4096}])
def test_validate_against_resource_missing_cap_fails_closed(caps: dict[str, Any]) -> None:
    res = _resource(caps)
    with pytest.raises(CategorizedError) as exc:
        validate_against_resource(Selector(vcpus=1, memory_gb=1), res)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    # An unnamed host labels the missing-ceiling error by its id so the operator can find
    # the unregistered host.
    assert str(res.id) in str(exc.value)


def test_validate_against_resource_missing_cap_labels_named_host() -> None:
    # A registered host name is preferred over the id in the missing-ceiling message so the
    # operator sees the human label, not just a UUID.
    res = _resource({}, name="builder-01")
    with pytest.raises(CategorizedError) as exc:
        validate_against_resource(Selector(vcpus=1, memory_gb=1), res)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "builder-01" in str(exc.value)


@pytest.mark.parametrize("bad", [None, "4", -1, True])
def test_validate_against_resource_invalid_cap_value_fails_closed(bad: object) -> None:
    res = _resource({"vcpus": bad, "memory_mb": 4096})
    with pytest.raises(CategorizedError) as exc:
        validate_against_resource(Selector(vcpus=1, memory_gb=1), res)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_validate_disk_at_ceiling_is_admitted() -> None:
    res = _resource({"vcpus": 4, "memory_mb": 8192, "disk_gb": 50})
    validate_disk_against_resource(50, res)  # exactly at the ceiling, no raise


def test_validate_disk_over_ceiling_is_configuration_error() -> None:
    res = _resource({"vcpus": 4, "memory_mb": 8192, "disk_gb": 50})
    with pytest.raises(CategorizedError) as exc:
        validate_disk_against_resource(51, res)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details == {"field": "disk_gb", "requested": "51", "ceiling": "50"}
    assert "disk_gb=51" in str(exc.value)


def test_validate_disk_none_skips_the_check() -> None:
    # A request that carries no disk (no ceiling to bound) never looks the ceiling up, so a
    # host without a disk ceiling does not fail a disk-less request.
    res = _resource({"vcpus": 4, "memory_mb": 8192})
    validate_disk_against_resource(None, res)  # no raise


def test_validate_disk_unadvertised_ceiling_is_unbounded() -> None:
    # A provider that sizes no disk from host storage (remote disk-image / fault-inject)
    # advertises no disk ceiling; a disk request to it is not bounded here (not a gap).
    res = _resource({"vcpus": 4, "memory_mb": 8192})  # no disk_gb advertised
    validate_disk_against_resource(10, res)  # no raise
