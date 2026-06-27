"""Project onboarding and verification command helpers."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg_pool import AsyncConnectionPool


def seed_project_statements(
    *,
    project: str,
    limit_kcu: Decimal,
    max_concurrent_allocations: int,
    max_concurrent_systems: int,
) -> list[tuple[str, Sequence[Any]]]:
    """Build the idempotent budget/quota upserts for a project (see :func:`seed_project`).

    These raw ``INSERT``s reproduce the row content the audited ``accounting.set_budget`` /
    ``accounting.set_quota`` tools write, but deliberately bypass them: the seeder runs at
    deploy time with no OIDC token or request context, so it cannot satisfy their
    ``require_role(..., admin)`` gate. The production project-onboarding path is the admin
    tools, not this seeder — see ``docs/operating/project-onboarding.md``.
    """
    return [
        (
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) "
            "VALUES (%s, %s, 0) "
            "ON CONFLICT (project) DO UPDATE SET limit_kcu = EXCLUDED.limit_kcu",
            (project, limit_kcu),
        ),
        (
            "INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (project) DO UPDATE SET "
            "max_concurrent_allocations = EXCLUDED.max_concurrent_allocations, "
            "max_concurrent_systems = EXCLUDED.max_concurrent_systems",
            (project, max_concurrent_allocations, max_concurrent_systems),
        ),
    ]


async def seed_project(
    *,
    project: str,
    limit_kcu: Decimal,
    max_concurrent_allocations: int,
    max_concurrent_systems: int,
) -> None:
    """Seed budget/quota rows and register discoverable resources for enabled providers.

    The token-less onboarding path for local/host deployments: the writes bypass the audited
    admin tools (see :func:`seed_project_statements`), so it runs at deploy time with no OIDC
    token or request context.
    """
    from kdive.db.pool import create_pool

    pool = create_pool()
    await pool.open()
    try:
        async with pool.connection() as conn, conn.transaction():
            for statement, params in seed_project_statements(
                project=project,
                limit_kcu=limit_kcu,
                max_concurrent_allocations=max_concurrent_allocations,
                max_concurrent_systems=max_concurrent_systems,
            ):
                await conn.execute(statement.encode(), params)
        await register_discovered_resources(pool)
    finally:
        await pool.close()


async def register_discovered_resources(pool: AsyncConnectionPool) -> None:
    from kdive.providers.assembly.composition import ProviderComposition

    await ProviderComposition().build_provider_resolver().register_all_discovery(pool)


@dataclass(frozen=True, slots=True)
class ProjectFundingStatus:
    """The funding rows a project must have for ``allocations.request`` to pass (ADR-0256).

    ``budget_present`` / ``quota_present`` reflect whether the ``budgets`` / ``quotas`` rows
    exist; the figures are the values admission control reads. ``max_concurrent_systems`` is
    deliberately absent — that is the ``systems.create`` gate, not what ``allocations.request``
    checks, and reading it would mean a bespoke query rather than reusing the admission reads.
    """

    budget_present: bool
    quota_present: bool
    limit_kcu: Decimal | None
    spent_kcu: Decimal | None
    max_concurrent_allocations: int | None
    occupancy: int

    @property
    def funded(self) -> bool:
        """True iff both funding rows exist (the admission funding reads will return rows)."""
        return self.budget_present and self.quota_present


async def verify_project(*, project: str) -> ProjectFundingStatus:
    """Read back ``project``'s funding rows to confirm the seed persisted (ADR-0256).

    Reuses admission control's own funding reads — :func:`budget_snapshot` and
    :func:`quota_status` — so "funded" means "the reads admission performs will return rows,"
    with no second copy of the lookup to drift. These are used here as **advisory point-reads
    outside the PROJECT lock** (``quota_status`` is documented "read under the held PROJECT
    lock"); that is acceptable because verify reports state, it makes no admission decision.

    Connects with :func:`create_pool` (raises ``CONFIGURATION_ERROR`` if ``KDIVE_DATABASE_URL``
    is unset), so verify never returns for an unset URL. The targeted DB is whatever
    ``KDIVE_DATABASE_URL`` resolves to — the same value the seed used in the same run.

    Args:
        project: The project whose ``budgets`` / ``quotas`` rows to read back.

    Returns:
        A :class:`ProjectFundingStatus` describing presence and the read figures.
    """
    from kdive.db.pool import create_pool
    from kdive.services.allocation.admission.core import quota_status
    from kdive.services.allocation.idempotency import budget_snapshot

    pool = create_pool()
    await pool.open()
    try:
        async with pool.connection() as conn:
            budget = await budget_snapshot(conn, project)
            max_concurrent_allocations, occupancy = await quota_status(conn, project)
    finally:
        await pool.close()
    limit_kcu, spent_kcu = budget if budget is not None else (None, None)
    return ProjectFundingStatus(
        budget_present=budget is not None,
        quota_present=max_concurrent_allocations is not None,
        limit_kcu=limit_kcu,
        spent_kcu=spent_kcu,
        max_concurrent_allocations=max_concurrent_allocations,
        occupancy=occupancy,
    )


def redact_database_url(url: str) -> str:
    """Mask the password in a Postgres conninfo so it is safe to print (ADR-0256).

    Handles the ``postgresql://`` URL form carrying a userinfo password (password → ``***``,
    host/port/db intact). Returns the input unchanged when it carries no password — the
    diagnostic value is the host/db, not the secret. Any *other* string that mentions
    ``password`` (a libpq keyword/value conninfo, where the value may be quoted or spaced and a
    token regex would only partially mask it) is replaced wholesale with ``<redacted>``: a
    partial mask could leak the tail of a real secret, so it is never attempted.

    Args:
        url: A psycopg URL or keyword/value conninfo string.

    Returns:
        A display-safe rendering with any password component masked.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme and parsed.password is not None:
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        netloc = f"{parsed.username or ''}:***@{host}"
        return urllib.parse.urlunsplit(
            (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
        )
    if re.search(r"password", url, re.IGNORECASE):
        return "<redacted: conninfo with password>"
    return url


def format_verify_result(
    status: ProjectFundingStatus, *, project: str, redacted_url: str
) -> tuple[str, int]:
    """Render the ``verify-project`` message + exit code (ADR-0256).

    Args:
        status: The read-back funding status.
        project: The project that was verified.
        redacted_url: The credential-redacted target DB (see :func:`redact_database_url`).

    Returns:
        ``(message, exit_code)`` — code ``0`` when funded, ``1`` when either row is absent.
    """
    if status.funded:
        message = (
            f"verified project {project!r} is funded in {redacted_url}: "
            f"budget limit_kcu={status.limit_kcu} spent_kcu={status.spent_kcu}, "
            f"quota max_concurrent_allocations={status.max_concurrent_allocations} "
            f"(occupancy={status.occupancy})"
        )
        return message, 0
    missing = [
        name
        for name, present in (("budget", status.budget_present), ("quota", status.quota_present))
        if not present
    ]
    message = (
        f"project {project!r} is NOT funded in {redacted_url}: missing "
        f"{' and '.join(missing)} row(s). Seed it with `just onboard` "
        f"(or `kdive seed-project --project {project}`) against this database."
    )
    return message, 1
