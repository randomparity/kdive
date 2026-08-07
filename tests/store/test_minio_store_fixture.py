from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.store import conftest as store_conftest
from tests.support import xdist_backend


def _isolate_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Point per_run_root at a private dir so these direct _acquire_minio_endpoint
    calls do not read or perturb the real session container's coordination state."""
    monkeypatch.setattr(store_conftest.xdist_backend, "per_run_root", lambda _factory: root)


def test_override_env_selects_endpoint_and_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_TEST_S3_URL", "http://minio.example:9000")
    monkeypatch.delenv("KDIVE_TEST_S3_ACCESS_KEY", raising=False)
    endpoint, access, secret = store_conftest._select_s3_endpoint()
    assert endpoint == "http://minio.example:9000"
    assert access == "minioadmin"  # pragma: allowlist secret - compose defaults
    assert secret == "minioadmin"  # pragma: allowlist secret - compose defaults


def test_bucket_name_is_per_worker_unique() -> None:
    a = store_conftest._worker_bucket_name()
    b = store_conftest._worker_bucket_name()
    assert a != b and a.startswith("kdive-test-") and len(a) <= 63


def test_override_selected_without_starting_a_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.setenv("KDIVE_TEST_S3_URL", "http://minio.example:9000")

    def _boom(_labels: Mapping[str, str]) -> tuple[str, str]:
        raise AssertionError("override path must not start a container")

    monkeypatch.setattr(store_conftest, "_start_minio", _boom)
    with store_conftest._acquire_minio_endpoint(tmp_path_factory, require_docker=False) as (
        endpoint,
        _access,
        _secret,
    ):
        assert endpoint == "http://minio.example:9000"


def test_require_docker_reraises_start_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.delenv("KDIVE_TEST_S3_URL", raising=False)
    _isolate_root(monkeypatch, tmp_path)

    def _boom(_labels: Mapping[str, str]) -> tuple[str, str]:
        raise RuntimeError("docker down")

    monkeypatch.setattr(store_conftest, "_start_minio", _boom)
    with (
        pytest.raises(RuntimeError),
        store_conftest._acquire_minio_endpoint(tmp_path_factory, require_docker=True),
    ):
        pass


def test_no_docker_skips_when_not_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.delenv("KDIVE_TEST_S3_URL", raising=False)
    _isolate_root(monkeypatch, tmp_path)

    def _boom(_labels: Mapping[str, str]) -> tuple[str, str]:
        raise RuntimeError("docker down")

    monkeypatch.setattr(store_conftest, "_start_minio", _boom)
    monkeypatch.setattr(store_conftest.xdist_backend, "docker_available", lambda: False)
    with (
        pytest.raises(pytest.skip.Exception),
        store_conftest._acquire_minio_endpoint(tmp_path_factory, require_docker=False),
    ):
        pass


def test_stop_minio_removes_the_containers_anonymous_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`remove` must be called with `v=True`.

    The MinIO image declares VOLUME /data, so dropping `v` leaks one dangling
    anonymous volume per run — holding every artifact the run uploaded. The suite
    stays green either way, so the call is the only in-process observable.
    """
    import testcontainers.core.docker_client as tc_docker

    calls: dict[str, object] = {}

    class _Container:
        def remove(self, **kwargs: object) -> None:
            calls.update(kwargs)

    class _Containers:
        def get(self, container_id: str) -> _Container:
            calls["container_id"] = container_id
            return _Container()

    class _Inner:
        containers = _Containers()

    class _DockerClient:
        client = _Inner()

    monkeypatch.setattr(tc_docker, "DockerClient", _DockerClient)
    store_conftest._stop_minio("deadbeef")
    assert calls["container_id"] == "deadbeef"
    assert calls["v"] is True, "removal must delete the anonymous data volume"
    assert calls["force"] is True


def test_readiness_error_propagates_not_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.delenv("KDIVE_TEST_S3_URL", raising=False)
    _isolate_root(monkeypatch, tmp_path)
    monkeypatch.setattr(store_conftest, "_start_minio", lambda _labels: ("http://h:9000", "cid"))
    monkeypatch.setattr(store_conftest, "_stop_minio", lambda _cid: None)
    with (
        pytest.raises(ValueError),  # a body error must NOT become pytest.skip
        store_conftest._acquire_minio_endpoint(tmp_path_factory, require_docker=False),
    ):
        raise ValueError("minio never became ready")


def test_start_minio_stamps_the_reaper_labels_on_the_real_container(tmp_path: Path) -> None:
    """The MinIO half of the crash reaper, against a real daemon (ADR-0551, #1910).

    `_start_minio` builds its container through a different testcontainers class than
    `_start_postgres`, so the Postgres proof covers none of this path. It needs its own
    because `with_kwargs` is assignment, not merge (`testcontainers/core/container.py`):
    a second `.with_kwargs(...)` added later silently drops these labels, the MinIO
    reaper goes inert, the leak returns — and every mock-based test here stays green.
    """
    xdist_backend.skip_without_docker()
    import docker

    client = docker.from_env()
    labels = xdist_backend.backend_container_labels(tmp_path, "minio")
    # Held for the container's whole life: this host runs concurrent suites, and a free
    # lock is the signal that tells another run's sweep to reap it mid-test.
    with xdist_backend._liveness_held(tmp_path, "minio"):
        _endpoint, container_id = store_conftest._start_minio(labels)
        try:
            container = client.containers.get(container_id)
            assert container.labels[xdist_backend.BACKEND_LABEL] == "minio"
            assert (
                container.labels[xdist_backend.LIVENESS_LABEL]
                == labels[xdist_backend.LIVENESS_LABEL]
            )
        finally:
            store_conftest._stop_minio(container_id)
