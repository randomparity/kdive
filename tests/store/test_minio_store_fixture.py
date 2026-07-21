from __future__ import annotations

from pathlib import Path

import pytest

from tests.store import conftest as store_conftest


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

    def _boom() -> tuple[str, str]:
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

    def _boom() -> tuple[str, str]:
        raise RuntimeError("docker down")

    monkeypatch.setattr(store_conftest, "_start_minio", _boom)
    with (
        pytest.raises(RuntimeError),
        store_conftest._acquire_minio_endpoint(tmp_path_factory, require_docker=True),
    ):
        pass


def test_readiness_error_propagates_not_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.delenv("KDIVE_TEST_S3_URL", raising=False)
    _isolate_root(monkeypatch, tmp_path)
    monkeypatch.setattr(store_conftest, "_start_minio", lambda: ("http://h:9000", "cid"))
    monkeypatch.setattr(store_conftest, "_stop_minio", lambda _cid: None)
    with (
        pytest.raises(ValueError),  # a body error must NOT become pytest.skip
        store_conftest._acquire_minio_endpoint(tmp_path_factory, require_docker=False),
    ):
        raise ValueError("minio never became ready")
