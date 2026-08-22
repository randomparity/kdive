"""Shared fixtures for systemd worker lifecycle tests."""

from __future__ import annotations


def start_payload(**overrides: object) -> dict[str, object]:
    """Return the complete, explicitly allowed worker-runtime request."""
    settings: dict[str, object] = {
        "python": "/usr/bin/python3",
        "source_root": "/srv/kdive",
        "rootfs_dir": "/var/lib/kdive/rootfs",
        "build_workspace": "/var/lib/kdive/build",
        "build_component_roots": "/srv/kdive/fixtures",
        "install_staging": "/var/lib/kdive/install",
        "fixture_catalog_path": "/srv/kdive/fixtures/catalog.yaml",
        "worker_database_url": "postgresql://worker:password@db/kdive",  # pragma: allowlist secret
        "libvirt_uri": "qemu+unix:///session?socket=/run/libvirt/virtqemud-sock",
        "s3_endpoint_url": "http://minio:9000",
        "s3_bucket": "kdive-artifacts",
        "s3_region": "us-east-1",
        "aws_access_key_id": "access-key",
        "aws_secret_access_key": "secret-key",  # pragma: allowlist secret
        "accepted_lanes": ["default", "state-fenced"],
        "build_user": "builder",
        "log_level": "INFO",
        "health_binds": {"1": "127.0.0.1:9465", "2": "127.0.0.1:9470"},
    }
    payload: dict[str, object] = {
        "operation": "start",
        "worker_count": 2,
        "settings": settings,
    }
    for name in tuple(overrides):
        if name in settings:
            settings[name] = overrides.pop(name)
    payload.update(overrides)
    return payload
