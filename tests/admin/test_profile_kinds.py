"""Tests for the profile-kind residue sweep (issue #1907, ADR-0579).

Two halves, matching the module's own split: the pure surface (``_section_label`` and
``format_profile_kind_result``) needs no database, and :func:`scan_profile_kinds` is driven
against the migrated fixture database over an open connection. The pool-opening wrapper and the
CLI wiring are tested separately.

The database seeds go through ``RESOURCES.insert`` / ``ALLOCATIONS.insert`` / ``SYSTEMS.insert``
so the fixture rows are built by the code production uses and cannot drift from the schema. The
one carve-out is ``created_at``: it is server-generated, so the ordering fixture imposes it with
a direct ``UPDATE`` after inserting.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.admin.profile_kinds import (
    _RAISING_LANES,
    _SCAN_QUERY,
    ProfileKindMismatch,
    _section_label,
    format_profile_kind_result,
    scan_profile_kinds,
    verify_profile_kinds,
)
from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System

_REDACTED_URL = "postgresql://demo:***@db.example/kdive"

# 500 keys outside `_KNOWN_KINDS`, all mapping to `<unrecognized>` and deduplicating to one entry.
_FIVE_HUNDRED_UNKNOWN_KEYS = {f"key-{i}": {} for i in range(500)}


_DEFAULT_SYSTEM_ID = UUID("00000000-0000-0000-0000-000000000001")


def _make_mismatch(
    *,
    project: str = "demo",
    system_id: UUID = _DEFAULT_SYSTEM_ID,
    state: str = "ready",
    profile_section: str = "fault-inject",
    resource_kind: str = "fault-inject",
) -> ProfileKindMismatch:
    return ProfileKindMismatch(
        system_id=system_id,
        project=project,
        state=state,
        profile_section=profile_section,
        resource_kind=resource_kind,
    )


def _mismatch_line(report: str) -> str:
    """Return the sole `system=...` row from a formatted report."""
    lines = [line for line in report.splitlines() if line.startswith("system=")]
    assert len(lines) == 1
    return lines[0]


# --- _section_label ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        pytest.param({"local-libvirt": {"a": 1}}, "local-libvirt", id="one-key-object"),
        pytest.param(
            {"fault-inject": {}, "local-libvirt": {}},
            "fault-inject,local-libvirt",
            id="two-sections",
        ),
        pytest.param(_FIVE_HUNDRED_UNKNOWN_KEYS, "<unrecognized>", id="500-unknown-keys"),
        pytest.param(
            {"fault-inject": None}, "fault-inject=<not-an-object>", id="section-not-object-null"
        ),
        pytest.param(
            {"fault-inject": []}, "fault-inject=<not-an-object>", id="section-not-object-array"
        ),
        pytest.param(
            {"fault-inject": "x"}, "fault-inject=<not-an-object>", id="section-not-object-string"
        ),
        pytest.param({"\x1b[31mBOOM": {}}, "<unrecognized>", id="unrecognized-key"),
        pytest.param({}, "<none>", id="empty-object"),
        # One case, not two. JSON `null` and an absent `provider` key both arrive here as
        # `None`, so a second `None` row would exercise no path the first does not and could
        # only fail once the first already had. The distinction is real one layer down, at the
        # SQL boundary, and it is pinned there — see the `provider-json-null` scan case.
        pytest.param(None, "<none>", id="none-json-null-or-absent"),
        pytest.param("nope", "<not-an-object>", id="string-scalar"),
        pytest.param([1, 2], "<not-an-object>", id="array"),
        pytest.param(7, "<not-an-object>", id="number"),
        pytest.param(True, "<not-an-object>", id="bool"),
    ],
)
def test_section_label_measured_table(provider: object, expected: str) -> None:
    assert _section_label(provider) == expected


# --- format_profile_kind_result: charset + tokenization (spec Testing item 8) --------------


@pytest.mark.parametrize(
    "hostile",
    ["\x1b", "\x7f", "\x9b", "\xa0", "\u202e"],
    ids=["esc", "del", "csi", "nbsp", "rlo"],
)
def test_format_charset_whole_line_is_printable(hostile: str) -> None:
    mismatch = _make_mismatch(project=f"proj-{hostile}-name")
    report = format_profile_kind_result([mismatch], redacted_url=_REDACTED_URL)

    line = _mismatch_line(report)

    assert line.isprintable()


@pytest.mark.parametrize(
    "project",
    [
        "x state=ready profile_section=fault-inject",
        'x" state=ready profile_section=fault-inject resource_kind=fault-inject junk="y',
        "x' state=torn_down profile_section=fault-inject resource_kind=fault-inject junk='y",
        "x\tstate=torn_down",
    ],
    ids=["no-quote", "forged-double-quote", "forged-single-quote", "forged-tab"],
)
def test_format_tokenization_project_cannot_forge_a_field(project: str) -> None:
    """The report line is a whitespace-delimited `key=value` sequence, so the property that
    matters is that a hostile `project` contributes exactly one token — not that `repr` was
    called on it.

    Asserting the latter (anchoring on `f"project={project!r}"`) restates the implementation
    and cannot fail on forgery: `repr` selects its quote character rather than escaping, and
    leaves ASCII space intact because a space is printable. The `forged-single-quote` case
    below is the one that proves it — against a bare `!r` it renders `state=torn_down` as the
    *first* `state=` token on the row, so any grep- or awk-driven triage of a multi-row report
    reads the wrong state for that System.
    """
    mismatch = _make_mismatch(project=project)
    report = format_profile_kind_result([mismatch], redacted_url=_REDACTED_URL)
    line = _mismatch_line(report)

    # The bite: split the way an operator's grep/awk does and demand one token per key.
    keys = [token.split("=", 1)[0] for token in line.split() if "=" in token]
    assert keys.count("state") == 1
    assert keys.count("profile_section") == 1
    assert keys.count("resource_kind") == 1
    assert keys.count("project") == 1

    # The real state must be the only one present, whatever the project name claims.
    assert "state=ready" in line
    assert "state=torn_down" not in line


# --- format_profile_kind_result: empty / populated report shape ----------------------------


def test_format_empty_names_redacted_url() -> None:
    report = format_profile_kind_result([], redacted_url=_REDACTED_URL)

    assert report == (
        "verified every System bound to a Resource has a stored provisioning-profile "
        f"provider of exactly one section keyed by its Resource kind in {_REDACTED_URL}"
    )


def test_format_mismatches_header_body_and_closing_block() -> None:
    first = _make_mismatch(
        system_id=UUID("00000000-0000-0000-0000-000000000001"),
        project="proj-a",
        state="ready",
        profile_section="fault-inject",
        resource_kind="fault-inject",
    )
    second = _make_mismatch(
        system_id=UUID("00000000-0000-0000-0000-000000000002"),
        project="proj-b",
        state="provisioning",
        profile_section="<none>",
        resource_kind="local-libvirt",
    )

    report = format_profile_kind_result([first, second], redacted_url=_REDACTED_URL)
    lines = report.splitlines()

    assert lines[0] == (
        "found 2 System(s) whose stored provisioning-profile provider is not exactly one "
        f"section keyed by their bound Resource's kind in {_REDACTED_URL}"
    )
    assert (
        "system=00000000-0000-0000-0000-000000000001 project='proj-a' state=ready "
        "profile_section=fault-inject resource_kind=fault-inject" in report
    )
    assert (
        "system=00000000-0000-0000-0000-000000000002 project='proj-b' state=provisioning "
        "profile_section=<none> resource_kind=local-libvirt" in report
    )
    assert "ADR-0549" in report
    assert "remediation is not automated" in report.lower()
    for lane in _RAISING_LANES:
        assert lane in report


def test_raising_lanes_resolve_to_real_paths_and_symbols() -> None:
    """The lane list is operator-facing: it is printed at the foot of every non-empty report so
    a reader can go look at the code. Asserting the four literal strings appear in the report
    only pins `_RAISING_LANES` against itself — it restates the constant and stays green through
    any rename, move, or deletion of the four modules, which is the rot the list's own comment
    claims to have removed. Resolve each entry instead: the path must exist and the symbol must
    be defined in it. This reddens on exactly the drift the string assert cannot see.
    """
    repo_root = Path(__file__).resolve().parents[2]

    for lane in _RAISING_LANES:
        path_part, _, symbol_part = lane.partition(" (")
        symbol = symbol_part.rstrip(")")
        assert symbol, f"{lane!r} carries no (symbol) parenthetical"

        target = repo_root / path_part
        assert target.is_file(), f"{lane!r} names a path that does not exist"

        source = target.read_text(encoding="utf-8")
        assert re.search(rf"^\s*(async\s+)?def\s+{re.escape(symbol)}\b", source, re.MULTILINE), (
            f"{lane!r} names {symbol!r}, which is not defined in {path_part} — a policy method "
            "called there is not a symbol an operator can grep for in that file"
        )


# --- scan_profile_kinds: the database-backed sweep (spec Testing items 1-6) -----------------

# Supplied to every seeded record, and ignored for `created_at` / `updated_at`: both are in
# `_SERVER_GENERATED`, so the repositories omit them from their inserts and the columns fall to
# their schema defaults. The ordering fixture below imposes `created_at` out of band instead.
_DT = datetime(2026, 1, 1, tzinfo=UTC)

_LIBVIRT_SECTION: dict[str, Any] = {
    "domain_xml_params": {"machine": "q35"},
    "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/fedora-40.qcow2"},
    "crashkernel": "256M",
}
_FAULT_INJECT_SECTION: dict[str, Any] = {"destructive_ops": [], "capture_method": "console"}


def _base_profile() -> dict[str, Any]:
    """A stored profile document carrying no ``provider`` key."""
    return {
        "schema_version": 1,
        "arch": "x86_64",
        "vcpu": 4,
        "memory_mb": 4096,
        "disk_gb": 20,
        "boot_method": "direct-kernel",
        "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    }


def _profile(provider: object) -> dict[str, Any]:
    return {**_base_profile(), "provider": provider}


@asynccontextmanager
async def _conn(url: str) -> AsyncIterator[psycopg.AsyncConnection]:
    """An ``autocommit=True`` connection.

    The mode is a requirement, not a style choice: ``AsyncConnection.connect()`` defaults to
    ``autocommit=False``, and item 1's ``SET default_transaction_read_only = on`` is a silent
    no-op for the transaction already open in that mode — the guard would be dead and the test
    would pass either way.
    """
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


async def _seed_resource(conn: psycopg.AsyncConnection, kind: ResourceKind) -> UUID:
    resource = await RESOURCES.insert(
        conn,
        Resource(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            kind=kind,
            pool=kind.value,
            cost_class="local",
            status=ResourceStatus.AVAILABLE,
            host_uri="qemu:///system",
        ),
    )
    return resource.id


async def _seed_allocation(
    conn: psycopg.AsyncConnection, resource_id: UUID, *, project: str
) -> UUID:
    allocation = await ALLOCATIONS.insert(
        conn,
        Allocation(
            id=uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project=project,
            resource_id=resource_id,
            state=AllocationState.GRANTED,
        ),
    )
    return allocation.id


async def _seed_bound_system(
    conn: psycopg.AsyncConnection,
    *,
    kind: ResourceKind,
    profile: dict[str, Any],
    project: str = "proj",
    system_id: UUID | None = None,
) -> UUID:
    """Seed a Resource of ``kind``, a granted Allocation on it, and a ``ready`` System."""
    resource_id = await _seed_resource(conn, kind)
    allocation_id = await _seed_allocation(conn, resource_id, project=project)
    system = await SYSTEMS.insert(
        conn,
        System(
            id=system_id if system_id is not None else uuid4(),
            created_at=_DT,
            updated_at=_DT,
            principal="alice",
            project=project,
            allocation_id=allocation_id,
            state=SystemState.READY,
            provisioning_profile=profile,
        ),
    )
    return system.id


def test_scan_reports_the_mismatched_system_under_a_live_read_only_guard(
    migrated_url: str,
) -> None:
    """Spec item 1: #1907's own acceptance case, run read-only with the guard proven live."""

    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            mismatched = await _seed_bound_system(
                conn,
                kind=ResourceKind.FAULT_INJECT,
                profile=_profile({"local-libvirt": _LIBVIRT_SECTION}),
                project="residue",
            )
            await _seed_bound_system(
                conn,
                kind=ResourceKind.LOCAL_LIBVIRT,
                profile=_profile({"local-libvirt": _LIBVIRT_SECTION}),
                project="clean",
            )

            # Seed first, then arm the guard: the seeding writes share this connection.
            await conn.execute("SET default_transaction_read_only = on")

            assert await scan_profile_kinds(conn) == [
                ProfileKindMismatch(
                    system_id=mismatched,
                    project="residue",
                    state="ready",
                    profile_section="local-libvirt",
                    resource_kind="fault-inject",
                )
            ]

            # The bite behind criterion 3. `scan_profile_kinds` only SELECTs, so without this
            # the test passes whether the guard is live or dead.
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                await conn.execute(
                    "INSERT INTO budgets (project, limit_kcu, spent_kcu) VALUES ('probe', 1, 0)"
                )

    asyncio.run(_run())


def test_scan_of_a_clean_database_returns_no_mismatches(migrated_url: str) -> None:
    """Spec item 2: item 1's seed minus the mismatched System, so its assertion has a bite."""

    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            system_id = await _seed_bound_system(
                conn,
                kind=ResourceKind.LOCAL_LIBVIRT,
                profile=_profile({"local-libvirt": _LIBVIRT_SECTION}),
                project="clean",
            )

            # Positive control: without this, a seed that landed outside the scan's joins would
            # also pass the `== []` below, since emptiness proves nothing happened.
            async with conn.cursor() as cur:
                await cur.execute("SELECT id FROM systems WHERE id = %s", (system_id,))
                assert await cur.fetchone() is not None

            assert await scan_profile_kinds(conn) == []

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("profile", "expected_section"),
    [
        pytest.param(_base_profile(), "<none>", id="no-provider-key"),
        # Distinct from the row above at *this* layer: `{"provider": null}` and an absent
        # `provider` key are different stored rows, even though psycopg hands both to
        # `_section_label` as `None`. Pinned here, where the difference exists.
        pytest.param(_profile(None), "<none>", id="provider-json-null"),
        pytest.param(
            _profile({"fault-inject": _FAULT_INJECT_SECTION, "local-libvirt": _LIBVIRT_SECTION}),
            "fault-inject,local-libvirt",
            id="second-section-beside-the-matching-one",
        ),
        pytest.param(
            _profile({"fault-inject": None}),
            "fault-inject=<not-an-object>",
            id="section-under-the-bound-kind-is-not-an-object",
        ),
    ],
)
def test_scan_reports_degenerate_provider_shapes(
    migrated_url: str, profile: dict[str, Any], expected_section: str
) -> None:
    """Spec items 3-5: the predicate is total over the shapes a stored row can hold.

    ``second-section-beside-the-matching-one`` is the regression guard for the predicate's
    **second** disjunct; ``section-under-the-bound-kind-is-not-an-object`` is the only case
    here that reddens if the **first** is dropped, since every other case mismatches on the key.
    """

    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            system_id = await _seed_bound_system(
                conn, kind=ResourceKind.FAULT_INJECT, profile=profile, project="residue"
            )

            assert await scan_profile_kinds(conn) == [
                ProfileKindMismatch(
                    system_id=system_id,
                    project="residue",
                    state="ready",
                    profile_section=expected_section,
                    resource_kind="fault-inject",
                )
            ]

    asyncio.run(_run())


# --- scan_profile_kinds: deterministic order (spec Testing item 6) --------------------------

# Fixed, explicit UUIDs: every seed helper in this repo uses `uuid4()`, which would make the
# tiebreak assertion pass or fail at random.
_ID_EARLY = UUID("00000000-0000-0000-0000-0000000000aa")
_ID_TIE_LOW = UUID("00000000-0000-0000-0000-000000000001")
_ID_TIE_HIGH = UUID("00000000-0000-0000-0000-000000000002")

_ORDER_EARLY_AT = datetime(2026, 1, 1, tzinfo=UTC)
_ORDER_LATE_AT = datetime(2026, 6, 1, tzinfo=UTC)

_ASSERTED_ORDER = (_ID_EARLY, _ID_TIE_LOW, _ID_TIE_HIGH)

# `_SCAN_QUERY` with its trailing `ORDER BY` stripped: the exact shape `scan_profile_kinds` would
# issue under the "drop the whole ORDER BY" mutation. The join's chosen plan — not just the
# `systems` heap's own layout — decides the order an un-ordered read of these rows comes back in,
# so a hand-rolled query over `systems` alone measures a different, irrelevant scan shape.
# `rsplit` is correct by construction, not by luck: a top-level `ORDER BY` can only be followed
# by `LIMIT`/`OFFSET`/`FETCH`/`FOR UPDATE`, while a window's `OVER (ORDER BY ...)` or a subquery's
# own clause must appear earlier in the statement — so the LAST occurrence is the top-level one.
# The assertion below turns the two shapes that would break the derivation (the keyword gone, or
# a second occurrence) into a named failure at import instead of a cryptic `ProgrammingError` or
# a pinned-value mismatch.
assert _SCAN_QUERY.count("ORDER BY") == 1, "_UNORDERED_SCAN_QUERY derivation assumes exactly one"
_UNORDERED_SCAN_QUERY = _SCAN_QUERY.rsplit("ORDER BY", 1)[0]


async def _seed_ordering_fixture(conn: psycopg.AsyncConnection) -> None:
    """Seed three mismatched Systems whose stored order disagrees with the asserted order.

    The asserted order is :data:`_ASSERTED_ORDER`, and the fixture disagrees with it in **both**
    of its orderings — otherwise the scan returns the asserted order for free and both
    ``ORDER BY`` mutations report green against a query shipping no ``ORDER BY`` at all:

    - the tied pair is **inserted** larger-``id`` first, and
    - every row's ``created_at`` is **UPDATE**d in the reverse of the asserted order.

    Deliberately no claim here about *why* an un-ordered read emits rows in that order. For this
    three-table join it is planner-dependent, not simply ``systems``' own heap layout: a bare
    ``SELECT id FROM systems`` returns a different sequence and fails against the unmutated tree.
    That is exactly why the precondition below reads through :data:`_UNORDERED_SCAN_QUERY` rather
    than a single-table probe — it measures the shape the mutation would actually expose.

    ``created_at`` is server-generated (``_SERVER_GENERATED`` in ``db/repositories.py``), so
    ``SYSTEMS.insert`` cannot write it; the direct ``UPDATE`` is the one carve-out from the
    repositories in this fixture. The tie is imposed by **one identical literal timestamp** —
    two rows seeded together on an ``autocommit=True`` connection do not share ``now()``, and
    without a real tie ``ORDER BY s.created_at`` alone fully determines the pair's order.
    """
    profile = _profile({"local-libvirt": _LIBVIRT_SECTION})
    for system_id in (_ID_EARLY, _ID_TIE_HIGH, _ID_TIE_LOW):
        await _seed_bound_system(
            conn, kind=ResourceKind.FAULT_INJECT, profile=profile, system_id=system_id
        )
    for system_id, created_at in (
        (_ID_TIE_HIGH, _ORDER_LATE_AT),
        (_ID_TIE_LOW, _ORDER_LATE_AT),
        (_ID_EARLY, _ORDER_EARLY_AT),
    ):
        await conn.execute(
            "UPDATE systems SET created_at = %s WHERE id = %s", (created_at, system_id)
        )

    # Precondition guard for THIS FIXTURE, not for `scan_profile_kinds`: the disagreement the
    # two docstring bullets above describe rests on the planner's chosen join strategy, which a
    # version bump or a schema change adding a preferred index could silently stop holding. If
    # that happens, an un-ordered read of these rows returns the asserted order for free, and the
    # drop-whole-`ORDER BY` mutation passes against a query with no `ORDER BY` at all with
    # nothing here to say why. Issue `_UNORDERED_SCAN_QUERY` — the real scan minus its
    # `ORDER BY`, so the read is driven by the same planner decisions the mutation would be — and
    # pin the result to the reverse-of-asserted order this fixture is built to produce. If this
    # assertion fails, the fixture went stale — the shipped query has not been touched.
    #
    # What this does NOT measure: the `drop , s.id` mutation additionally needs the
    # `created_at`-only sort to leave the tied pair in scan order, and an UN-ordered read cannot
    # observe sort stability at all. If small-N stability stopped holding, the tiebreak test
    # would go green under that mutation while this assertion still passed. That assumption is
    # stated rather than guarded: pinning it would mean a second string-surgery derivation to
    # test a PostgreSQL implementation detail rather than this code.
    #
    # Both couplings — the join plan below and the sort stability above — are recorded in
    # issue #2079, which carries the triage path. If this assertion reddens, start there:
    # a new PostgreSQL plan is the likely cause, not a regression in `_SCAN_QUERY`.
    async with conn.cursor() as cur:
        await cur.execute(_UNORDERED_SCAN_QUERY)
        physical_order = [row[0] for row in await cur.fetchall()]
    assert physical_order == [_ID_TIE_HIGH, _ID_TIE_LOW, _ID_EARLY]


def test_scan_orders_by_created_at(migrated_url: str) -> None:
    """Spec item 6, first half: the earlier-stamped row comes back first."""

    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_ordering_fixture(conn)

            ids = [mismatch.system_id for mismatch in await scan_profile_kinds(conn)]

            assert len(ids) == len(_ASSERTED_ORDER)
            assert ids[0] == _ID_EARLY

    asyncio.run(_run())


def test_scan_breaks_created_at_ties_on_id(migrated_url: str) -> None:
    """Spec item 6, second half: rows sharing a timestamp come back in ``id`` order."""

    async def _run() -> None:
        async with _conn(migrated_url) as conn:
            await _seed_ordering_fixture(conn)

            ids = [mismatch.system_id for mismatch in await scan_profile_kinds(conn)]

            assert len(ids) == len(_ASSERTED_ORDER)
            assert ids[1:] == [_ID_TIE_LOW, _ID_TIE_HIGH]

    asyncio.run(_run())


# --- verify_profile_kinds: the pool-opening wrapper (spec Testing item 13) -------------------


def test_verify_profile_kinds_opens_its_own_pool_against_a_real_database(
    monkeypatch: pytest.MonkeyPatch, migrated_url: str
) -> None:
    """Spec item 13: drives `verify_profile_kinds` end to end — it opens its own pool from
    `KDIVE_DATABASE_URL` and returns the same rows `scan_profile_kinds(conn)` does. Same seed as
    item 1, but through the wrapper and the environment variable rather than an explicit
    connection.

    What this does **not** cover, stated so the gap is not read as coverage: nothing here
    asserts `finally: await pool.close()` ran. A leaked pool raises no error the suite escalates
    (`addopts` sets no `filterwarnings`), so deleting that `finally` would stay green. Pool
    closure rests on inspection. The unset-URL path is covered separately, below.
    """

    async def _seed() -> UUID:
        async with _conn(migrated_url) as conn:
            mismatched = await _seed_bound_system(
                conn,
                kind=ResourceKind.FAULT_INJECT,
                profile=_profile({"local-libvirt": _LIBVIRT_SECTION}),
                project="residue",
            )
            await _seed_bound_system(
                conn,
                kind=ResourceKind.LOCAL_LIBVIRT,
                profile=_profile({"local-libvirt": _LIBVIRT_SECTION}),
                project="clean",
            )
            return mismatched

    mismatched_id = asyncio.run(_seed())
    monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)

    result = asyncio.run(verify_profile_kinds())

    assert result == [
        ProfileKindMismatch(
            system_id=mismatched_id,
            project="residue",
            state="ready",
            profile_section="local-libvirt",
            resource_kind="fault-inject",
        )
    ]


def test_verify_profile_kinds_raises_configuration_error_without_a_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first row of the spec's failure-mode table, asserted rather than argued.

    `create_pool()` resolves `KDIVE_DATABASE_URL` *before* opening anything, so an unset
    variable must fail as a `CategorizedError(CONFIGURATION_ERROR)` — which `main()` renders and
    maps to ADR-0089's exit code — not as a connection error from a pool that was opened first.
    Needs no database: it must raise before any query.
    """
    monkeypatch.delenv("KDIVE_DATABASE_URL", raising=False)

    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(verify_profile_kinds())

    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
