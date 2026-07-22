"""``reconcile_once`` threads its stores and connection into every store-consuming repair.

The store-consuming repairs — the three image-catalog sweeps (leaked / dangling /
expired-private), the abandoned-upload reaper, and the three artifact-GC sweeps (report /
investigation / expired-build) — short-circuit on an empty DB, so a config-driven pass over
inert stores never dereferences ``config.image_store`` / ``config.upload_store`` (nor the
publish grace, nor the pooled ``conn``). That lets the loop's repair-factory arg-passthrough
(``_leaked_images_repair`` … ``_expired_build_artifacts_gc_repair``) go unexercised: a factory
that forwarded ``None`` in place of a real store/grace/conn would be indistinguishable from the
correct wiring.

This seeds **one live candidate per store-consuming repair** and hands ``reconcile_once`` real
recording stores, then asserts each repair reached its store and did real work
(``report.<count> == 1`` and ``report.failures == ()``). A mis-threaded factory arg makes the
repair dereference ``None`` and raise, which the pass catches and records in ``failures`` with a
zero count — so both assertions fail and the mutant dies. The inventory factory's
``config.image_store`` passthrough is covered separately by the s3-present-file loop tests in
``tests/integration/test_reconcile_inventory.py`` (that pass only dereferences the store for an
object-HEAD-gated s3 image).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.uploads import ManifestEntry
from kdive.domain.capacity.state import RunState
from kdive.providers.infra.reaping import NullReaper
from kdive.reconciler.cleanup.images import ImageMtime
from kdive.reconciler.loop import reconcile_once
from tests.reconcile_helpers import make_reconcile_config
from tests.reconciler.conftest import connect, seed_run, seed_system
from tests.reconciler.test_image_sweeps import _insert_image_row

# Ages chosen to clear each repair's default window: image publish grace 1h; report
# retention 7d; investigation cleanup grace 1d; build-artifact retention 30d.
_LEAKED_KEY = "images/local-libvirt/orphan/x86_64.qcow2"
_DANGLING_KEY = "images/local-libvirt/gone/x86_64.qcow2"
_PRIVATE_OBJECT = "images/local-libvirt__proj/priv/x86_64.qcow2"


class _RecordingImageStore:
    """A structural ``ImageSweepStore``: lists objects (leaked), HEADs them (dangling), deletes.

    ``objects`` maps object key -> age; the absolute mtime is ``now - age`` so the leaked
    grace comparison stays on the DB clock. Deleted keys drop out of both listings and HEADs.
    """

    def __init__(self, objects: dict[str, timedelta]) -> None:
        self._objects = dict(objects)
        self.deleted: list[str] = []

    def list_image_objects(self) -> list[ImageMtime]:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        return [
            ImageMtime(key=key, last_modified=now - age)
            for key, age in self._objects.items()
            if key not in self.deleted
        ]

    def head_present(self, key: str) -> bool:
        return key in self._objects and key not in self.deleted

    def delete(self, key: str) -> None:
        self.deleted.append(key)

    def put_artifact(self, request: object) -> object:  # pragma: no cover - sweeps never upload
        raise NotImplementedError("recording image store does not upload artifacts")


class _RecordingUploadStore:
    """A structural ``UploadStore``: lists a prefix (abandoned reaper) and records deletes (GC)."""

    def __init__(self, prefixed: dict[str, list[str]]) -> None:
        self._prefixed = prefixed
        self.deleted: list[str] = []

    def list_prefix(self, prefix: str) -> list[str]:
        return list(self._prefixed.get(prefix, []))

    def delete(self, key: str) -> None:
        self.deleted.append(key)


async def _seed_artifact(
    conn: psycopg.AsyncConnection,
    *,
    owner_kind: str,
    owner_id: UUID,
    retention_class: str,
    key: str,
    age: timedelta,
) -> None:
    await conn.execute(
        "INSERT INTO artifacts (owner_kind, owner_id, object_key, etag, sensitivity, "
        "retention_class, created_at) VALUES (%s, %s, %s, 'etag', 'redacted', %s, now() - %s)",
        (owner_kind, owner_id, key, retention_class, age),
    )


async def _seed_closed_investigation_build(conn: psycopg.AsyncConnection) -> str:
    """A closed investigation past the cleanup grace with a run-owned build artifact to reclaim."""
    inv_id = uuid4()
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state, cleanup_pending_at) "
        "VALUES (%s, 'p', 'proj', 't', 'closed', now() - %s)",
        (inv_id, timedelta(days=2)),
    )
    run_id = uuid4()
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, state, build_profile, target_kind, "
        "principal, project) VALUES (%s, %s, NULL, 'created', '{}'::jsonb, 'local-libvirt', "
        "'p', 'proj')",
        (run_id, inv_id),
    )
    key = "local/runs/investigation-build"
    await _seed_artifact(
        conn,
        owner_kind="runs",
        owner_id=run_id,
        retention_class="build",
        key=key,
        age=timedelta(hours=1),  # recent: only the closed-investigation GC reclaims it
    )
    return key


async def _seed_abandoned_upload(conn: psycopg.AsyncConnection) -> tuple[str, str]:
    """A CREATED run with a past-deadline upload manifest and one uncommitted object."""
    system_id = await seed_system(conn)
    run_id = await seed_run(conn, system_id, run_state=RunState.CREATED)
    prefix = f"local/runs/{run_id}/"
    request = upload_manifest.UploadManifestReplaceRequest(
        owner_kind="runs",
        owner_id=run_id,
        prefix=prefix,
        entries=[ManifestEntry("kernel", "a", 1)],
        ttl=timedelta(seconds=-1),
    )
    await upload_manifest.replace_manifest(conn, request)
    return prefix, f"{prefix}kernel"


def test_reconcile_once_threads_stores_into_every_store_consuming_repair(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        image_store = _RecordingImageStore(
            {
                _LEAKED_KEY: timedelta(hours=2),  # past 1h grace, no row -> leaked reap
                _PRIVATE_OBJECT: timedelta(hours=2),  # expired-private row's object -> deleted
            }
        )
        report_key = "local/reports/old.csv"
        build_key = "local/runs/expired-build"
        async with await connect(migrated_url) as seed:
            # dangling: a registered row whose object is absent from the store, past deadline.
            await _insert_image_row(
                seed, name="gone", object_key=_DANGLING_KEY, pending_age=timedelta(hours=2)
            )
            # expired-private: a private row already past its expiry, object present in store.
            await _insert_image_row(
                seed,
                name="priv",
                visibility="private",
                owner="proj",
                object_key=_PRIVATE_OBJECT,
                expires_in=timedelta(seconds=-1),
            )
            # report GC: an old report artifact past the 7d retention.
            await _seed_artifact(
                seed,
                owner_kind="reports",
                owner_id=uuid4(),
                retention_class="report",
                key=report_key,
                age=timedelta(days=8),
            )
            # investigation GC: a closed investigation's run build artifact past the grace.
            inv_build_key = await _seed_closed_investigation_build(seed)
            # expired-build GC: a run build artifact past the 30d TTL (open/no investigation).
            await _seed_artifact(
                seed,
                owner_kind="runs",
                owner_id=uuid4(),
                retention_class="build",
                key=build_key,
                age=timedelta(days=40),
            )
            prefix, upload_object = await _seed_abandoned_upload(seed)

        upload_store = _RecordingUploadStore({prefix: [upload_object]})
        config = make_reconcile_config(image_store=image_store, upload_store=upload_store)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=config)

        # Every store-consuming repair reached its store and did exactly its one seeded unit of
        # work; nothing raised (a mis-threaded factory arg would land the repair in failures).
        assert report.failures == ()
        assert report.leaked_images == 1
        assert report.dangling_images == 1
        assert report.expired_private_images == 1
        assert report.abandoned_uploads == 1
        # report_artifacts_gc_count has no scalar report field; it lives only in repair_counts.
        assert report.repair_counts["report_artifacts_gc_count"] == 1
        assert report.investigation_artifacts_gc_count == 1
        assert report.expired_build_artifacts_gc_count == 1

        # The stores were the ones actually driven: each recorded the deletes its repair issued.
        assert _LEAKED_KEY in image_store.deleted  # leaked sweep reaped the orphan object
        assert _PRIVATE_OBJECT in image_store.deleted  # expired-private deleted its object
        assert upload_object in upload_store.deleted  # abandoned reaper deleted the uncommitted obj
        assert report_key in upload_store.deleted
        assert inv_build_key in upload_store.deleted
        assert build_key in upload_store.deleted

    asyncio.run(_run())


def test_reconcile_once_over_inert_stores_reports_no_store_work(migrated_url: str) -> None:
    # The companion baseline: with no seeded candidates and the inert default stores, the
    # store-consuming repairs are clean no-ops (0 counts, no failures) — so the test above,
    # not an incidental side effect, is what proves the wiring.
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper(), config=make_reconcile_config())
        assert report.failures == ()
        assert report.leaked_images == 0
        assert report.dangling_images == 0
        assert report.expired_private_images == 0
        assert report.abandoned_uploads == 0
        assert report.repair_counts["report_artifacts_gc_count"] == 0
        assert report.investigation_artifacts_gc_count == 0
        assert report.expired_build_artifacts_gc_count == 0

    asyncio.run(_run())
