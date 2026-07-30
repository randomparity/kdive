"""A malformed store reply costs the upload orphan sweep one key, not the whole pass (#1685).

This is the consequence half of the store-boundary contract pinned in
``tests/store/test_objectstore_malformed_response.py``, and it is deliberately driven through the
**real** :class:`~kdive.store.objectstore.ObjectStore` over a fake boto client rather than through
the fake store the rest of the sweep suite uses. The defect lives in the seam between the two
layers: the sweep's per-key handler catches ``CategorizedError`` and nothing else (a bug in that
module *should* abort the pass), so whether a malformed reply is survivable is decided entirely by
what the store raises. A fake store that raised ``CategorizedError`` directly would assume away the
thing under test.

The load-bearing assertion in every test here is that the keys **behind** the malformed one were
still reclaimed. The malformed key is seeded to sort first, so a pass that aborts leaves them
untouched — which is exactly what happens before the store-boundary fix, and what makes these
assertions able to fail.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.artifacts import upload_manifest
from kdive.artifacts.uploads import ManifestEntry
from kdive.domain.capacity.state import RunState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.reconciler.cleanup.upload_orphans import repair_leaked_upload_objects
from kdive.store.objectstore import ObjectStore
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_system

_GRACE = timedelta(hours=1)
_NO_TTL = timedelta(0)
_BUCKET = "the-bucket"


class _FakeS3:
    """A boto client stand-in that can serve one key a reply missing or corrupting one field.

    Only the three operations the sweep reaches are implemented — the paged listing, the per-key
    ``head_object``, and ``delete_object`` — because the point is to exercise ``ObjectStore``'s own
    parsing of those replies, not to emulate S3.

    ``head_object`` for ``bad_key`` returns HTTP 200 with ``bad_field`` removed (or replaced by
    ``bad_value``), which is the shape of a proxy or partial S3 implementation that answers
    successfully with an unusable body. Every other key gets a well-formed reply.
    """

    def __init__(
        self,
        keys: list[str],
        *,
        age: timedelta,
        bad_key: str | None = None,
        bad_field: str | None = None,
        bad_value: object | None = None,
    ) -> None:
        self._mtime = datetime.now(UTC) - age
        self._present = list(keys)
        self._bad_key = bad_key
        self._bad_field = bad_field
        self._bad_value = bad_value
        self.deleted: list[str] = []
        self.listed_prefixes: list[str] = []

    @property
    def present(self) -> set[str]:
        return set(self._present)

    def get_paginator(self, _op: str) -> _FakeS3:
        return self

    def paginate(self, **kwargs: object) -> Iterator[dict[str, Any]]:
        prefix = str(kwargs["Prefix"])
        self.listed_prefixes.append(prefix)
        contents = [
            {"Key": key, "LastModified": self._mtime}
            for key in sorted(self._present)
            if key.startswith(prefix)
        ]
        yield {"Contents": contents}

    def head_object(self, **kwargs: object) -> dict[str, Any]:
        key = str(kwargs["Key"])
        reply: dict[str, Any] = {
            "ContentLength": 1,
            "ETag": f'"etag-of-{key}"',
            "LastModified": self._mtime,
            "Metadata": {},
        }
        if key != self._bad_key or self._bad_field is None:
            return reply
        if self._bad_value is None:
            del reply[self._bad_field]
        else:
            reply[self._bad_field] = self._bad_value
        return reply

    def delete_object(self, **kwargs: object) -> dict[str, Any]:
        key = str(kwargs["Key"])
        self.deleted.append(key)
        if key in self._present:
            self._present.remove(key)
        return {}


async def _seed_rowless_upload_prefix(url: str) -> tuple[UUID, str]:
    """Seed a Run whose upload window has already been reaped — the sweep's candidate state.

    A window minted with a negative TTL is lapsed on arrival, and deleting its manifest row leaves
    the prefix with no ``upload_manifests`` row and no ``artifacts`` row: rowless, which is what the
    sweep reclaims.
    """
    async with await connect(url) as seed:
        system_id = await seed_system(seed)
        run_id = await seed_run(seed, system_id, run_state=RunState.CREATED)
        prefix = f"local/runs/{run_id}/"
        await upload_manifest.replace_manifest(
            seed,
            upload_manifest.UploadManifestReplaceRequest(
                owner_kind="runs",
                owner_id=run_id,
                prefix=prefix,
                entries=[ManifestEntry("kernel", "a", 1)],
                ttl=timedelta(seconds=-1),
            ),
        )
        await upload_manifest.delete_manifest(seed, "runs", run_id)
    return run_id, prefix


async def _sweep_expecting_one_fault(migrated_url: str, client: _FakeS3) -> CategorizedError:
    """Run one sweep pass over ``client`` and return the fault it raised at the end."""
    store = ObjectStore(client, _BUCKET)
    async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
        with pytest.raises(CategorizedError) as excinfo:
            await run_repair(
                pool,
                lambda conn: repair_leaked_upload_objects(conn, store, _GRACE, _NO_TTL),
            )
    return excinfo.value


@pytest.mark.parametrize("field", ["LastModified", "ContentLength", "ETag"])
def test_a_head_reply_missing_a_field_costs_one_key_and_the_pass_finishes(
    migrated_url: str, field: str
) -> None:
    """The whole point of #1685: the malformed key is skipped and counted, the rest are reclaimed.

    Parametrized over all three required fields because the sweep does not care *which* field the
    store dropped — it cares that the store's answer arrived as the error category its per-key
    handler catches. A guard on ``LastModified`` alone would leave two fields able to reproduce the
    identical abort.

    ``aaa-vmcore`` sorts first inside the listing page, so the two keys asserted on are strictly
    *behind* the malformed one. Before the store-boundary fix the ``KeyError`` escapes
    ``_reclaim_page``, escapes ``_sweep_root``, and ends the pass with those two keys still present
    — so the ``deleted`` assertion below is one that can, and did, fail.
    """

    async def _run() -> None:
        _, prefix = await _seed_rowless_upload_prefix(migrated_url)
        malformed = f"{prefix}aaa-vmcore"
        behind = [f"{prefix}bbb-kernel", f"{prefix}ccc-initrd"]
        client = _FakeS3([malformed, *behind], age=_GRACE * 2, bad_key=malformed, bad_field=field)

        fault = await _sweep_expecting_one_fault(migrated_url, client)

        assert fault.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        # The pass reached every key behind the malformed one and reclaimed them, in listing order.
        assert client.deleted == behind
        # And it did not delete on a reply it could not read.
        assert client.present == {malformed}

    asyncio.run(_run())


def test_a_head_reply_with_a_wrong_typed_field_costs_one_key_too(migrated_url: str) -> None:
    """An ill-typed ``LastModified`` is survivable for the same reason a missing one is.

    Without the boundary check this reaches ``_RECLAIMABLE_SQL`` as a ``text`` in a
    ``timestamptz[]`` parameter position. That fault would land inside ``_reclaim_page``'s
    ``psycopg.Error`` handler and end the **root** rather than one key — losing every remaining
    candidate under it — which is a quieter version of the same defect.
    """

    async def _run() -> None:
        _, prefix = await _seed_rowless_upload_prefix(migrated_url)
        malformed = f"{prefix}aaa-vmcore"
        behind = [f"{prefix}bbb-kernel"]
        client = _FakeS3(
            [malformed, *behind],
            age=_GRACE * 2,
            bad_key=malformed,
            bad_field="LastModified",
            bad_value="2026-07-29T00:00:00Z",
        )

        fault = await _sweep_expecting_one_fault(migrated_url, client)

        assert fault.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert client.deleted == behind
        assert client.present == {malformed}

    asyncio.run(_run())


def test_the_recorded_fault_names_the_store_call_the_key_and_the_field(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The skip is logged with enough to act on, which is what the issue asked for.

    ``_reclaim_page`` logs the candidate's key and the exception's message, so an operator seeing
    one key fail every pass forever needs the message itself to say *why* — that the store's
    ``head_object`` reply omitted a field, and which one. "head_object failed" would not
    distinguish this from a per-key deny.
    """

    async def _run() -> None:
        _, prefix = await _seed_rowless_upload_prefix(migrated_url)
        malformed = f"{prefix}aaa-vmcore"
        client = _FakeS3(
            [malformed, f"{prefix}bbb-kernel"],
            age=_GRACE * 2,
            bad_key=malformed,
            bad_field="LastModified",
        )

        with caplog.at_level(logging.WARNING, logger="kdive.reconciler.cleanup.upload_orphans"):
            await _sweep_expecting_one_fault(migrated_url, client)

        # The per-key skip is the WARNING; the pass's end-of-run tally is the ERROR beside it.
        skips = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(skips) == 1
        assert malformed in skips[0]
        assert "head_object" in skips[0]
        assert _BUCKET in skips[0]
        assert "LastModified" in skips[0]

    asyncio.run(_run())


def test_a_fault_that_is_not_a_store_fault_still_aborts_the_whole_pass(migrated_url: str) -> None:
    """The property the store-boundary fix had to preserve, stated as a test.

    ``_reclaim_page`` catches ``CategorizedError`` and its docstring says why nothing wider: "a bug
    in this module should still abort the pass". The tempting fix for #1685 was to add ``KeyError``
    to that ``except``, which would have made every bug in the sweep a silently counted per-key skip
    — the exact opposite of what the module asks for. Converting the reply at the store boundary
    instead leaves this property intact, and this test is what fails if someone widens that
    ``except`` later.

    A ``RuntimeError`` out of ``head`` stands in for any such bug: it must reach the caller
    unchanged, and it must not be counted as a reclaimable-key fault.
    """

    class _BuggyStore(ObjectStore):
        def head(self, key: str) -> Any:
            raise RuntimeError(f"a bug in the sweep, not a store fault, on {key}")

    async def _run() -> None:
        _, prefix = await _seed_rowless_upload_prefix(migrated_url)
        client = _FakeS3([f"{prefix}aaa-vmcore"], age=_GRACE * 2)
        store = _BuggyStore(client, _BUCKET)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with pytest.raises(RuntimeError):
                await run_repair(
                    pool,
                    lambda conn: repair_leaked_upload_objects(conn, store, _GRACE, _NO_TTL),
                )
        # Aborted, not skipped-and-counted: nothing was deleted and the sibling root was never
        # reached, which is what distinguishes an abort from the per-key fault path.
        assert client.deleted == []
        assert client.listed_prefixes == ["local/runs/"]

    asyncio.run(_run())


def test_a_malformed_listing_entry_ends_its_root_without_ending_the_pass(
    migrated_url: str,
) -> None:
    """The listing is the sweep's other store read, and it must fault the same survivable way.

    A ``KeyError`` from a listing entry escapes ``_next_page_or_fault`` — which catches
    ``CategorizedError`` alone — and ends the pass before ``local/investigations/`` is listed at
    all. As a ``CategorizedError`` it ends only the faulted root, which is the behaviour ADR-0455 §5
    already specifies for a listing fault. The assertion is that the *sibling* root was still
    listed, because that is the progress the abort destroys.
    """

    class _MalformedListing(_FakeS3):
        """Serves ``local/runs/`` one entry with no ``Key`` at all; the sibling root is normal."""

        def paginate(self, **kwargs: object) -> Iterator[dict[str, Any]]:
            prefix = str(kwargs["Prefix"])
            if not prefix.startswith("local/runs/"):
                yield from super().paginate(**kwargs)
                return
            self.listed_prefixes.append(prefix)
            yield {"Contents": [{"LastModified": self._mtime}]}

    async def _run() -> None:
        await _seed_rowless_upload_prefix(migrated_url)
        client = _MalformedListing([], age=_GRACE * 2)

        fault = await _sweep_expecting_one_fault(migrated_url, client)

        assert fault.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        # The sibling root was still listed: the malformed entry ended its root, not the pass.
        assert client.listed_prefixes == ["local/runs/", "local/investigations/"]

    asyncio.run(_run())
