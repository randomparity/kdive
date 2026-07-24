"""The per-investigation staging-dir sweep the reclaim handler runs once a drain completes.

A crash-orphaned ``<token>.*.partial`` no row owns is unlinked before the empty-dir removal (else
it keeps the dir non-empty forever); a dir still holding a base is left in place (ADR-0441 §5).
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from kdive.jobs.handlers.artifacts.rootfs_reclaim import sweep_investigation_staging_dir

_TOKEN = "dGVzdC10b2tlbg"  # an arbitrary base64url content-address token


def test_partial_sweep_unlinks_and_removes_empty_dir(tmp_path: Path) -> None:
    # AC-8h: a crash-orphaned <token>.*.partial is swept before the now-empty dir is removed.
    inv = uuid4()
    inv_dir = tmp_path / str(inv)
    inv_dir.mkdir(parents=True)
    orphan = inv_dir / f"{_TOKEN}.{uuid4().hex}.partial"
    orphan.write_bytes(b"partial")
    sweep_investigation_staging_dir(str(tmp_path), inv)
    assert not orphan.exists()
    assert not inv_dir.exists()  # empty after the partial swept -> removed


def test_partial_sweep_keeps_dir_holding_a_base(tmp_path: Path) -> None:
    inv = uuid4()
    inv_dir = tmp_path / str(inv)
    inv_dir.mkdir(parents=True)
    (inv_dir / f"{_TOKEN}.{uuid4().hex}.partial").write_bytes(b"partial")
    base = inv_dir / f"{_TOKEN}.qcow2"
    base.write_bytes(b"base")  # a still-deferred base keeps the dir non-empty
    sweep_investigation_staging_dir(str(tmp_path), inv)
    assert inv_dir.exists()
    assert base.exists()
    assert not list(inv_dir.glob("*.partial"))  # partial still swept
