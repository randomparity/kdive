"""Remote-libvirt wiring for the ``kdive stage-volume`` operator command (ADR-0336)."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

import kdive.config as config
from kdive.config.core_settings import DATABASE_URL
from kdive.domain.catalog.images import ImageVisibility
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.cataloging.object_keys import config_write_request
from kdive.images.planes.provenance_probes import inspect_root_boot
from kdive.images.rootfs.stage_volume import StageVolumeDeps, _TargetRow, capture_kernel_config
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, all_remote_configs_by_name
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
from kdive.store.objectstore import object_store_from_env


def _resolve_single_remote_config(provider: str) -> RemoteLibvirtConfig:
    """Return the lone remote config, or raise an actionable configuration error."""
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
            "SELECT id, volume, state, publication_attempt_id FROM image_catalog "
            "WHERE provider = %s AND name = %s AND arch = %s AND managed_by = 'config' "
            "  AND volume IS NOT NULL AND path IS NULL AND object_key IS NULL "
            "  AND visibility = 'public' AND state = 'registered' AND format = 'qcow2'",
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
    return _TargetRow(row_id=row[0], volume=row[1], state=row[2], publication_attempt_id=row[3])


def _upload_volume(config_: RemoteLibvirtConfig, volume: str, qcow2: Path) -> None:
    """Open one mutual-TLS connection and stream ``qcow2`` into ``volume`` on the host pool."""
    backend = secret_backend_from_env(registry=SecretRegistry())
    with remote_connection(config_, backend, open_connection=open_libvirt_protocol) as conn:
        upload_qcow2_volume(
            cast("VolumeUploadConn", conn), config_.storage_pool, volume, qcow2, require_new=True
        )


def _attach_config(provider: str, name: str, arch: str, row_id: UUID, config_bytes: bytes) -> None:
    """Upload the captured config and set the row's ``kernel_config_key`` (advisory step)."""
    request = config_write_request(
        provider, name, arch, ImageVisibility.PUBLIC, None, config=config_bytes
    )
    object_store_from_env().put_artifact(request)
    try:
        with psycopg.connect(config.require(DATABASE_URL)) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE image_catalog SET kernel_config_key = %s WHERE id = %s",
                (request.key(), row_id),
            )
            conn.commit()
    except psycopg.Error as exc:
        raise CategorizedError(
            "stage-volume could not set kernel_config_key after uploading the config",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"image": f"{provider}/{name}/{arch}"},
        ) from exc


def _persist_root(
    provider: str,
    name: str,
    arch: str,
    target: _TargetRow,
    digest: str,
    root: dict[str, object],
) -> None:
    """Commit the matching root/digest only while the resolved public staged target remains."""
    try:
        with psycopg.connect(config.require(DATABASE_URL)) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE image_catalog SET digest = %s, provenance = provenance || %s, "
                "provenance_attested = false WHERE id = %s AND provider = %s AND name = %s "
                "AND arch = %s AND volume = %s AND state = %s "
                "AND publication_attempt_id IS NOT DISTINCT FROM %s "
                "AND managed_by = 'config' AND visibility = 'public' AND format = 'qcow2' "
                "AND path IS NULL AND object_key IS NULL RETURNING id",
                (
                    digest,
                    Jsonb({"root_spec": root}),
                    target.row_id,
                    provider,
                    name,
                    arch,
                    target.volume,
                    target.state,
                    target.publication_attempt_id,
                ),
            )
            if cur.fetchone() is None:
                raise CategorizedError(
                    "stage-volume target changed after upload; remote bytes remain unverified; "
                    "inspect and manually clean up the volume or declare a fresh target",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                )
            conn.commit()
    except psycopg.Error as exc:
        raise CategorizedError(
            "stage-volume could not persist root metadata after upload; remote bytes remain "
            "unverified; inspect and manually clean up the volume or declare a fresh target",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc


def build_stage_volume_deps(provider: str) -> StageVolumeDeps:
    """Wire the remote-libvirt dependencies for one ``stage-volume`` run."""
    remote = _resolve_single_remote_config(provider)
    return StageVolumeDeps(
        find_row=_find_staged_row,
        capture_config=capture_kernel_config,
        upload_volume=lambda volume, qcow2: _upload_volume(remote, volume, qcow2),
        attach_config=_attach_config,
        inspect_root=inspect_root_boot,
        persist_root=_persist_root,
    )
