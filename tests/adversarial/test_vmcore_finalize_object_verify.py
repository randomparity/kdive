"""Adversarial: finalize refuses to reference an object the orphan sweep destroyed (ADR-0497).

The falsifying case is issue #1574, and it needs no re-mint. The vmcore lane's keys are
deterministic per ``(run, method)`` and mint **no** upload window, so a first ``capture_vmcore``
attempt that wrote the core and died before ``finalize_capture`` leaves
``local/runs/<run>/vmcore-<method>`` rowless *and* manifest-less indefinitely. Past
``orphan_grace + upload_ttl`` it is a live candidate for the ADR-0455 sweep, which re-reads and
re-classifies immediately before deleting — and a retried capture's write landing inside that last
gap is deleted anyway (ADR-0455 §3).

The keys here are the **local-libvirt** shape on purpose: the sweep's roots derive from
``UPLOAD_TENANT = "local"``, and local-libvirt is constructed with ``tenant="local"``, so its
``put_stream`` core and ``put`` redacted sibling are the vmcore objects the sweep can reach.
Remote-libvirt's presigned guest PUT writes under ``remote-libvirt/`` and is outside every swept
root. Neither write is fenced by a lock the sweep contends on — ``precheck_run`` releases the Run
lock before the capture by design (ADR-0244), and the sweep takes none.

ADR-0497 does not stop the delete: the conditional delete that would have is inert on the MinIO
releases this repo pins, so the sweep still destroys the bytes. What these tests pin is that the
loss stops being *silent* — the retry's ``finalize_capture`` heads both objects it is about to
reference, refuses to commit any row when the store no longer holds them at the captured etag, and
so the Run fails loudly instead of reporting success behind a dangling ``artifacts`` row.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts.storage import HeadResult, ObjectListing, StoredArtifact
from kdive.db.repositories import RUNS
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.artifacts import vmcore as vmcore_plane
from kdive.jobs.payloads import Authorizing, CaptureVmcorePayload
from kdive.providers.ports.retrieve import CaptureOutput
from kdive.reconciler.cleanup.upload_orphans import repair_leaked_upload_objects
from tests.mcp._seed import seed_crashed_system, seed_run_on_system
from tests.mcp.systems_support import provider_resolver

_AUTH = Authorizing(principal="alice", agent_session="s", project="proj")
_METHOD = CaptureMethod.HOST_DUMP
_GRACE = timedelta(hours=1)
_NO_TTL = timedelta(0)
_RAW_ETAG = "etag-of-the-captured-core"
_REDACTED_ETAG = "etag-of-the-captured-dmesg"
_ABANDONED_ETAG = "etag-of-the-abandoned-first-attempt"


class _VerifyStore:
    """A store over ``key -> (etag, age)``, satisfying the finalize verify *and* the sweep's port.

    One fake serves both because the crossing test needs both: the real ADR-0455 sweep deletes
    through it, and ``finalize_capture`` then heads through it. ``age`` fixes ``last_modified`` at
    ``now - age`` so a test can put a key past the sweep's grace.
    """

    def __init__(self, objects: dict[str, tuple[str, timedelta]]) -> None:
        self._objects = dict(objects)
        self.headed_keys: list[str] = []
        self.deleted: list[str] = []
        self.deleted_etags: list[str] = []

    @property
    def present(self) -> set[str]:
        return set(self._objects)

    def put(self, key: str, etag: str, age: timedelta = timedelta(0)) -> None:
        self._objects[key] = (etag, age)

    def head(self, key: str) -> HeadResult | None:
        self.headed_keys.append(key)
        entry = self._objects.get(key)
        if entry is None:
            return None
        etag, age = entry
        return HeadResult(
            size_bytes=1,
            checksum_sha256=None,
            etag=etag,
            last_modified=datetime.now(UTC) - age,
        )

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(key for key in self._objects if key.startswith(prefix))

    def iter_prefix_pages_with_mtime(self, prefix: str) -> Iterator[list[ObjectListing]]:
        # One page: this fake's subject is the sweep's re-read→delete gap, not its paging.
        yield [
            ObjectListing(key=key, last_modified=datetime.now(UTC) - age)
            for key, (_etag, age) in sorted(self._objects.items())
            if key.startswith(prefix)
        ]

    def delete(self, key: str) -> None:
        entry = self._objects.pop(key, None)
        if entry is not None:
            self.deleted_etags.append(entry[0])
        self.deleted.append(key)


class _SweepInTheGapStore(_VerifyStore):
    """Runs the real orphan sweep's same-key re-read→delete gap: re-PUT, then delete anyway.

    ``before_delete`` fires from inside ``delete``, on the ``to_thread`` worker, once — which is
    exactly the window between the sweep's per-key re-check and its ``delete_object``. Every
    pre-existing test of this hook fires it before a *different* key's delete, so the same-key gap
    — the one #1574 is about — was never exercised.
    """

    def __init__(self, objects: dict[str, tuple[str, timedelta]], *, put_in_gap: str) -> None:
        super().__init__(objects)
        self._put_in_gap = put_in_gap
        self._fired = False

    def delete(self, key: str) -> None:
        if not self._fired:
            self._fired = True
            # The retried capture's put_stream completes here, at the very key about to be
            # deleted. Its artifacts row is still minutes away, so nothing protects these bytes.
            self.put(self._put_in_gap, _RAW_ETAG, timedelta(0))
        super().delete(key)


class _FakeRetriever:
    """Returns the capture the objects in the store were supposed to be."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self.calls = 0

    def capture(self, system_id: UUID, run_id: UUID, method: CaptureMethod) -> CaptureOutput:
        self.calls += 1
        return _capture_output(self._run_id, method)


def _raw_key(run_id: str, method: CaptureMethod = _METHOD) -> str:
    return f"local/runs/{run_id}/vmcore-{method.value}"


def _redacted_key(run_id: str, method: CaptureMethod = _METHOD) -> str:
    return f"{_raw_key(run_id, method)}-redacted"


def _capture_output(run_id: str, method: CaptureMethod = _METHOD) -> CaptureOutput:
    return CaptureOutput(
        raw=StoredArtifact(_raw_key(run_id, method), _RAW_ETAG, Sensitivity.SENSITIVE, "vmcore"),
        redacted=StoredArtifact(
            _redacted_key(run_id, method), _REDACTED_ETAG, Sensitivity.REDACTED, "vmcore"
        ),
        vmcore_build_id="deadbeef",
        raw_size_bytes=512,
    )


def _stored_capture(
    run_id: str, *, age: timedelta = timedelta(0)
) -> dict[str, tuple[str, timedelta]]:
    return {_raw_key(run_id): (_RAW_ETAG, age), _redacted_key(run_id): (_REDACTED_ETAG, age)}


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=2, max_size=6, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _artifact_count(pool: AsyncConnectionPool, run_id: str) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT count(*) AS n FROM artifacts WHERE owner_kind = 'runs' AND owner_id = %s",
            (run_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


async def _audit_count(pool: AsyncConnectionPool, run_id: str) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT count(*) AS n FROM audit_log WHERE object_kind = 'runs' AND object_id = %s "
            "AND transition = 'capture_vmcore'",
            (run_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


async def _seeded_capture_job(pool: AsyncConnectionPool) -> tuple[str, Job]:
    sys_id = await seed_crashed_system(pool)
    run_id = await seed_run_on_system(pool, sys_id, debuginfo_ref=None, build_id=None)
    async with pool.connection() as conn:
        job = await queue.enqueue(
            conn,
            JobKind.CAPTURE_VMCORE,
            CaptureVmcorePayload(run_id=run_id, method=_METHOD),
            _AUTH,
            f"{run_id}:capture_vmcore:{_METHOD.value}",
        )
    return run_id, job


def test_the_sweep_destroys_a_put_landing_in_its_own_key_s_gap(migrated_url: str) -> None:
    """ADR-0455 §3's residual, asserted rather than assumed, for the **same** key.

    This is the loss ADR-0497 mitigates but does not close, so it is pinned as a *fact about the
    sweep* rather than as a fix: the re-PUT lands between the per-key re-check and the delete, and
    the delete runs unconditionally, so the fresh bytes are destroyed. It is unconditional because
    the etag fence that would have stopped it — S3 ``If-Match`` on ``DeleteObject`` — is accepted
    and ignored by both MinIO releases this repo pins (ADR-0497 §1), so shipping it would have made
    this assertion pass while the object still went. If this test ever starts failing because the
    object survived, the conditional delete has become viable and ADR-0497 §1 should be revisited.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, _job = await _seeded_capture_job(pool)
            raw = _raw_key(run_id)
            # The dead first attempt's leftovers: rowless, manifest-less, past the grace.
            store = _SweepInTheGapStore({raw: (_ABANDONED_ETAG, _GRACE * 2)}, put_in_gap=raw)
            async with pool.connection() as conn:
                assert await repair_leaked_upload_objects(conn, store, _GRACE, _NO_TTL) == 1
            assert store.deleted == [raw]
            # The identity is the point, not the absence: an aged rowless orphan being deleted is
            # what the sweep is *for*. What this pins is that the bytes destroyed are the ones the
            # gap PUT wrote, which the sweep never re-read and never decided on.
            assert store.deleted_etags == [_RAW_ETAG]
            assert raw not in store.present

    asyncio.run(_run())


def test_a_capture_whose_object_the_sweep_deleted_commits_no_row(migrated_url: str) -> None:
    """#1574 end to end: the sweep destroys the re-PUT, and the finalize must not paper over it.

    The sweep runs for real, with the concurrent PUT fired inside its own key's re-read→delete gap,
    and then the retried capture finalizes. Before ADR-0497 this committed two ``artifacts`` rows
    and an audit row against bytes that no longer existed, and the job returned a result reference —
    a dangling reference on a Run reporting success, with nothing raised. The sequencing here is a
    simplification of the true interleaving (the real PUT is inside the gap and the finalize follows
    it); the state handed to the finalize is identical either way.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, job = await _seeded_capture_job(pool)
            raw = _raw_key(run_id)
            store = _SweepInTheGapStore(
                {
                    raw: (_ABANDONED_ETAG, _GRACE * 2),
                    _redacted_key(run_id): (_REDACTED_ETAG, _GRACE * 2),
                },
                put_in_gap=raw,
            )
            async with pool.connection() as conn:
                await repair_leaked_upload_objects(conn, store, _GRACE, _NO_TTL)
            # The retried capture's bytes went, not the abandoned attempt's the sweep re-read.
            assert store.deleted_etags[0] == _RAW_ETAG
            assert raw not in store.present

            retriever = _FakeRetriever(run_id)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await vmcore_plane.capture_handler(
                        conn,
                        job,
                        resolver=provider_resolver(retriever=retriever),
                        artifact_store=store,
                    )
            assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            assert caught.value.details["object_key"] == raw
            assert await _artifact_count(pool, run_id) == 0
            assert await _audit_count(pool, run_id) == 0

    asyncio.run(_run())


def test_an_object_replaced_since_the_capture_commits_no_row(migrated_url: str) -> None:
    """Presence is not identity: a delete-and-re-PUT is caught by the etag, not by a HEAD 404.

    A presence-only check would commit here, writing an ``artifacts.etag`` that never matched any
    bytes the capture saw. This is the arm that makes the etag comparison load-bearing rather than
    decorative, and it costs the same round trip.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, job = await _seeded_capture_job(pool)
            store = _VerifyStore(_stored_capture(run_id))
            store.put(_raw_key(run_id), "etag-of-some-other-bytes")

            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await vmcore_plane.capture_handler(
                        conn,
                        job,
                        resolver=provider_resolver(retriever=_FakeRetriever(run_id)),
                        artifact_store=store,
                    )
            assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
            assert caught.value.details["captured_etag"] == _RAW_ETAG
            assert caught.value.details["stored_etag"] == "etag-of-some-other-bytes"
            assert await _artifact_count(pool, run_id) == 0

    asyncio.run(_run())


def test_the_redacted_sibling_is_verified_too(migrated_url: str) -> None:
    """Both objects the capture wrote sit under ``local/runs/<run>/`` and are both candidates.

    ``vmcore-<method>-redacted`` is written server-side by ``persist_redacted``, is rowless for the
    same whole window as the core, and is attributed by the sweep's parser exactly the same way. A
    verify that covered only the raw core would leave the redacted row — the one whose id becomes
    ``refs.result`` (ADR-0466) — free to dangle.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, job = await _seeded_capture_job(pool)
            store = _VerifyStore({_raw_key(run_id): (_RAW_ETAG, timedelta(0))})

            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as caught:
                    await vmcore_plane.capture_handler(
                        conn,
                        job,
                        resolver=provider_resolver(retriever=_FakeRetriever(run_id)),
                        artifact_store=store,
                    )
            assert caught.value.details["object_key"] == _redacted_key(run_id)
            assert await _artifact_count(pool, run_id) == 0

    asyncio.run(_run())


def test_a_capture_whose_objects_are_intact_still_commits(migrated_url: str) -> None:
    """The fence must open: a store that still holds both objects at their etags commits normally.

    Without this the three refusal tests above are all satisfied by a verify that always raises.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, job = await _seeded_capture_job(pool)
            store = _VerifyStore(_stored_capture(run_id))

            async with pool.connection() as conn:
                ref = await vmcore_plane.capture_handler(
                    conn,
                    job,
                    resolver=provider_resolver(retriever=_FakeRetriever(run_id)),
                    artifact_store=store,
                )
            assert ref is not None
            assert await _artifact_count(pool, run_id) == 2
            assert store.headed_keys == [_raw_key(run_id), _redacted_key(run_id)]

    asyncio.run(_run())


def test_the_idempotent_replay_needs_no_verify(migrated_url: str) -> None:
    """A replay commits no new row, so it stats nothing — the object it names is already protected.

    The first capture registers the rows; the second finds them under the Run lock and returns the
    existing redacted id. Heading again there would be a round trip that cannot change the outcome,
    and — worse — would make a replay fail on an object whose row already exists, which is the
    orphan sweep's own fence and not this handler's business.

    This arm is the ``precheck_run`` short-circuit, which returns before ``finalize_capture`` is
    entered at all; :func:`test_finalize_s_own_replay_arm_verifies_nothing_either` covers the
    in-transaction one, which is the arm a verify placed at the top of the function would break.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, job = await _seeded_capture_job(pool)
            store = _VerifyStore(_stored_capture(run_id))
            resolver = provider_resolver(retriever=_FakeRetriever(run_id))
            async with pool.connection() as conn:
                first = await vmcore_plane.capture_handler(
                    conn, job, resolver=resolver, artifact_store=store
                )
            store.headed_keys.clear()
            store.delete(_raw_key(run_id))  # the object is gone; the row still references it
            async with pool.connection() as conn:
                replay = await vmcore_plane.capture_handler(
                    conn, job, resolver=resolver, artifact_store=store
                )
            assert replay == first
            assert store.headed_keys == []
            assert await _artifact_count(pool, run_id) == 2

    asyncio.run(_run())


def test_finalize_s_own_replay_arm_verifies_nothing_either(migrated_url: str) -> None:
    """``finalize_capture``'s in-transaction replay must not stat the object either.

    Driven directly rather than through ``capture_handler``, because the handler's second call
    short-circuits in ``precheck_run`` and never enters this function — so the test above cannot
    reach this arm, and cannot see a verify hoisted to the top of ``finalize_capture`` or added to
    the ``existing is not None`` branch. Both would raise here, on a Run whose rows are already
    committed and whose object is genuinely gone: a state the orphan sweep cannot create (a
    committed ``artifacts`` row is its own fence) and which this handler must replay, not fail.
    """

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            run_id, job = await _seeded_capture_job(pool)
            store = _VerifyStore(_stored_capture(run_id))
            async with pool.connection() as conn:
                expected = await vmcore_plane.capture_handler(
                    conn,
                    job,
                    resolver=provider_resolver(retriever=_FakeRetriever(run_id)),
                    artifact_store=store,
                )
            empty = _VerifyStore({})  # nothing at all is in the bucket any more
            async with pool.connection() as conn:
                run = await RUNS.get(conn, UUID(run_id))
                assert run is not None
                replay = await vmcore_plane.finalize_capture(
                    conn,
                    job,
                    run,
                    _METHOD,
                    _capture_output(run_id),
                    artifact_store=empty,
                )
            assert replay == expected
            assert empty.headed_keys == []
            assert await _artifact_count(pool, run_id) == 2

    asyncio.run(_run())
