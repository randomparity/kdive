"""Disposable-MinIO fixtures for the object-store tests (ADR-0017, ADR-0401).

``minio_store`` yields an :class:`ObjectStore` bound to a per-worker bucket on a
MinIO shared for the whole run. It first honors ``KDIVE_TEST_S3_URL`` (a running
MinIO/S3, e.g. ``just compose-up``, credentials ``KDIVE_TEST_S3_ACCESS_KEY`` /
``KDIVE_TEST_S3_SECRET_KEY`` defaulting to the compose ``minioadmin`` root); with no
override it lazily starts one shared testcontainer coordinated across xdist workers
(``tests/support/xdist_backend``). Each worker owns a ``kdive-test-<worker>-<token>``
bucket; ``key_ns`` gives each test a unique key prefix within it. When Docker is
unreachable the fixture skips, unless ``KDIVE_REQUIRE_DOCKER=1``, which re-raises so a
broken runner cannot mask the suite. On a *persistent* override backend, crashed runs
leave ``kdive-test-*`` buckets that must be swept periodically (ADR-0401 residual).

MinIO's official image is archived (final tag pinned below); if it stops resolving,
swap in localstack or a Chainguard MinIO rebuild (ADR-0017).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any
from uuid import uuid4

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from kdive.store.objectstore import ObjectStore
from tests.support import xdist_backend

# MinIO's official images are archived; the last tag actually pushed to Docker Hub
# is RELEASE.2025-09-07T16-13-09Z (the later source-only 2025-10-15 patch was never
# published as an image). Pinned for the disposable test container; if it stops
# resolving, swap to a Chainguard MinIO rebuild or a localstack S3 fixture (ADR-0017).
_MINIO_IMAGE = "minio/minio:RELEASE.2025-09-07T16-13-09Z"
_MINIO_PORT = 9000
_ROOT_USER = "kdive-test"
_ROOT_PASSWORD = "kdive-test-secret"  # disposable local test container credential
_REGION = "us-east-1"
_READY_TIMEOUT_S = 60.0
_DEFAULT_S3_ACCESS_KEY = "minioadmin"  # just compose-up MinIO root
_DEFAULT_S3_SECRET_KEY = "minioadmin"  # pragma: allowlist secret - local dev only


def _await_ready(client: Any) -> None:
    """Poll ``list_buckets`` until MinIO answers or the timeout elapses."""
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            client.list_buckets()
            return
        except (BotoCoreError, ClientError, OSError) as exc:
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(f"MinIO not ready within {_READY_TIMEOUT_S}s: {last_exc}")


def _select_s3_endpoint() -> tuple[str, str, str]:
    """Return (endpoint, access_key, secret_key) for an override MinIO, if set."""
    endpoint = os.environ["KDIVE_TEST_S3_URL"]
    access = os.environ.get("KDIVE_TEST_S3_ACCESS_KEY", _DEFAULT_S3_ACCESS_KEY)
    secret = os.environ.get("KDIVE_TEST_S3_SECRET_KEY", _DEFAULT_S3_SECRET_KEY)
    return endpoint, access, secret


def _worker_bucket_name() -> str:
    return f"kdive-test-{xdist_backend.xdist_worker_id()}-{xdist_backend.worker_namespace_token()}"


def _start_minio() -> tuple[str, str]:
    from testcontainers.core.config import testcontainers_config
    from testcontainers.core.container import DockerContainer

    testcontainers_config.ryuk_disabled = True  # refcount owns lifecycle (ADR-0401)
    container = (
        DockerContainer(_MINIO_IMAGE)
        .with_command("server /data")
        .with_env("MINIO_ROOT_USER", _ROOT_USER)
        .with_env("MINIO_ROOT_PASSWORD", _ROOT_PASSWORD)
        .with_exposed_ports(_MINIO_PORT)
    )
    container.start()
    endpoint = (
        f"http://{container.get_container_host_ip()}:{container.get_exposed_port(_MINIO_PORT)}"
    )
    return endpoint, container.get_wrapped_container().id


def _stop_minio(container_id: str) -> None:
    import docker.errors
    from testcontainers.core.docker_client import DockerClient

    with suppress(docker.errors.NotFound):  # already reaped
        DockerClient().client.containers.get(container_id).remove(force=True)


def _s3_client(endpoint: str, access: str, secret: str) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=_REGION,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _ensure_empty_bucket(client: Any, bucket: str) -> None:
    """Create the bucket if absent, then always empty it (handles a same-token retry
    where the bucket already exists, and MinIO/us-east-1 returning 200 for an owned
    bucket rather than raising)."""
    with suppress(client.exceptions.BucketAlreadyOwnedByYou, client.exceptions.BucketAlreadyExists):
        client.create_bucket(Bucket=bucket)
    _empty_bucket(client, bucket)  # unconditional: no-op on a fresh bucket


def _empty_bucket(client: Any, bucket: str) -> None:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})


@contextmanager
def _acquire_minio_endpoint(
    tmp_path_factory: pytest.TempPathFactory, *, require_docker: bool
) -> Iterator[tuple[str, str, str]]:
    """Yield (endpoint, access_key, secret_key): the override if set, else a shared
    container. Extracted so the override / require-docker / skip decisions are directly
    testable (the tests monkeypatch ``_start_minio``)."""
    if os.environ.get("KDIVE_TEST_S3_URL"):
        yield _select_s3_endpoint()
        return

    try:
        import testcontainers.core.container  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dev dep always present
        if require_docker:
            raise
        pytest.skip(f"testcontainers not installed: {exc}")

    root = xdist_backend.per_run_root(tmp_path_factory)
    with xdist_backend.shared_container_or_skip(
        root, "minio", start=_start_minio, stop=_stop_minio, require_docker=require_docker
    ) as endpoint:
        yield endpoint, _ROOT_USER, _ROOT_PASSWORD


@pytest.fixture(scope="session")
def minio_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ObjectStore]:
    require_docker = os.environ.get("KDIVE_REQUIRE_DOCKER") == "1"
    bucket = _worker_bucket_name()
    with _acquire_minio_endpoint(tmp_path_factory, require_docker=require_docker) as (
        endpoint,
        access,
        secret,
    ):
        client = _s3_client(endpoint, access, secret)
        _await_ready(client)
        _ensure_empty_bucket(client, bucket)
        try:
            yield ObjectStore(client, bucket)
        finally:
            _empty_bucket(client, bucket)
            client.delete_bucket(Bucket=bucket)


@pytest.fixture
def key_ns() -> str:
    """A per-test unique key prefix (used as the ``tenant`` component)."""
    return uuid4().hex
