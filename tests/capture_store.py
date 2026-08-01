"""The store side of a fake vmcore capture (ADR-0497).

``finalize_capture`` heads every object it is about to reference and refuses to commit a row unless
the store still holds it at the etag the capture observed. A fake retriever that returns a
``CaptureOutput`` naming objects no store holds is therefore no longer a complete double: the
handler needs a store that agrees with it.

:class:`WrittenObjects` is that agreement, composed into the capture fakes so one object plays both
the provider and the bucket it wrote to — which is what the real pair does, and which means the
double cannot drift from the keys and etags the test itself put under test. Tests that are *about*
the verify (``tests/adversarial/test_vmcore_finalize_object_verify.py``) build their own store
instead, because they need to disagree with the capture on purpose.

One consequence is worth naming, because a shared double is easy to over-read. When two
concurrent handlers drive the *same* fake for the same Run, both record the same etag for the same
key, so both finalizes agree with the store and both commit. In production that is the
byte-identical-cores case. When the bytes differ, the later write to each of the two keys wins, and
one or both finalizes refuse — both when the raw and redacted writes interleave in opposite orders
(ADR-0497 Consequences). The refusal arm is pinned directly by
``test_an_object_replaced_since_the_capture_commits_no_row`` rather than by forcing that
interleaving through a barrier.
"""

from __future__ import annotations

from kdive.artifacts.storage import HeadResult
from kdive.providers.ports.retrieve import CaptureOutput
from tests.clock import STORE_MTIME


class WrittenObjects:
    """The keys and etags a capture double claims to have written, answerable through ``head``."""

    def __init__(self) -> None:
        self._written: dict[str, str] = {}
        self.headed_keys: list[str] = []

    def record(self, output: CaptureOutput) -> CaptureOutput:
        """Remember both of ``output``'s objects as written, and return it unchanged."""
        for stored in (output.raw, output.redacted):
            self._written[stored.key] = stored.etag
        return output

    def head(self, key: str) -> HeadResult | None:
        """Stat a recorded object; ``last_modified`` is fixed — no capture test is about it."""
        self.headed_keys.append(key)
        etag = self._written.get(key)
        if etag is None:
            return None
        return HeadResult(
            size_bytes=1,
            checksum_sha256=None,
            etag=etag,
            last_modified=STORE_MTIME,
            version_id="test-version",
        )
