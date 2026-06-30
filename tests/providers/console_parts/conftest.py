"""Shared fixtures for the console-parts provider tests.

Re-exports the disposable-MinIO fixtures so sidecar tests can write and read objects
against a real object store — the same pattern as the remote-libvirt conftest's
re-export of the disposable-Postgres fixtures.
"""

from __future__ import annotations

from tests.store.conftest import key_ns, minio_store

__all__ = ["key_ns", "minio_store"]
