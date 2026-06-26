"""Installed-package admin helpers: migrate, install-fixtures, seed-project.

The app-process bring-up (the `stack` supervisor and the `install-compose`/
`print-local-env` dev crutches) was retired in ADR-0088 decision 9: the published
image — or the compose app tier — is the bring-up path. Only the real operations the
image still invokes remain here.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.admin.default_fixtures import LOCAL_LIBVIRT_FIXTURES
from kdive.config.core_settings import DATABASE_URL
from kdive.db.migrate import apply_migrations


def default_fixture_files() -> Mapping[str, str]:
    return LOCAL_LIBVIRT_FIXTURES


def _refuse_existing(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")


def install_fixtures(dest: Path, *, force: bool = False) -> None:
    _refuse_existing(dest, force=force)
    for relative, content in LOCAL_LIBVIRT_FIXTURES.items():
        path = dest / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def migrate(database_url: str | None = None) -> int:
    """Apply database migrations only (ADR-0121).

    Inventory reconcile is the reconciler loop's job (ADR-0112) and the build-config seed is the
    ``seed-build-configs`` command (ADR-0096) — both are deliberately *not* run here, so a failed
    "migrate" Job always means a SQL migration failed, never a config/bucket fault.

    Args:
        database_url: A psycopg connection string, or ``None`` to read ``KDIVE_DATABASE_URL``.

    Returns:
        The number of migrations applied.
    """
    url = database_url or config.require(DATABASE_URL)
    conn = psycopg.connect(url, autocommit=True)
    try:
        applied = apply_migrations(conn)
    finally:
        conn.close()
    print(f"applied {len(applied)} migration(s)")
    return len(applied)


def seed_build_configs_step(database_url: str | None = None) -> int:
    """Publish the packaged build-config fragments (the deploy ``seed-build-configs`` step).

    Re-homed out of ``migrate()`` (ADR-0121). S3-gated + idempotent: a wholly-unconfigured object
    store is a clean skip (returns 0); a configured-but-broken store (missing bucket, bad
    credentials) raises — a real object-store fault must surface, not be swallowed.

    Args:
        database_url: A psycopg connection string, or ``None`` to read ``KDIVE_DATABASE_URL``.

    Returns:
        The number of build-config fragments published (0 if already current or skipped).
    """
    url = database_url or config.require(DATABASE_URL)
    seeded = _seed_build_configs_step(url)
    print(f"seeded {seeded} build-config fragment(s)")
    return seeded


def _run_async_db_step(
    database_url: str, step: Callable[[psycopg.AsyncConnection], Awaitable[int]]
) -> int:
    import asyncio

    async def _run() -> int:
        async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
            return await step(conn)

    return asyncio.run(_run())


def _seed_build_configs_step(database_url: str) -> int:
    """Publish the packaged build-config fragments after migrating (ADR-0096).

    Runs in the deploy ``migrate -> seed`` step. Idempotent (sha256-gated). The fragments
    live in the object store, so the seed is skipped when ``KDIVE_S3_*`` is unconfigured —
    a no-S3 migrate (e.g. a schema-only test or a partial bring-up) degrades cleanly and the
    fragment is seeded on a later migrate once the object store is available. Mirrors the
    optional object-store policy in :mod:`kdive.store.assembly`.

    Args:
        database_url: A psycopg-compatible connection string for the application database.

    Returns:
        The number of build-config fragments published (0 if already current or skipped).
    """
    from kdive.build_configs.seed import seed_build_configs
    from kdive.domain.errors import CategorizedError, ErrorCategory
    from kdive.store.objectstore import object_store_from_env

    try:
        store = object_store_from_env()
    except CategorizedError as exc:
        if exc.category is not ErrorCategory.CONFIGURATION_ERROR:
            raise
        print("skipped build-config seed: object store not configured")
        return 0

    async def _seed(conn: psycopg.AsyncConnection) -> int:
        return await seed_build_configs(conn, store)

    return _run_async_db_step(database_url, _seed)


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
