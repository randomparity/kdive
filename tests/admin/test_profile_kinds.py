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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.admin.profile_kinds import (
    ProfileKindMismatch,
    _section_label,
    format_profile_kind_result,
    scan_profile_kinds,
)
from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
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
        pytest.param(None, "<none>", id="none-json-null"),
        pytest.param(None, "<none>", id="none-absent-provider"),
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
    ],
    ids=["no-quote", "forged-double-quote"],
)
def test_format_tokenization_project_cannot_forge_a_field(project: str) -> None:
    mismatch = _make_mismatch(project=project)
    report = format_profile_kind_result([mismatch], redacted_url=_REDACTED_URL)
    line = _mismatch_line(report)

    anchor = f"project={project!r}"
    assert anchor in line  # fail here, not on an IndexError below
    tail = line.split(anchor, 1)[1]

    # Documentation, not the bite: once the anchor matches, the remainder of the line is the
    # fixed suffix, so these counts are a constant for every input — keep them as an
    # executable description of the tail's shape, not as the property that catches forgery.
    assert tail.count("state=") == 1
    assert tail.count("profile_section=") == 1
    assert tail.count("resource_kind=") == 1


# --- format_profile_kind_result: empty / populated report shape ----------------------------


def test_format_empty_names_redacted_url() -> None:
    report = format_profile_kind_result([], redacted_url=_REDACTED_URL)

    assert report == (
        "verified no System's provisioning-profile provider section mismatches its "
        f"Resource kind in {_REDACTED_URL}"
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
        "found 2 System(s) whose provisioning-profile provider section does not match "
        f"their Resource kind in {_REDACTED_URL}"
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
    assert "src/kdive/mcp/tools/lifecycle/control/registrar.py:207" in report
    assert "src/kdive/services/runs/steps.py:445" in report
    assert "src/kdive/jobs/handlers/runs/boot_evidence.py:243" in report
    assert "src/kdive/mcp/tools/lifecycle/vmcore/handlers.py:181" in report


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
            await _seed_bound_system(
                conn,
                kind=ResourceKind.LOCAL_LIBVIRT,
                profile=_profile({"local-libvirt": _LIBVIRT_SECTION}),
                project="clean",
            )

            assert await scan_profile_kinds(conn) == []

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("profile", "expected_section"),
    [
        pytest.param(_base_profile(), "<none>", id="no-provider-key"),
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


async def _seed_ordering_fixture(conn: psycopg.AsyncConnection) -> None:
    """Seed three mismatched Systems whose stored order disagrees with the asserted order.

    The asserted order is :data:`_ASSERTED_ORDER`, and the fixture disagrees with it in **both**
    of its orderings — otherwise the scan returns the asserted order for free and both
    ``ORDER BY`` mutations report green against a query shipping no ``ORDER BY`` at all:

    - the tied pair is **inserted** larger-``id`` first, and
    - every row's ``created_at`` is **UPDATE**d in the reverse of the asserted order. PostgreSQL
      writes a new tuple version on ``UPDATE``, so a seq scan returns rows in last-write order.

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
