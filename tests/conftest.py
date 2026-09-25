"""Shared test fixtures.

The autouse ``reset_config`` fixture clears the config snapshot around every test so a
per-case ``monkeypatch.setenv`` is honored rather than frozen behind a stale snapshot
(ADR-0087's scoped-not-permanent resolution).

The autouse ``sandbox_systems_toml`` fixture isolates the inventory-path default
(``KDIVE_SYSTEMS_TOML`` → ``$XDG_CONFIG_HOME/kdive/systems.toml``, ADR-0112): it points
``XDG_CONFIG_HOME`` at an empty per-test temp dir and unsets ``KDIVE_SYSTEMS_TOML``, so a
test that loads inventory without setting either exercises the production XDG branch but
lands on an absent file (a quiet no-op) instead of reading the developer's real
``~/.config/kdive/systems.toml``. A test that needs a concrete file still overrides via
``monkeypatch.setenv`` + ``config.load()``.

The one exception is a test carrying a live-tier marker (``live_stack`` / ``live_vm``). Those
are operator-run against a real fleet that is only reachable through the inventory file, so an
explicitly-exported ``KDIVE_SYSTEMS_TOML`` survives for them. Unsetting it unconditionally is
what made the remote live_stack spine skip with "no [[remote_libvirt]] instance declared" on
every host regardless of configuration — a gated tier that could never run (#1610).

The autouse ``s3_backend_env`` fixture re-pins the ``KDIVE_S3_*`` configuration around every
test, so a case that mutates it cannot leak into the next. S3 is a required backend
(ADR-0337), so the default test environment supplies dummy configuration for direct boundary
tests. MCP and worker registry tests inject inert store wiring because they inspect registration
without invoking object I/O. The *resolved* configuration still yields to real ``KDIVE_S3_*``
values exported by live_vm jobs. A test that exercises S3 absence builds its own ``Registry`` or
``delenv``s the vars explicitly.

The session-scoped autouse ``session_owned_tempdir`` fixture repoints the default temp root at
pytest's base temp directory, so an unnamed ``tempfile`` destination is bounded by pytest's
rotation instead of accumulating in ``/tmp`` until the filesystem runs out of inodes (#1613).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier, Literal
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
import kdive.jobs.assembly as job_assembly_module
import kdive.mcp.assembly.app as mcp_app_module
from kdive.assembly import ProcessAssembly
from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.runs import complete_build
from kdive.store.assembly import ObjectStoreAssembly, ObjectStoreFactory
from kdive.store.objectstore import ObjectStore
from tests._addopts_scrub import pytest_collection  # noqa: F401  registered as a conftest hook
from tests.db.conftest import _cluster_global_role_lock, _MigratedWorkerDb
from tests.integration.live_stack import conftest as stack_conftest
from tests.integration.live_stack.skew import SkewPolicy, probe_stack_skew, skew_policy

# Direct object-store boundary tests still need a complete configuration at collection time.
# ``setdefault`` yields to a real ``KDIVE_S3_*`` in the developer's shell.
_DUMMY_S3_ENDPOINT_URL = "http://minio.test:9000"
_DUMMY_S3_BUCKET = "kdive-test"
os.environ.setdefault("KDIVE_S3_ENDPOINT_URL", _DUMMY_S3_ENDPOINT_URL)
os.environ.setdefault("KDIVE_S3_BUCKET", _DUMMY_S3_BUCKET)

# The RESOLVED configuration: a real ``KDIVE_S3_*`` from the environment, else the dummies above.
# ``s3_backend_env`` re-pins these, never the dummy constants — the live_vm jobs export the running
# stack's real endpoint (scripts/live-stack/env.sh), and overwriting it with the unresolvable
# ``minio.test`` placeholder makes every live object-store call fail name resolution.
_S3_ENDPOINT_URL = os.environ["KDIVE_S3_ENDPOINT_URL"]
_S3_BUCKET = os.environ["KDIVE_S3_BUCKET"]
_LOGIN_PASSWORD = "external-boot-authority-test"  # pragma: allowlist secret


def pytest_sessionstart(session: pytest.Session) -> None:
    """Capture live proof context even when pytest suppresses its header with ``-q``."""
    base_url = os.environ.get("KDIVE_STACK_BASE_URL")
    if base_url and skew_policy() is not SkewPolicy.OFF:
        stack_conftest._HEADER_PROBES[base_url] = probe_stack_skew(base_url)
    if session.config.option.verbose < 0 and (base_url or os.environ.get("KDIVE_KERNEL_SRC")):
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            for line in pytest_report_header():
                reporter.write_line(line)


def pytest_report_header() -> list[str]:
    """Name the resolved kernel tree and probed app revisions in live proof output."""
    lines = []
    if kernel_src := os.environ.get("KDIVE_KERNEL_SRC"):
        lines.append(f"live kernel tree: {Path(kernel_src).resolve()}")
    base_url = os.environ.get("KDIVE_STACK_BASE_URL")
    if not base_url or skew_policy() is SkewPolicy.OFF:
        return lines
    probe = stack_conftest._HEADER_PROBES.get(base_url) or probe_stack_skew(base_url)
    stack_conftest._HEADER_PROBES[base_url] = probe
    revisions = [
        f"{result.process}="
        + (
            "not deployed"
            if not result.applicable
            else probe.revisions.get(result.process) or "unknown"
        )
        for result in probe.results
    ]
    lines.append("live-stack probed revisions: " + ", ".join(revisions))
    return lines


@dataclass(frozen=True, slots=True)
class _RoleDsns:
    parameters: dict[str, str]
    logins: dict[str, str]

    def __call__(self, role: str) -> str:
        return make_conninfo(
            **{
                **self.parameters,
                "user": self.logins[role],
                "password": _LOGIN_PASSWORD,
            }
        )


def _offline_object_store_assembly(
    store_factory: ObjectStoreFactory | None = None,
) -> ObjectStoreAssembly:
    """Return inert store wiring for app-schema tests that never invoke object I/O."""
    if store_factory is not None:
        return ObjectStoreAssembly(store=store_factory())
    return ObjectStoreAssembly(store=cast(ObjectStore, object()))


def _offline_process_assembly(secret_registry: SecretRegistry) -> ProcessAssembly:
    stores = _offline_object_store_assembly()
    return ProcessAssembly(
        object_stores=stores,
        providers=ProviderComposition(secret_registry=secret_registry, object_store=stores.store),
    )


# Several MCP contract modules build the app during collection, before fixtures can patch its
# infrastructure boundary. Keep those schema-only builds offline; tests of production store
# assembly call ``build_object_store_assembly`` directly or replace this seam explicitly.
mcp_app_module.build_process_assembly = _offline_process_assembly  # ty: ignore[invalid-assignment]
job_assembly_module.build_process_assembly = _offline_process_assembly  # ty: ignore[invalid-assignment]


# The operator's real inventory path, captured before any fixture runs (same reasoning as
# ``_S3_ENDPOINT_URL`` above). The gated live tiers are operator-run against a real fleet and
# reach it through ``KDIVE_SYSTEMS_TOML``; blanket-unsetting it made the remote live_stack
# preflight skip with "no [[remote_libvirt]] instance declared" on EVERY host, however well
# configured, so that tier could never execute (#1610).
_OPERATOR_SYSTEMS_TOML = os.environ.get("KDIVE_SYSTEMS_TOML")
_LIVE_TIER_MARKERS = ("live_stack", "live_vm")


@pytest.fixture(autouse=True)
def sandbox_systems_toml(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sandbox the inventory path, except for an operator-run live tier.

    Unit/service tests always land on an absent file, so they exercise the production XDG
    branch without reading the developer's real ``~/.config/kdive/systems.toml``. A test
    carrying a live-tier marker is operator-run by definition: it keeps an explicitly-exported
    ``KDIVE_SYSTEMS_TOML``, since the fleet it drives is only reachable through that file.
    ``XDG_CONFIG_HOME`` is sandboxed either way — the implicit default path is never honored.
    """
    live_tier = any(request.node.get_closest_marker(m) for m in _LIVE_TIER_MARKERS)
    if live_tier and _OPERATOR_SYSTEMS_TOML:
        monkeypatch.setenv("KDIVE_SYSTEMS_TOML", _OPERATOR_SYSTEMS_TOML)
    else:
        monkeypatch.delenv("KDIVE_SYSTEMS_TOML", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture(autouse=True)
def s3_backend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", _S3_ENDPOINT_URL)
    monkeypatch.setenv("KDIVE_S3_BUCKET", _S3_BUCKET)


@pytest.fixture(scope="session")
def authority_role_logins(
    _migrated_db: _MigratedWorkerDb, postgres_url: str
) -> Iterator[dict[str, str]]:
    """Create one locked LOGIN set per xdist worker after its role schema exists."""
    suffix = uuid4().hex[:16]
    logins = {
        role: f"kdive_eba_{role.removeprefix('kdive_')}_{suffix}"
        for role in (
            "kdive_server",
            "kdive_worker",
            "kdive_reconciler",
            "kdive_provider_authority",
        )
    }
    with (
        _cluster_global_role_lock(postgres_url),
        psycopg.connect(_migrated_db.url, autocommit=True) as conn,
    ):
        for role, login in logins.items():
            conn.execute(
                SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                    Identifier(login), Literal(_LOGIN_PASSWORD), Identifier(role)
                )
            )
    try:
        yield logins
    finally:
        with (
            _cluster_global_role_lock(postgres_url),
            psycopg.connect(_migrated_db.url, autocommit=True) as conn,
        ):
            for login in logins.values():
                conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))


@pytest.fixture
def authority_role_dsns(migrated_url: str, authority_role_logins: dict[str, str]) -> _RoleDsns:
    """Bind the xdist worker's shared LOGINs to this test's migrated database."""
    with psycopg.connect(migrated_url) as conn:
        return _RoleDsns(dict(conn.info.get_parameters()), authority_role_logins)


@pytest.fixture
async def kdive_worker_pool(authority_role_dsns: _RoleDsns) -> AsyncIterator[AsyncConnectionPool]:
    """Yield an open pool connected as the kdive_worker LOGIN principal."""
    pool = AsyncConnectionPool(
        authority_role_dsns("kdive_worker"), min_size=1, max_size=2, open=False
    )
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture(scope="session", autouse=True)
def session_owned_tempdir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point the session's default temp root at a directory pytest owns and rotates (#1613).

    Anything the suite creates without naming a directory — ``tempfile.mkdtemp()``,
    ``NamedTemporaryFile``, a subprocess honoring ``TMPDIR`` — lands in the system ``/tmp`` and
    survives the process unless some caller unlinks it. A caller that forgets leaks one entry per
    call forever. That exhausts *inodes* rather than disk, so it is invisible to ``df -h`` and
    surfaces as thousands of unrelated collection errors once the filesystem can no longer create
    tmpdirs.

    Rebinding the default root to pytest's base temp directory makes the leak bounded instead of
    unbounded: pytest already garbage-collects all but the three most recent base temp roots, so a
    forgotten unlink costs a directory for a few runs rather than forever. This is a backstop, not
    a licence — a helper that creates temp state should still own its lifecycle, and
    ``tests/guards/test_temp_file_hygiene.py`` pins the backstop itself.
    """
    previous_tempdir = tempfile.tempdir
    previous_env = os.environ.get("TMPDIR")
    root = tmp_path_factory.mktemp("systemp")
    tempfile.tempdir = str(root)
    os.environ["TMPDIR"] = str(root)
    yield
    tempfile.tempdir = previous_tempdir
    if previous_env is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = previous_env


@pytest.fixture(scope="session", autouse=True)
def standard_test_umask() -> Iterator[None]:
    """Ensure tests run with standard restrictive umask (0022) across environments."""
    previous_umask = os.umask(0o022)
    yield
    os.umask(previous_umask)


@pytest.fixture(autouse=True)
def reset_config() -> Iterator[None]:
    config.reset()
    yield
    config.reset()


@pytest.fixture(autouse=True)
def reset_external_build_validation_slots() -> Iterator[None]:
    """Give every test a fresh external-build validation semaphore.

    ``complete_build._EXTERNAL_BUILD_VALIDATION_SLOTS`` is a module-level
    ``asyncio.Semaphore`` built at import time. ``Semaphore.acquire`` reaches ``_get_loop()``
    only on the *contended* path — an uncontended acquire just decrements the counter — so the
    object stays unbound until some test actually waits on it, and then binds to that test's
    loop. Every test here drives the service through its own ``asyncio.run``, i.e. a new loop,
    so the next test that contends it dies with ``RuntimeError: ... is bound to a different
    event loop``.

    That makes the failure order-dependent, which under ``xdist --dist worksteal`` means it
    depends on how tests happen to be distributed: the same suite passes or fails by luck of
    scheduling, and adding an unrelated test to ``tests/services/runs`` is enough to flip it.

    Rebinding a fresh semaphore per test contains the leak at its source. This is test
    isolation only: production builds one loop per process and holds the semaphore for its
    lifetime, which is correct, and the counter is always released by ``_validate_uploads``'s
    ``finally`` before a test ends.
    """
    original = complete_build._EXTERNAL_BUILD_VALIDATION_SLOTS
    complete_build._EXTERNAL_BUILD_VALIDATION_SLOTS = asyncio.Semaphore(1)
    yield
    complete_build._EXTERNAL_BUILD_VALIDATION_SLOTS = original


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """Snapshot and restore mutable global root-logger state around every test.

    ``main()`` (and any real entrypoint) calls ``bootstrap_stdout_floor``, which attaches a
    stream handler to the *root* logger bound to the live ``sys.stderr``. Under pytest that
    stream is the per-test capture buffer, which pytest closes when the test ends. A test that
    exercises an entrypoint without stubbing the bootstrap therefore leaves that handler on the
    process-wide root logger pointed at a now-closed stream. On the next test that shares the
    worker (order-dependent under ``xdist --dist worksteal``), any propagating record makes the
    stale handler raise ``ValueError: I/O operation on closed file`` mid-``callHandlers``, which
    silently drops the record from pytest's ``caplog`` capture and breaks otherwise-unrelated
    log-assertion tests.

    Restoring the root handler list, level, and the global ``logging.disable`` floor after each
    test contains that leak at the source of the isolation failure, so no ordering can carry one
    test's logging mutation into another. This is test isolation only: production runs the
    bootstrap once and keeps the handler for the process lifetime, which is correct.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    disable = logging.root.manager.disable
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.disable(disable)
