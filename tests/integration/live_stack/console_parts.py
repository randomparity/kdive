"""Non-collected support for the post-readiness console-parts live proof."""

from __future__ import annotations

import asyncio
import time

from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.spine import (
    SpinePhaseError,
    full_artifact_text,
    ok,
    scalar,
)


def console_part_ids(listing: ToolResponse) -> list[str]:
    """Return console-part artifact ids in the order supplied by ``artifacts.list``."""
    return [
        item.object_id for item in listing.items if "console-part-" in item.refs.get("object", "")
    ]


async def poll_for_new_console_part(
    client: LiveStackClient,
    system_id: str,
    initial_ids: set[str],
    marker: str,
    *,
    deadline_s: float,
    interval_s: float,
) -> tuple[str, str]:
    """Return a new immutable console part containing ``marker`` and its full plaintext."""
    deadline = time.monotonic() + deadline_s
    inspected_ids: set[str] = set()
    current_ids: list[str] = []
    while True:
        listing = ok(
            await scalar(client, "artifacts.list", system_id=system_id),
            "poll-parts",
        )
        current_ids = console_part_ids(listing)
        candidates = [
            artifact_id
            for artifact_id in current_ids
            if artifact_id not in initial_ids and artifact_id not in inspected_ids
        ]
        for artifact_id in candidates:
            text = await full_artifact_text(client, artifact_id, "read-new-part")
            inspected_ids.add(artifact_id)
            if marker in text:
                return artifact_id, text
        if time.monotonic() >= deadline:
            raise SpinePhaseError(
                "console-parts",
                f"no marker-bearing console-part artifacts within {deadline_s:g}s "
                f"(initial={len(initial_ids)}, current={len(current_ids)}, "
                f"inspected={len(inspected_ids)})",
            )
        await asyncio.sleep(interval_s)
