"""The worker's accepted dispatch lanes (#1538, ADR-0550)."""

from __future__ import annotations

import pytest

import kdive.config as config
from kdive.config.core_settings import WORKER_ACCEPTED_LANES
from kdive.domain.errors import CategorizedError
from kdive.domain.operations.jobs import (
    ACTIVE_JOB_KINDS,
    DEFAULT_JOB_DISPATCH_LANE,
    STATE_FENCED_JOB_DISPATCH_LANE,
    dispatch_lane_for_kind,
)


def test_the_default_accepts_every_lane_a_kind_routes_to() -> None:
    """S5, the starvation guard.

    A lane no deployed worker accepts means those jobs never run and the object they fence
    stays fenced, with nothing to surface it — ``repair_abandoned_jobs`` reaps only ``running``
    rows. The default accepting every routed lane makes that unreachable by construction; this
    test is what keeps it that way when a fourth lane is added.
    """
    config.load({})
    accepted = set(config.require(WORKER_ACCEPTED_LANES))
    routed = {dispatch_lane_for_kind(kind) for kind in ACTIVE_JOB_KINDS}
    assert routed <= accepted, f"lanes with no default consumer: {sorted(routed - accepted)}"


def test_the_default_names_both_known_lanes() -> None:
    config.load({})
    assert config.require(WORKER_ACCEPTED_LANES) == (
        DEFAULT_JOB_DISPATCH_LANE,
        STATE_FENCED_JOB_DISPATCH_LANE,
    )


def test_an_explicit_single_lane_narrows_the_worker() -> None:
    # Supported and deliberate: it restores the pre-ADR-0550 resource footprint, at the cost of
    # starving the omitted lane. The worker warns at startup rather than refusing to start.
    config.load({WORKER_ACCEPTED_LANES.name: "state-fenced"})
    assert config.require(WORKER_ACCEPTED_LANES) == (STATE_FENCED_JOB_DISPATCH_LANE,)


def test_surrounding_whitespace_is_tolerated() -> None:
    config.load({WORKER_ACCEPTED_LANES.name: " default , state-fenced "})
    assert config.require(WORKER_ACCEPTED_LANES) == (
        DEFAULT_JOB_DISPATCH_LANE,
        STATE_FENCED_JOB_DISPATCH_LANE,
    )


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "empty"),
        ("   ", "whitespace only"),
        ("default,,state-fenced", "blank entry"),
        (",", "blank entries only"),
        ("state_fenced", "underscore typo for the fenced lane"),
        ("default,provider-a", "unknown lane alongside a known one"),
    ],
)
def test_a_malformed_or_unknown_lane_set_is_rejected(raw: str, reason: str) -> None:
    """S8. An unknown lane is rejected rather than accepted-and-ignored.

    A typo would otherwise produce a worker that accepts a lane nothing routes to while
    starving one that is routed — the starvation case, arrived at by a spelling mistake.
    """
    config.load({WORKER_ACCEPTED_LANES.name: raw})
    with pytest.raises(CategorizedError):
        config.require(WORKER_ACCEPTED_LANES)


def test_a_duplicated_lane_collapses_rather_than_inflating_the_loop_count() -> None:
    # The worker starts one claim loop per accepted lane and sizes its pool floor from the
    # count, so a repeated entry must not buy a second loop for the same lane.
    config.load({WORKER_ACCEPTED_LANES.name: "default,default"})
    assert config.require(WORKER_ACCEPTED_LANES) == (DEFAULT_JOB_DISPATCH_LANE,)
