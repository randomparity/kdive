"""Env-backed seam wiring for ``kdive stage-volume`` (ADR-0336).

Split from :mod:`kdive.images.rootfs.stage_volume` so the orchestration stays a pure, unit-tested
function while the process-level wiring — a sync DB connection, the object store, and the mutual-TLS
libvirt volume upload — lives behind :func:`build_stage_volume_deps`.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import UUID

import psycopg

import kdive.config as config
from kdive.config.core_settings import DATABASE_URL
from kdive.domain.catalog.images import ImageVisibility
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.rootfs.stage_volume import (
    StageVolumeDeps,
    _TargetRow,
    capture_kernel_config,
)
from kdive.providers.remote_libvirt.config import (
    RemoteLibvirtConfig,
    all_remote_configs_by_name,
)
from kdive.providers.remote_libvirt.connection.transport import (
    open_libvirt_protocol,
    remote_connection,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.volume_upload import (
    VolumeUploadConn,
    upload_qcow2_volume,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import secret_backend_from_env
from kdive.services.images.publish import config_write_request
from kdive.store.objectstore import object_store_from_env


def _resolve_single_remote_config(provider: str) -> RemoteLibvirtConfig:
    """The lone declared ``[[remote_libvirt]]`` instance's config, or a fix-hint failure.

    ``stage-volume`` targets a host; with exactly one declared instance the choice is unambiguous.
    Zero or many instances fail with a ``CONFIGURATION_ERROR`` naming the declared instances so the
    operator declares or disambiguates before staging.
    """
    if provider != "remote-libvirt":
        raise CategorizedError(
            f"stage-volume supports provider 'remote-libvirt', not {provider!r}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider},
        )
    instances = all_remote_configs_by_name()
    if len(instances) != 1:
        names = sorted(name for name, _ in instances)
        raise CategorizedError(
            "stage-volume needs exactly one declared [[remote_libvirt]] instance; "
            f"found {len(instances)} ({names})",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"instances": ", ".join(names)},
        )
    return instances[0][1]


def _find_staged_row(provider: str, name: str, arch: str) -> _TargetRow:
    """Resolve the target ``staged`` volume catalog row, or fail fast if it is absent."""
    with psycopg.connect(config.require(DATABASE_URL)) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, volume FROM image_catalog "
            "WHERE provider = %s AND name = %s AND arch = %s AND managed_by = 'config' "
            "  AND volume IS NOT NULL",
            (provider, name, arch),
        )
        row = cur.fetchone()
    if row is None:
        raise CategorizedError(
            f"no staged [[image]] {name!r} ({arch}) is declared for {provider!r}; declare and "
            "reconcile the [[image]] (kind = 'staged') before staging its volume",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"provider": provider, "name": name, "arch": arch},
        )
    return _TargetRow(row_id=row[0], volume=row[1])


def _upload_volume(config_: RemoteLibvirtConfig, volume: str, qcow2: Path) -> None:
    """Open one mutual-TLS connection and stream ``qcow2`` into ``volume`` on the host pool."""
    backend = secret_backend_from_env(registry=SecretRegistry())
    with remote_connection(config_, backend, open_connection=open_libvirt_protocol) as conn:
        upload_qcow2_volume(cast("VolumeUploadConn", conn), config_.storage_pool, volume, qcow2)


def _attach_config(provider: str, name: str, arch: str, row_id: UUID, config_bytes: bytes) -> None:
    """Upload the captured config and set the row's ``kernel_config_key`` (advisory step)."""
    request = config_write_request(
        provider, name, arch, ImageVisibility.PUBLIC, None, config=config_bytes
    )
    object_store_from_env().put_artifact(request)
    with psycopg.connect(config.require(DATABASE_URL)) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE image_catalog SET kernel_config_key = %s WHERE id = %s",
            (request.key(), row_id),
        )
        conn.commit()


def build_stage_volume_deps(provider: str) -> StageVolumeDeps:
    """Wire the env-backed :class:`StageVolumeDeps` for one ``stage-volume`` run."""
    remote = _resolve_single_remote_config(provider)
    return StageVolumeDeps(
        find_row=_find_staged_row,
        capture_config=capture_kernel_config,
        upload_volume=lambda volume, qcow2: _upload_volume(remote, volume, qcow2),
        attach_config=_attach_config,
    )
