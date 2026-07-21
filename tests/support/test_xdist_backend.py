from __future__ import annotations

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


def test_corrupt_state_file_warns_and_starts_fresh(tmp_path: Path) -> None:
    _FakeContainer.starts = 0
    _FakeContainer.stops = []
    (tmp_path / "kdive-pg.json").write_text("{ partial")  # externally-corrupted file
    # must not raise JSONDecodeError; warns (does not silently mask) then starts fresh
    with pytest.warns(UserWarning, match="corrupt"), _acquire(tmp_path) as url:
        assert url.endswith("/test")
        assert _FakeContainer.starts == 1


def test_start_then_write_failure_stops_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeContainer.starts = 0
    _FakeContainer.stops = []

    def _boom_write(_path: Path, _state: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(xdist_backend, "_write_state", _boom_write)
    # start() succeeds but recording state fails: the started container must be stopped
    # (not leaked) and the real write error must propagate (not be swallowed).
    with pytest.raises(OSError, match="disk full"), _acquire(tmp_path):
        pass
    assert _FakeContainer.starts == 1
    assert _FakeContainer.stops == ["cid-1"]  # stopped despite never being recorded


def test_stop_failure_warns_but_does_not_raise_and_unlinks(tmp_path: Path) -> None:
    def boom(_cid: str) -> None:
        raise RuntimeError("already removed")

    # teardown must be best-effort: warn, not raise (so it can never mask a body error
    # or wedge the run), and always unlink so the next run starts clean.
    with (
        pytest.warns(UserWarning, match="stop"),
        xdist_backend.shared_container(tmp_path, "pg", start=_FakeContainer.start, stop=boom),
    ):
        pass  # sole holder; refcount hits 0 and stop() raises internally
    assert not (tmp_path / "kdive-pg.json").exists()
