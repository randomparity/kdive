"""Migration 0023 — image_catalog schema, CHECKs, and partial unique indexes (ADR-0092/0093).

The DB-level invariants are the catalog's safety net: a private row must carry an owner and an
expiry; a `defined` row must have no object and a non-`defined` row must; one registered public
image per identity and one registered private image per (owner, provider, name); but a `pending`
duplicate is admitted so a crashed publish never wedges retry.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from kdive.db import migrate
from kdive.domain.catalog.images import ImageState, ImageVisibility


def _columns(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {name: dtype for name, dtype in rows}


def _nullable(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {name: is_nullable for name, is_nullable in rows}


def _apply_through(conn: psycopg.Connection, version: str) -> None:
    for migration in migrate.discover_migrations():
        if migration.version <= version:
            conn.execute(migration.sql.encode())


def _insert_image(conn: psycopg.Connection, **overrides: object) -> None:
    """Insert one image_catalog row, defaulting to a registered public image."""
    row: dict[str, object] = {
        "provider": "local-libvirt",
        "name": "base",
        "arch": "x86_64",
        "format": "qcow2",
        "root_device": "/dev/vda",
        "object_key": "images/local-libvirt/base/x86_64.qcow2",
        "digest": "sha256:abc",
        "visibility": "public",
        "owner": None,
        "expires_at": None,
        "state": "registered",
    }
    row.update(overrides)
    if row["state"] == ImageState.PENDING.value:
        row.setdefault("publication_attempt_id", uuid4())
    columns = list(row.keys())
    query = sql.SQL("INSERT INTO image_catalog ({cols}) VALUES ({vals})").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        vals=sql.SQL(", ").join(sql.Placeholder(c) for c in columns),
    )
    conn.execute(query, row)


def test_migration_0023_creates_image_catalog(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    cols = _columns(pg_conn, "image_catalog")
    assert cols.get("provider") == "text"
    assert cols.get("name") == "text"
    assert cols.get("arch") == "text"
    assert cols.get("format") == "text"
    assert cols.get("root_device") == "text"
    assert cols.get("object_key") == "text"
    assert cols.get("digest") == "text"
    assert cols.get("capabilities") == "ARRAY"
    assert cols.get("provenance") == "jsonb"
    assert cols.get("visibility") == "text"
    assert cols.get("owner") == "text"
    assert cols.get("expires_at") == "timestamp with time zone"
    assert cols.get("state") == "text"
    assert cols.get("pending_since") == "timestamp with time zone"
    assert cols.get("publication_attempt_id") == "uuid"
    assert cols.get("publication_principal") == "text"
    assert cols.get("created_at") == "timestamp with time zone"
    assert cols.get("updated_at") == "timestamp with time zone"


def test_nullable_columns(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    nullable = _nullable(pg_conn, "image_catalog")
    # object_key/digest/owner/expires_at are conditionally present (CHECK-bound), so nullable.
    for col in (
        "object_key",
        "digest",
        "owner",
        "expires_at",
        "publication_attempt_id",
        "publication_principal",
    ):
        assert nullable.get(col) == "YES", col
    # identity + state columns are always present.
    for col in ("provider", "name", "arch", "format", "root_device", "visibility", "state"):
        assert nullable.get(col) == "NO", col


def test_publication_attempt_columns_are_nullable_for_predecessor_writers(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    columns = _columns(pg_conn, "image_catalog")
    nullable = _nullable(pg_conn, "image_catalog")

    assert columns["publication_attempt_id"] == "uuid"
    assert columns["publication_principal"] == "text"
    assert nullable["publication_attempt_id"] == "YES"
    assert nullable["publication_principal"] == "YES"


def test_visibility_check_rejects_unknown(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, visibility="internal")


def test_state_check_rejects_unknown(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, state="published")


def test_private_row_requires_owner(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, visibility="private", owner=None, expires_at="2099-01-01T00:00:00Z")


def test_public_row_rejects_owner(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, visibility="public", owner="proj", expires_at=None)


def test_private_row_requires_expiry(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, visibility="private", owner="proj", expires_at=None)


def test_public_row_rejects_expiry(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, visibility="public", owner=None, expires_at="2099-01-01T00:00:00Z")


def test_private_row_accepts_owner_and_expiry(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(
        pg_conn,
        visibility="private",
        owner="proj",
        expires_at="2099-01-01T00:00:00Z",
    )
    row = pg_conn.execute("SELECT owner FROM image_catalog WHERE visibility = 'private'").fetchone()
    assert row is not None and row[0] == "proj"


def test_defined_row_requires_null_object_key(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, state="defined", object_key="images/x", digest=None)


def test_non_defined_row_requires_object_key(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(pg_conn, state="pending", object_key=None, digest=None)


def test_defined_row_accepts_null_object_key(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="defined", object_key=None, digest=None)
    row = pg_conn.execute("SELECT object_key FROM image_catalog WHERE state = 'defined'").fetchone()
    assert row is not None and row[0] is None


def test_staged_path_row_accepts_path_only(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(
        pg_conn,
        state="registered",
        object_key=None,
        volume=None,
        path="/var/lib/kdive/rootfs/x.img",
        digest=None,
    )
    row = pg_conn.execute("SELECT path FROM image_catalog WHERE state = 'registered'").fetchone()
    assert row is not None and row[0] == "/var/lib/kdive/rootfs/x.img"


def test_registered_row_rejects_two_of_three_sources(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(
            pg_conn,
            state="registered",
            object_key="images/x",
            volume=None,
            path="/var/lib/kdive/rootfs/x.img",
            digest="sha256:abc",
        )


def test_registered_row_rejects_no_source(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(
            pg_conn, state="registered", object_key=None, volume=None, path=None, digest=None
        )


def test_defined_row_rejects_path(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_image(
            pg_conn,
            state="defined",
            object_key=None,
            volume=None,
            path="/var/lib/kdive/rootfs/x.img",
            digest=None,
        )


def test_two_registered_public_same_identity_rejected(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, object_key="images/a")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_image(pg_conn, object_key="images/b")


def test_pending_duplicate_admitted(pg_conn: psycopg.Connection) -> None:
    # A crashed publish's leftover `pending` row must never block a re-publish of the same
    # identity, so the partial unique index covers `registered` only.
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="pending", object_key="images/a")
    _insert_image(pg_conn, state="pending", object_key="images/b")
    row = pg_conn.execute("SELECT count(*) FROM image_catalog WHERE state = 'pending'").fetchone()
    assert row is not None and row[0] == 2


def test_registered_coexists_with_pending_same_identity(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="registered", object_key="images/a")
    _insert_image(pg_conn, state="pending", object_key="images/b")


def test_two_defined_public_same_identity_rejected(pg_conn: psycopg.Connection) -> None:
    # Seed idempotency at the DB level: one `defined` baseline per public identity.
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="defined", object_key=None, digest=None)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_image(pg_conn, state="defined", object_key=None, digest=None)


def test_two_registered_private_same_identity_rejected(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(
        pg_conn,
        visibility="private",
        owner="proj",
        expires_at="2099-01-01T00:00:00Z",
        object_key="images/a",
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_image(
            pg_conn,
            visibility="private",
            owner="proj",
            expires_at="2099-01-01T00:00:00Z",
            object_key="images/b",
        )


def test_two_projects_register_same_private_name(pg_conn: psycopg.Connection) -> None:
    # The private unique index is keyed by (owner, provider, name): two projects may both hold
    # a registered private image of the same name.
    migrate.apply_migrations(pg_conn)
    _insert_image(
        pg_conn,
        visibility="private",
        owner="proj-a",
        expires_at="2099-01-01T00:00:00Z",
        object_key="images/a",
    )
    _insert_image(
        pg_conn,
        visibility="private",
        owner="proj-b",
        expires_at="2099-01-01T00:00:00Z",
        object_key="images/b",
    )


def test_updated_at_trigger_bumps_on_update(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="pending", object_key="images/a")
    before = pg_conn.execute(
        "SELECT updated_at FROM image_catalog WHERE state = 'pending'"
    ).fetchone()
    assert before is not None
    pg_conn.execute(
        "UPDATE image_catalog SET state = 'registered', publication_attempt_id = NULL "
        "WHERE state = 'pending'"
    )
    after = pg_conn.execute(
        "SELECT updated_at FROM image_catalog WHERE state = 'registered'"
    ).fetchone()
    assert after is not None and after[0] > before[0]


def test_state_check_covers_every_enum_value(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'image_state_check'"
    ).fetchone()
    assert row is not None
    definition = row[0]
    missing = [m.value for m in ImageState if f"'{m.value}'" not in definition]
    assert not missing, f"image_state_check is missing {missing}"


def test_visibility_check_covers_every_enum_value(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    row = pg_conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'image_visibility_check'"
    ).fetchone()
    assert row is not None
    definition = row[0]
    missing = [m.value for m in ImageVisibility if f"'{m.value}'" not in definition]
    assert not missing, f"image_visibility_check is missing {missing}"


def test_0093_normalizes_legacy_pending_rows_without_inventing_principals(
    pg_conn: psycopg.Connection,
) -> None:
    _apply_through(pg_conn, "0092")

    for name, state, object_key, visibility, owner, expires_at in (
        ("public-pending", "pending", "images/a", "public", None, None),
        (
            "private-pending",
            "pending",
            "images/b",
            "private",
            "project-a",
            "2099-01-01T00:00:00Z",
        ),
        ("registered", "registered", "images/c", "public", None, None),
    ):
        pg_conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, object_key, digest, visibility, "
            "owner, expires_at, state) "
            "VALUES ('local-libvirt', %s, 'x86_64', 'qcow2', '/dev/vda', %s, 'sha256:abc', "
            "%s, %s, %s, %s)",
            (name, object_key, visibility, owner, expires_at, state),
        )

    migration = next(m for m in migrate.discover_migrations() if m.version == "0093")
    pg_conn.execute(migration.sql.encode())
    rows = pg_conn.execute(
        "SELECT name, state, publication_attempt_id, publication_principal "
        "FROM image_catalog ORDER BY name"
    ).fetchall()
    by_name = {row[0]: row[1:] for row in rows}
    assert by_name["public-pending"][0] == "pending"
    assert by_name["public-pending"][1] is not None
    assert by_name["public-pending"][2] is None
    assert by_name["private-pending"][0] == "pending"
    assert by_name["private-pending"][1] is not None
    assert by_name["private-pending"][2] is None
    assert by_name["registered"] == ("registered", None, None)


@pytest.mark.parametrize(
    ("overrides", "update"),
    [
        ({"state": "registered"}, sql.SQL("publication_attempt_id = gen_random_uuid()")),
        (
            {"state": "registered"},
            sql.SQL("publication_principal = 'tenant-a'"),
        ),
        (
            {
                "state": "pending",
                "visibility": "public",
                "publication_attempt_id": uuid4(),
            },
            sql.SQL("publication_principal = 'tenant-a'"),
        ),
    ],
)
def test_0093_rejects_invalid_publication_attempt_shapes(
    pg_conn: psycopg.Connection,
    overrides: dict[str, object],
    update: sql.SQL,
) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, **overrides)

    with pytest.raises(psycopg.errors.CheckViolation), pg_conn.transaction():
        pg_conn.execute(sql.SQL("UPDATE image_catalog SET {}").format(update))


def test_0093_rejects_pending_insert_without_publication_attempt(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)

    with pytest.raises(psycopg.errors.CheckViolation):
        pg_conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, object_key, digest, visibility, "
            "owner, expires_at, state) VALUES "
            "('local-libvirt', 'pending', 'x86_64', 'qcow2', '/dev/vda', 'images/a', "
            "'sha256:abc', 'public', NULL, NULL, 'pending')"
        )


def test_phase_two_reserve_adopt_and_registration_sql_remain_compatible(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    attempt = uuid4()
    _insert_image(pg_conn, state="defined", object_key=None, digest=None)

    pg_conn.execute(
        "UPDATE image_catalog SET state = 'pending', object_key = %s, digest = %s, "
        "pending_since = now(), publication_attempt_id = %s, publication_principal = NULL",
        ("images/attempt", "sha256:new", attempt),
    )
    reserved = pg_conn.execute("SELECT state, publication_attempt_id FROM image_catalog").fetchone()
    assert reserved == ("pending", attempt)

    pg_conn.execute(
        "UPDATE image_catalog SET state = 'registered', publication_attempt_id = NULL, "
        "publication_principal = NULL WHERE publication_attempt_id = %s",
        (attempt,),
    )
    assert pg_conn.execute(
        "SELECT state, publication_attempt_id, publication_principal FROM image_catalog"
    ).fetchone() == ("registered", None, None)


def test_phase_two_reserve_insert_sql_remains_compatible(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    attempt = uuid4()

    pg_conn.execute(
        "INSERT INTO image_catalog "
        "(provider, name, arch, format, root_device, object_key, digest, visibility, owner, "
        "expires_at, state, publication_attempt_id, publication_principal, pending_since) "
        "VALUES ('local-libvirt', 'private', 'x86_64', 'qcow2', '/dev/vda', 'images/private', "
        "'sha256:abc', 'private', 'project-a', '2099-01-01T00:00:00Z', 'pending', %s, "
        "'tenant-a', now())",
        (attempt,),
    )

    assert pg_conn.execute(
        "SELECT state, publication_attempt_id, publication_principal FROM image_catalog"
    ).fetchone() == ("pending", attempt, "tenant-a")


def test_phase_two_recovery_disarm_then_delete_remains_compatible(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    attempt = uuid4()
    _insert_image(
        pg_conn,
        state="pending",
        object_key="images/reclaimed",
        publication_attempt_id=attempt,
    )

    disarmed = pg_conn.execute(
        "UPDATE image_catalog SET publication_attempt_id = NULL, publication_principal = NULL "
        "WHERE id = (SELECT id FROM image_catalog) AND publication_attempt_id = %s",
        (attempt,),
    ).rowcount
    deleted = pg_conn.execute("DELETE FROM image_catalog").rowcount

    assert disarmed == 0
    assert deleted == 0
    assert pg_conn.execute("SELECT count(*) FROM image_catalog").fetchone() == (0,)


def test_phase_two_registered_delete_remains_compatible(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="registered")

    assert pg_conn.execute("DELETE FROM image_catalog").rowcount == 1


def test_phase_three_direct_pending_delete_is_not_suppressed(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    _insert_image(pg_conn, state="pending")

    assert pg_conn.execute("DELETE FROM image_catalog").rowcount == 1
