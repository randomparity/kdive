from __future__ import annotations

from pathlib import Path


def test_compose_uses_owned_seaweedfs_and_new_volume() -> None:
    compose = Path("docker-compose.yml").read_text()

    assert "seaweedfs:" in compose
    assert "build: ./deploy/seaweedfs" in compose
    assert "kdive-seaweedfs-data:/data" in compose
    assert "kdive-minio-data:/data" not in compose
    assert '"-m", "kdive.store.initialize_bucket"' in compose
    assert "http://seaweedfs:8333" in compose
