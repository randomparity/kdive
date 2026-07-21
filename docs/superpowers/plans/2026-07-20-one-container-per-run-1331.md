# One backend container per test run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the per-xdist-worker Postgres/MinIO containers to one shared container per run, isolating workers by a per-worker uuid-scoped database/bucket, so `just test -n auto` starts one of each instead of ~18.

**Architecture:** A resource-agnostic coordination helper (`tests/support/xdist_backend.py`) owns the cross-process single-container lifecycle (per-run temp-root `fcntl.flock` + refcounted JSON state file, stop-by-id, Ryuk disabled). The two session fixtures `postgres_url` (`tests/db/conftest.py`) and `minio_store` (`tests/store/conftest.py`) call it: each first honors an env override (`KDIVE_TEST_PG_URL` / `KDIVE_TEST_S3_URL`), else acquires the shared container, then provisions its own `kdive_test_<worker>_<token>` database / `kdive-test-<worker>-<token>` bucket. Governed by ADR-0400.

**Tech Stack:** Python 3.14, `uv`, pytest + pytest-xdist, testcontainers, psycopg, boto3, stdlib `fcntl`/`uuid`/`json`.

## Global Constraints

- Python 3.14; run everything via `uv run` / `just` recipes (the justfile is the source of truth).
- No new runtime or dev dependency — coordination uses stdlib `fcntl.flock` (ADR-0400 rejects `filelock`).
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` runs whole-tree (src + tests). Absolute imports only.
- `KDIVE_REQUIRE_DOCKER=1` must still turn a no-Docker skip into a hard failure (ADR-0015/0017); `KDIVE_REQUIRE_DOCKER` unset must still skip cleanly.
- Guardrail suite: `just lint`, `just type`, `just test`. Full gate: `just ci`. Single test: `uv run python -m pytest <path>::<name> -q`.
- Per-worker names: database `kdive_test_<worker>_<token>`, bucket `kdive-test-<worker>-<token>`, where `<worker>` = `PYTEST_XDIST_WORKER` (or `master`) and `<token>` = a fresh `uuid4().hex[:12]` minted per worker at fixture setup.
- Shared Postgres `max_connections = max(500, PYTEST_XDIST_WORKER_COUNT × 20)`.
- Conventional-commit messages, imperative ≤72-char subject, ending with the repo's `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer. Stage explicit paths only.

## File Structure

- **Create** `tests/support/__init__.py` — package marker (empty).
- **Create** `tests/support/xdist_backend.py` — coordination helper: `xdist_worker_id`, `xdist_worker_count`, `per_run_root`, `worker_namespace_token`, `postgres_max_connections`, `shared_container` context manager, `with_database_name`.
- **Create** `tests/support/test_xdist_backend.py` — unit tests for the helper against a fake container (no Docker).
- **Modify** `tests/db/conftest.py` — rewrite `postgres_url`; keep `pg_conn`, `migrated_url`.
- **Modify** `tests/store/conftest.py` — rewrite `minio_store`; keep `key_ns`.
- **Create** `tests/db/test_postgres_url_fixture.py` — override + require-docker + per-worker-db tests.
- **Create** `tests/store/test_minio_store_fixture.py` — override + require-docker + per-worker-bucket tests.
- **Create** `tests/integration/test_shared_backend_real.py` — Docker-gated real-container coordination test (AC1 real, AC6b real).
- **Modify** `docker-compose.yml` — postgres `command:` sets `max_connections=500`.
- **Modify** `docs/operating/docker-compose.md` — required-cleanup note for a persistent override backend.

---

### Task 1: Coordination helper

**Files:**
- Create: `tests/support/__init__.py`
- Create: `tests/support/xdist_backend.py`
- Test: `tests/support/test_xdist_backend.py`

**Interfaces:**
- Produces:
  - `xdist_worker_id() -> str` — `os.environ.get("PYTEST_XDIST_WORKER", "master")`.
  - `xdist_worker_count() -> int` — `PYTEST_XDIST_WORKER_COUNT` or 1.
  - `per_run_root(tmp_path_factory) -> pathlib.Path` — the per-run temp root (`.parent` under xdist, else `getbasetemp()`).
  - `worker_namespace_token() -> str` — `uuid.uuid4().hex[:12]`.
  - `postgres_max_connections() -> int` — `max(500, xdist_worker_count() * 20)`.
  - `with_database_name(url: str, dbname: str) -> str` — return `url` with its path replaced by `/dbname`.
  - `shared_container(root, name, *, start, stop) -> Iterator[str]` — context manager yielding the server URL. `start()` returns `(url, container_id)`; `stop(container_id)` stops it. Coordinates via `root/f"kdive-{name}.lock"` (`fcntl.flock`) and `root/f"kdive-{name}.json"` (`{"url","container_id","refcount"}`): first caller calls `start` and writes `refcount=1`; others increment and reuse the URL; the caller that decrements to 0 calls `stop` and unlinks the state file.

- [ ] **Step 1: Write the failing tests**

```python
# tests/support/test_xdist_backend.py
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.support import xdist_backend


def test_worker_id_defaults_to_master(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert xdist_backend.xdist_worker_id() == "master"
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw7")
    assert xdist_backend.xdist_worker_id() == "gw7"


def test_worker_count_defaults_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER_COUNT", raising=False)
    assert xdist_backend.xdist_worker_count() == 1
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "18")
    assert xdist_backend.xdist_worker_count() == 18


def test_max_connections_floor_and_scaling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "4")
    assert xdist_backend.postgres_max_connections() == 500  # floor
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "64")
    assert xdist_backend.postgres_max_connections() == 1280  # 64 * 20


def test_with_database_name_replaces_path() -> None:
    url = "postgresql://u:p@host:5432/test"
    assert xdist_backend.with_database_name(url, "kdive_test_gw0_abc") == (
        "postgresql://u:p@host:5432/kdive_test_gw0_abc"
    )


def test_namespace_token_is_unique_and_short() -> None:
    a, b = xdist_backend.worker_namespace_token(), xdist_backend.worker_namespace_token()
    assert a != b and len(a) == 12 and a.isalnum()


class _FakeContainer:
    starts = 0
    stops: list[str] = []

    @classmethod
    def start(cls) -> tuple[str, str]:
        cls.starts += 1
        return "postgresql://u:p@host:5432/test", f"cid-{cls.starts}"

    @classmethod
    def stop(cls, cid: str) -> None:
        cls.stops.append(cid)


def _acquire(root: Path):
    return xdist_backend.shared_container(
        root, "pg", start=_FakeContainer.start, stop=_FakeContainer.stop
    )


def test_single_start_across_concurrent_holders(tmp_path: Path) -> None:
    _FakeContainer.starts = 0
    _FakeContainer.stops = []
    with _acquire(tmp_path) as url_a:
        with _acquire(tmp_path) as url_b:
            assert url_a == url_b
            assert _FakeContainer.starts == 1  # one real start for two holders
            assert _FakeContainer.stops == []  # not stopped while a holder is active
        assert _FakeContainer.stops == []  # inner release did not stop it
    assert _FakeContainer.stops == ["cid-1"]  # last release stopped exactly once


def test_finish_early_then_reacquire_restarts(tmp_path: Path) -> None:
    _FakeContainer.starts = 0
    _FakeContainer.stops = []
    with _acquire(tmp_path):
        pass  # sole holder finishes -> container stopped, state cleared
    assert _FakeContainer.stops == ["cid-1"]
    with _acquire(tmp_path):
        assert _FakeContainer.starts == 2  # a later holder lazily starts a fresh one
    assert _FakeContainer.stops == ["cid-1", "cid-2"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/support/test_xdist_backend.py -q`
Expected: FAIL — `ModuleNotFoundError: tests.support.xdist_backend`.

- [ ] **Step 3: Write the helper**

```python
# tests/support/__init__.py
# (empty package marker)
```

```python
# tests/support/xdist_backend.py
"""Cross-process coordination for one shared backend container per test run.

Under pytest-xdist each worker is a separate process, so a ``scope="session"``
container fixture would start one container per worker. This helper lets all of a
run's workers share a single container: the per-run temp root holds a
``fcntl.flock`` guard and a refcounted JSON state file, so the first worker starts
the container and the last to leave stops it by id. See ADR-0400.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import fcntl

_POOL_MAX_SIZE = 10  # kdive.db.pool.create_pool default
_HEADROOM = 2
_CONNECTIONS_FLOOR = 500


def xdist_worker_id() -> str:
    """The xdist worker id (``gw0`` …) or ``master`` under a non-xdist run."""
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


def xdist_worker_count() -> int:
    """Number of xdist workers this run scheduled (1 when not under xdist)."""
    raw = os.environ.get("PYTEST_XDIST_WORKER_COUNT", "").strip()
    return int(raw) if raw else 1


def postgres_max_connections() -> int:
    """``max_connections`` sized for every worker's pool, with a fixed floor."""
    return max(_CONNECTIONS_FLOOR, xdist_worker_count() * _POOL_MAX_SIZE * _HEADROOM)


def worker_namespace_token() -> str:
    """A fresh globally-unique token for one worker's database/bucket name."""
    return uuid.uuid4().hex[:12]


def per_run_root(tmp_path_factory) -> Path:
    """The per-run temp root shared across this run's workers.

    Under xdist a worker's basetemp is ``…/pytest-N/popen-gwK``, so ``.parent`` is
    the run-shared ``…/pytest-N``. Under a non-xdist run ``getbasetemp()`` is already
    the per-run ``…/pytest-N`` and ``.parent`` would be the *persistent* per-user
    root, so use ``getbasetemp()`` itself.
    """
    base = Path(tmp_path_factory.getbasetemp())
    return base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base


def with_database_name(url: str, dbname: str) -> str:
    """Return ``url`` with its path component replaced by ``/dbname``."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@contextmanager
def shared_container(
    root: Path,
    name: str,
    *,
    start: Callable[[], tuple[str, str]],
    stop: Callable[[str], None],
) -> Iterator[str]:
    """Yield one shared container's server URL, coordinated across xdist workers.

    ``start()`` returns ``(server_url, container_id)``; ``stop(container_id)`` stops
    it. Exactly one container is alive at a time: the first holder starts it, later
    holders reuse the URL, and the holder that releases last stops it.
    """
    lock_path = root / f"kdive-{name}.lock"
    state_path = root / f"kdive-{name}.json"

    with _locked(lock_path):
        state = _read_state(state_path)
        if state is None:
            url, cid = start()
            state = {"url": url, "container_id": cid, "refcount": 1}
        else:
            state["refcount"] += 1
        _write_state(state_path, state)
        url = state["url"]

    try:
        yield url
    finally:
        with _locked(lock_path):
            state = _read_state(state_path)
            if state is None:
                return
            state["refcount"] -= 1
            if state["refcount"] <= 0:
                stop(state["container_id"])
                state_path.unlink(missing_ok=True)
            else:
                _write_state(state_path, state)


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read_state(state_path: Path) -> dict | None:
    try:
        return json.loads(state_path.read_text())
    except FileNotFoundError:
        return None


def _write_state(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/support/test_xdist_backend.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Lint + type, then commit**

Run: `just lint && just type`
Expected: clean.

```bash
git add tests/support/__init__.py tests/support/xdist_backend.py tests/support/test_xdist_backend.py
git commit -m "test: add xdist shared-backend coordination helper (#1331)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Rework `postgres_url` onto the helper

**Files:**
- Modify: `tests/db/conftest.py`
- Test: `tests/db/test_postgres_url_fixture.py`

**Interfaces:**
- Consumes: `tests.support.xdist_backend` (`per_run_root`, `xdist_worker_id`, `worker_namespace_token`, `postgres_max_connections`, `with_database_name`, `shared_container`).
- Produces: `postgres_url` (session fixture) now yields a per-worker `kdive_test_<worker>_<token>` conninfo on a shared server; `pg_conn`, `migrated_url` unchanged.

- [ ] **Step 1: Write the failing tests**

```python
# tests/db/test_postgres_url_fixture.py
from __future__ import annotations

import pytest

from tests.db import conftest as db_conftest


def test_override_skips_container_and_names_per_worker(
    monkeypatch: pytest.MonkeyPatch, pg_conn
) -> None:
    """With KDIVE_TEST_PG_URL set, no container is started and the conninfo points
    at the override host with a kdive_test_<worker>_<token> database."""
    # pg_conn gives a real (testcontainer or compose) server we can point the
    # override at: reuse its server URL as the override target.
    server = db_conftest._server_url_without_db(pg_conn.info.dsn)  # helper added below
    monkeypatch.setenv("KDIVE_TEST_PG_URL", server)
    started: list[str] = []
    monkeypatch.setattr(db_conftest, "_start_postgres", lambda: started.append("x"))
    gen = db_conftest.postgres_url.__wrapped__(tmp_path_factory=_FakeFactory())
    url = next(gen)
    try:
        assert "/kdive_test_" in url
        assert started == []  # override path never starts a container
    finally:
        gen.close()


def test_require_docker_hard_fails_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_TEST_PG_URL", raising=False)
    monkeypatch.setenv("KDIVE_REQUIRE_DOCKER", "1")
    monkeypatch.setattr(
        db_conftest, "_start_postgres",
        lambda: (_ for _ in ()).throw(RuntimeError("docker down")),
    )
    gen = db_conftest.postgres_url.__wrapped__(tmp_path_factory=_FakeFactory())
    with pytest.raises(RuntimeError):
        next(gen)
```

Notes for the implementer: `postgres_url.__wrapped__` reaches the undecorated
generator so the test can drive it directly; `_FakeFactory` returns a `tmp_path`
via `getbasetemp()`. Because reaching into the fixture internals is awkward, prefer
refactoring the container-start and server-selection into module-level helpers
(`_start_postgres`, `_select_server`, `_server_url_without_db`) that the tests call
directly, and keep the fixture a thin wrapper. Add a tiny `_FakeFactory` in the test
returning `tmp_path`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/db/test_postgres_url_fixture.py -q`
Expected: FAIL — missing `_start_postgres` / `_server_url_without_db`.

- [ ] **Step 3: Rewrite `tests/db/conftest.py`**

```python
"""Disposable-Postgres fixtures for the db tests (ADR-0015, ADR-0400).

`postgres_url` yields a per-worker database on a backend shared for the whole run.
It first honors `KDIVE_TEST_PG_URL` (a running server, e.g. `just compose-up`); with
no override it lazily starts one shared testcontainer coordinated across xdist
workers (`tests/support/xdist_backend`). Each worker owns a `kdive_test_<worker>_<token>`
database; `pg_conn` empties `public` per test. When Docker is unreachable the fixture
skips, unless `KDIVE_REQUIRE_DOCKER=1`, which re-raises so a broken runner can't mask
the suite. On a *persistent* override backend, crashed runs leave `kdive_test_*`
databases that must be swept periodically (ADR-0400 residual).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

from kdive.db import migrate
from tests.support import xdist_backend

_POSTGRES_IMAGE = "postgres:17"


def _start_postgres() -> tuple[str, str]:
    """Start one shared Postgres testcontainer; return (server_url, container_id)."""
    from testcontainers.core.config import testcontainers_config
    from testcontainers.postgres import PostgresContainer

    testcontainers_config.ryuk_disabled = True  # refcount owns lifecycle (ADR-0400)
    container = PostgresContainer(_POSTGRES_IMAGE).with_command(
        f"postgres -c max_connections={xdist_backend.postgres_max_connections()}"
    )
    container.start()
    return container.get_connection_url(driver=None), container.get_wrapped_container().id


def _stop_postgres(container_id: str) -> None:
    from testcontainers.core.docker_client import DockerClient

    DockerClient().client.containers.get(container_id).stop()
    DockerClient().client.containers.get(container_id).remove(force=True)


def _server_url_without_db(url: str) -> str:
    """Strip any database path so we can connect to the server to CREATE DATABASE."""
    return xdist_backend.with_database_name(url, "postgres")


def _provision_worker_db(server_url: str) -> tuple[str, str]:
    """Create this worker's database on `server_url`; return (worker_url, dbname)."""
    dbname = f"kdive_test_{xdist_backend.xdist_worker_id()}_{xdist_backend.worker_namespace_token()}"
    admin = _server_url_without_db(server_url)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{dbname}"')
    return xdist_backend.with_database_name(server_url, dbname), dbname


def _drop_worker_db(server_url: str, dbname: str) -> None:
    admin = _server_url_without_db(server_url)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


@pytest.fixture(scope="session")
def postgres_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    override = os.environ.get("KDIVE_TEST_PG_URL")
    require_docker = os.environ.get("KDIVE_REQUIRE_DOCKER") == "1"

    if override:
        worker_url, dbname = _provision_worker_db(override)
        try:
            yield worker_url
        finally:
            _drop_worker_db(override, dbname)
        return

    try:
        import testcontainers.postgres  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dev dep always present
        if require_docker:
            raise
        pytest.skip(f"testcontainers not installed: {exc}")

    root = xdist_backend.per_run_root(tmp_path_factory)
    try:
        with xdist_backend.shared_container(
            root, "pg", start=_start_postgres, stop=_stop_postgres
        ) as server_url:
            worker_url, dbname = _provision_worker_db(server_url)
            try:
                yield worker_url
            finally:
                _drop_worker_db(server_url, dbname)
    except Exception as exc:  # Docker daemon unreachable / image pull failure.
        if require_docker:
            raise
        pytest.skip(f"Docker unavailable for testcontainers: {exc}")


@pytest.fixture
def pg_conn(postgres_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        yield conn


@pytest.fixture
def migrated_url(pg_conn: psycopg.Connection, postgres_url: str) -> str:
    migrate.apply_migrations(pg_conn)
    return postgres_url
```

Notes: the `except Exception → skip` wraps only the container path (override skips
it). If `_start_postgres` raising must hard-fail under `KDIVE_REQUIRE_DOCKER`, the
re-raise inside the `except` preserves that. Keep the `migrated_url` docstring from
the original if the reviewer wants it; behavior is unchanged.

- [ ] **Step 4: Run the new tests + a slice of the db suite**

Run: `uv run python -m pytest tests/db/test_postgres_url_fixture.py tests/db -q`
Expected: PASS (skips cleanly if Docker absent and `KDIVE_REQUIRE_DOCKER` unset).

- [ ] **Step 5: Lint + type, then commit**

```bash
git add tests/db/conftest.py tests/db/test_postgres_url_fixture.py
git commit -m "test(db): share one Postgres per run, database per worker (#1331)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Rework `minio_store` onto the helper

**Files:**
- Modify: `tests/store/conftest.py`
- Test: `tests/store/test_minio_store_fixture.py`

**Interfaces:**
- Consumes: `tests.support.xdist_backend` (same functions as Task 2).
- Produces: `minio_store` (session fixture) now yields an `ObjectStore` bound to a per-worker `kdive-test-<worker>-<token>` bucket on a shared MinIO; `key_ns` unchanged.

- [ ] **Step 1: Write the failing tests**

```python
# tests/store/test_minio_store_fixture.py
from __future__ import annotations

import pytest

from tests.store import conftest as store_conftest


def test_override_env_selects_endpoint_and_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_TEST_S3_URL", "http://minio.example:9000")
    monkeypatch.delenv("KDIVE_TEST_S3_ACCESS_KEY", raising=False)
    endpoint, access, secret = store_conftest._select_s3_endpoint()
    assert endpoint == "http://minio.example:9000"
    assert access == "minioadmin" and secret == "minioadmin"  # pragma: allowlist secret - compose defaults


def test_bucket_name_is_per_worker_unique() -> None:
    a = store_conftest._worker_bucket_name()
    b = store_conftest._worker_bucket_name()
    assert a != b and a.startswith("kdive-test-") and len(a) <= 63
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/store/test_minio_store_fixture.py -q`
Expected: FAIL — missing `_select_s3_endpoint` / `_worker_bucket_name`.

- [ ] **Step 3: Rewrite `tests/store/conftest.py`**

Keep the existing `_MINIO_IMAGE`, `_MINIO_PORT`, `_ROOT_USER`, `_ROOT_PASSWORD`,
`_REGION`, `_READY_TIMEOUT_S`, `_await_ready`, and `key_ns`. Replace the session
fixture and add module-level helpers:

```python
_DEFAULT_S3_ACCESS_KEY = "minioadmin"  # just compose-up MinIO root
_DEFAULT_S3_SECRET_KEY = "minioadmin"  # pragma: allowlist secret - local dev only


def _select_s3_endpoint() -> tuple[str, str, str]:
    """Return (endpoint, access_key, secret_key) for an override MinIO, if set."""
    endpoint = os.environ["KDIVE_TEST_S3_URL"]
    access = os.environ.get("KDIVE_TEST_S3_ACCESS_KEY", _DEFAULT_S3_ACCESS_KEY)
    secret = os.environ.get("KDIVE_TEST_S3_SECRET_KEY", _DEFAULT_S3_SECRET_KEY)
    return endpoint, access, secret


def _worker_bucket_name() -> str:
    return (
        f"kdive-test-{xdist_backend.xdist_worker_id()}-"
        f"{xdist_backend.worker_namespace_token()}"
    )


def _start_minio() -> tuple[str, str]:
    from testcontainers.core.config import testcontainers_config
    from testcontainers.core.container import DockerContainer

    testcontainers_config.ryuk_disabled = True
    container = (
        DockerContainer(_MINIO_IMAGE)
        .with_command("server /data")
        .with_env("MINIO_ROOT_USER", _ROOT_USER)
        .with_env("MINIO_ROOT_PASSWORD", _ROOT_PASSWORD)
        .with_exposed_ports(_MINIO_PORT)
    )
    container.start()
    endpoint = (
        f"http://{container.get_container_host_ip()}:"
        f"{container.get_exposed_port(_MINIO_PORT)}"
    )
    return endpoint, container.get_wrapped_container().id


def _stop_minio(container_id: str) -> None:
    from testcontainers.core.docker_client import DockerClient

    DockerClient().client.containers.get(container_id).stop()
    DockerClient().client.containers.get(container_id).remove(force=True)


def _s3_client(endpoint: str, access: str, secret: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=_REGION,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _ensure_empty_bucket(client, bucket: str) -> None:
    try:
        client.create_bucket(Bucket=bucket)
    except client.exceptions.BucketAlreadyOwnedByYou:
        _empty_bucket(client, bucket)


def _empty_bucket(client, bucket: str) -> None:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})


@pytest.fixture(scope="session")
def minio_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ObjectStore]:
    override = os.environ.get("KDIVE_TEST_S3_URL")
    require_docker = os.environ.get("KDIVE_REQUIRE_DOCKER") == "1"
    bucket = _worker_bucket_name()

    if override:
        _, access, secret = _select_s3_endpoint()
        client = _s3_client(override, access, secret)
        _await_ready(client)
        _ensure_empty_bucket(client, bucket)
        try:
            yield ObjectStore(client, bucket)
        finally:
            _empty_bucket(client, bucket)
            client.delete_bucket(Bucket=bucket)
        return

    try:
        import testcontainers.core.container  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dev dep always present
        if require_docker:
            raise
        pytest.skip(f"testcontainers not installed: {exc}")

    root = xdist_backend.per_run_root(tmp_path_factory)
    try:
        with xdist_backend.shared_container(
            root, "minio", start=_start_minio, stop=_stop_minio
        ) as endpoint:
            client = _s3_client(endpoint, _ROOT_USER, _ROOT_PASSWORD)
            _await_ready(client)
            _ensure_empty_bucket(client, bucket)
            try:
                yield ObjectStore(client, bucket)
            finally:
                _empty_bucket(client, bucket)
                client.delete_bucket(Bucket=bucket)
    except Exception as exc:  # Docker daemon unreachable / image pull failure.
        if require_docker:
            raise
        pytest.skip(f"Docker unavailable for testcontainers: {exc}")
```

Add `from tests.support import xdist_backend` to the imports. Keep the existing
`from kdive.store.objectstore import ObjectStore`, `boto3`, `Config` imports.

- [ ] **Step 4: Run the new tests + a slice of the store suite**

Run: `uv run python -m pytest tests/store/test_minio_store_fixture.py tests/store -q`
Expected: PASS (skips cleanly when Docker absent and `KDIVE_REQUIRE_DOCKER` unset).

- [ ] **Step 5: Lint + type, then commit**

```bash
git add tests/store/conftest.py tests/store/test_minio_store_fixture.py
git commit -m "test(store): share one MinIO per run, bucket per worker (#1331)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Size the compose Postgres + document required cleanup

**Files:**
- Modify: `docker-compose.yml` (postgres service)
- Modify: `docs/operating/docker-compose.md`

- [ ] **Step 1: Add the postgres `command` in `docker-compose.yml`**

Under the `postgres:` service (currently `image: postgres:17`, `environment:`,
`ports:`, `healthcheck:`), add a `command`:

```yaml
  postgres:
    image: postgres:17
    command: ["postgres", "-c", "max_connections=500"]
    environment:
      POSTGRES_USER: kdive
```

(500 matches the fixture floor so an override run against this backend does not
exhaust connections.)

- [ ] **Step 2: Add the required-cleanup note to `docs/operating/docker-compose.md`**

Add a short subsection stating that when the compose Postgres/MinIO is used as a
test **override** backend (`KDIVE_TEST_PG_URL` / `KDIVE_TEST_S3_URL`), the test
fixtures create per-run `kdive_test_*` databases and `kdive-test-*` buckets, and a
crashed run leaves them behind; periodically drop stale `kdive_test_*` databases (or
recreate the compose volume) — required, not optional (ADR-0400).

- [ ] **Step 3: Validate compose + doc guards**

Run: `docker compose config -q && just docs-links && just docs-paths`
Expected: compose parses; links/paths resolve.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml docs/operating/docker-compose.md
git commit -m "chore(compose): size test-backend max_connections, document cleanup (#1331)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Docker-gated real-container coordination test

**Files:**
- Create: `tests/integration/test_shared_backend_real.py`

**Interfaces:**
- Consumes: `tests.support.xdist_backend.shared_container`, `_start_postgres`/`_stop_postgres` from `tests/db/conftest.py`.

- [ ] **Step 1: Write the Docker-gated test**

```python
# tests/integration/test_shared_backend_real.py
"""Real-container proof that shared_container starts exactly one backend and stops
it by id (AC1 real, AC6b). Skips without Docker; hard-fails under KDIVE_REQUIRE_DOCKER."""
from __future__ import annotations

import os

import psycopg
import pytest

from tests.db import conftest as db_conftest
from tests.support import xdist_backend


def _docker_or_skip() -> None:
    if os.environ.get("KDIVE_REQUIRE_DOCKER") == "1":
        return
    try:
        from testcontainers.core.docker_client import DockerClient

        DockerClient().client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker unavailable: {exc}")


def test_one_real_container_for_two_holders(tmp_path) -> None:
    _docker_or_skip()
    starts: list[str] = []

    def counting_start() -> tuple[str, str]:
        url, cid = db_conftest._start_postgres()
        starts.append(cid)
        return url, cid

    ctx = lambda: xdist_backend.shared_container(  # noqa: E731
        tmp_path, "pg-real", start=counting_start, stop=db_conftest._stop_postgres
    )
    with ctx() as url_a, ctx() as url_b:
        assert url_a == url_b
        assert len(starts) == 1  # one real container for two concurrent holders
        max_conn = xdist_backend.postgres_max_connections()
        with psycopg.connect(
            xdist_backend.with_database_name(url_a, "postgres"), autocommit=True
        ) as conn:
            got = conn.execute("SHOW max_connections").fetchone()[0]
            assert int(got) >= max_conn  # AC6b: server sized for the worker count

    # after the last release the container is gone
    from testcontainers.core.docker_client import DockerClient

    with pytest.raises(Exception):
        DockerClient().client.containers.get(starts[0])
```

- [ ] **Step 2: Run it (with Docker)**

Run: `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/integration/test_shared_backend_real.py -q`
Expected: PASS — one container started, `max_connections >= bound`, container removed after release.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_shared_backend_real.py
git commit -m "test(integration): prove one real backend container per run (#1331)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Full-suite verification and speedup evidence

**Files:** none (verification only).

- [ ] **Step 1: Run the full guardrail suite**

Run: `just ci`
Expected: all recipes green (lint, type, lint-shell, lint-workflows, check-mermaid, test, and the doc/config guards).

- [ ] **Step 2: Capture speedup evidence (AC7)**

Run `just test` and record the wall time; confirm via `-n auto --durations=25` that
the container-layer entries collapse from ~20 `~3s setup` + a serial `stop` tail to a
single start and single stop. Save the before/after numbers for the PR description.

Run: `uv run python -m pytest -m "not live_vm and not live_stack" -n auto --durations=25 -q`
Expected: at most one Postgres and one MinIO container observed via `docker ps`
during the run.

- [ ] **Step 3: No further commit** (verification only; the PR body carries the evidence).

## Rollback

Test-infra + one compose-config bump; no `src`, schema, or migration change. Reverting
Tasks 1–3 + 5 (helper, the two conftests, the new tests) restores container-per-worker
with no data or API impact. The `docker-compose.yml` `max_connections` bump (Task 4) is
independent and safe to leave; revert it for a full undo.

## Self-Review

- **Spec coverage:** AC1 → Task 1 (fake) + Task 5 (real); AC2 → Tasks 2/3 + existing db/store suites under Task 6; AC3 → Tasks 2/3 override tests; AC4 → Task 2/3 idempotency; AC5 → Tasks 2/3 require-docker tests; AC6 → Task 1 acquire/release + finish-early tests; AC6b → Task 5 `SHOW max_connections`; AC7 → Task 6; AC8 → Task 6 `just ci`.
- **Type consistency:** helper names (`per_run_root`, `shared_container`, `with_database_name`, `worker_namespace_token`, `postgres_max_connections`) are used identically in Tasks 2/3/5.
- **Placeholders:** none — every code step shows the code.
