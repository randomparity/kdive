"""Fault-inject resource capability keys (ADR-0072).

The fault-inject resource's ``seed``, per-plane ``fault_rate`` map, per-plane
``max_latency_s`` bound, and ``secret_ref`` are **keys in the existing
``resources.capabilities`` jsonb** — no new columns (migration 0018 only widens the
``resources_kind_check`` CHECK). Discovery writes them; the seeded fault engine and forced
secret resolution read them.

The fault planes match the typed provider ports the engine can perturb. ``fault_rate``
and ``max_latency_s`` are per-plane maps so a test can raise one plane's rate without
perturbing the others; an absent plane defaults to no fault / no delay.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from kdive.domain.errors import CategorizedError

SEED_KEY: Final = "seed"
FAULT_RATE_KEY: Final = "fault_rate"
MAX_LATENCY_S_KEY: Final = "max_latency_s"
SECRET_REF_KEY: Final = "secret_ref"
# Hot-plug behaviour (ADR-0823): ``busy``, ``slow`` or ``hang``; absent means un-plug and plug
# settle at once. ``HotplugBehaviour.from_capabilities`` parses it; like the fault engine's keys,
# the runtime reads it from the resource row at each binding (ADR-0074 amendment, #3716).
HOTPLUG_BEHAVIOUR_KEY: Final = "hotplug_behaviour"


def validate_capabilities(capabilities: Mapping[str, object]) -> None:
    """Reject a malformed fault-inject key, naming the key and its value.

    The one check binding and the reconcile merge share. Each key is parsed by the same code the
    runtime uses, one at a time, so a falsy malformed value (``0``, ``False``, ``""``, ``[]``)
    and a ``seed`` on a row with no fault rates are rejected like any other.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` naming the first malformed key and its value.
    """
    # The engine and hot-plug modules import the key names above, so they load lazily here.
    from kdive.providers.fault_inject.faulting.engine import FaultEngine
    from kdive.providers.fault_inject.lifecycle.hotplug import HotplugBehaviour

    parsers = (
        (SEED_KEY, FaultEngine.from_capabilities),
        (FAULT_RATE_KEY, FaultEngine.from_capabilities),
        (MAX_LATENCY_S_KEY, FaultEngine.from_capabilities),
        (HOTPLUG_BEHAVIOUR_KEY, HotplugBehaviour.from_capabilities),
    )
    for key, parse in parsers:
        if key not in capabilities:
            continue
        try:
            parse({key: capabilities[key]})
        except CategorizedError as exc:
            raise CategorizedError(
                f"fault-inject capability {key}={capabilities[key]!r} is invalid: {exc}",
                category=exc.category,
            ) from exc
