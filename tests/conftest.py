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
The ``staged_cleanup`` fixture is the matching explicit owner for ``render_argv``'s staged
host tempfiles.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

import kdive.config as config
import kdive.jobs.assembly as job_assembly_module
import kdive.mcp.assembly.app as mcp_app_module
from kdive.processes.assembly import ProcessAssembly
from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly, ObjectStoreFactory
from kdive.store.objectstore import ObjectStore
from tests._addopts_scrub import pytest_collection  # noqa: F401  registered as a conftest hook

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
    a licence — a helper that creates temp state should still own its lifecycle (see
    ``staged_cleanup``), and ``tests/guards/test_temp_file_hygiene.py`` pins the backstop itself.
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


@pytest.fixture
def staged_cleanup() -> Iterator[list[Path]]:
    """The ``cleanup`` list :func:`kdive.images.families.renderers.render_argv` stages files into.

    ``render_argv`` writes each ``StageFile`` step's content to a host tempfile and appends the
    path to this list, leaving the *caller* to unlink them once ``virt-customize`` has consumed
    them. Production passes a list it drains; a test that passes a throwaway ``[]`` discards the
    only handle to the staged files and leaks them. This fixture is the test-side owner: the test
    body can still read what was staged, and teardown unlinks it.
    """
    staged: list[Path] = []
    yield staged
    for path in staged:
        path.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def reset_config() -> Iterator[None]:
    config.reset()
    yield
    config.reset()


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
