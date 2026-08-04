"""Image-catalog seed helpers shared by reconciler tests."""

from datetime import timedelta
from uuid import UUID, uuid4

import psycopg


async def insert_image_row(
    conn: psycopg.AsyncConnection,
    *,
    provider: str = "local-libvirt",
    name: str = "debian",
    arch: str = "x86_64",
    state: str = "registered",
    visibility: str = "public",
    object_key: str | None = "images/local-libvirt/debian/x86_64.qcow2",
    owner: str | None = None,
    pending_age: timedelta = timedelta(hours=2),
    expires_in: timedelta | None = None,
    kernel_config_key: str | None = None,
    digest: str | None = None,
    size_bytes: int = 0,
    publication_principal: str | None = None,
) -> UUID:
    """Insert an image row with database-clock-relative retention timestamps."""
    expires_clause = "now() + make_interval(secs => %(expires_secs)s)" if expires_in else "NULL"
    cur = await conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, kernel_config_key, digest, "
        " visibility, owner, expires_at, state, size_bytes, publication_attempt_id, "
        "publication_principal, pending_since) "
        "VALUES (%(provider)s, %(name)s, %(arch)s, 'qcow2', '/dev/vda', %(object_key)s, "
        " %(kernel_config_key)s, %(digest)s, %(visibility)s, %(owner)s, "
        f"{expires_clause}, %(state)s, %(size_bytes)s, %(publication_attempt_id)s, "
        "%(publication_principal)s, "
        "now() - make_interval(secs => %(pending_secs)s)) "
        "RETURNING id",
        {
            "provider": provider,
            "name": name,
            "arch": arch,
            "object_key": object_key,
            "kernel_config_key": kernel_config_key,
            "digest": None if object_key is None else digest or "sha256:" + "a" * 64,
            "visibility": visibility,
            "owner": owner,
            "state": state,
            "size_bytes": size_bytes,
            "publication_attempt_id": uuid4() if state == "pending" else None,
            "publication_principal": publication_principal,
            "pending_secs": pending_age.total_seconds(),
            "expires_secs": (expires_in or timedelta()).total_seconds(),
        },
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0]
