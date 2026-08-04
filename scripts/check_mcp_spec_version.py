"""Drift guard for the MCP protocol version KDIVE negotiates (ADR-0537).

Two facts drift on two different clocks, so this script has two modes and only one of them
gates a PR:

* **default (offline)** — the pinned ``mcp`` library must still advertise the protocol range
  :mod:`kdive.mcp` declares. A dependency bump that moves either end fails here, which is the
  point: the range is an agent-facing compatibility surface that previously moved with no
  review. Runs from ``just mcp-spec-check`` in ``ci``; reads only the installed package, so it
  needs no network.
* **``--upstream`` (network)** — the specification repository must not publish a revision
  newer than the declared ceiling. Runs only from ``.github/workflows/mcp-spec-drift.yml`` on
  a weekly cron, and gates nothing: a PR must not depend on github.com being reachable.

Exit codes are the interface the workflow branches on, and the split matters. **1 means drift
was determined**; **2 means drift could not be determined** — a failed fetch, an unreadable
listing, or any unexpected error. The sibling ``rusty-imap-mcp`` conflates the two, so a
transient network fault there files an issue reporting drift that does not exist.

Both library values are read as module attributes at call time, never via ``from ... import``:
``mcp/shared/version.py`` materialises ``SUPPORTED_PROTOCOL_VERSIONS`` with the ceiling already
substituted at import, so a ``from``-bound name would not see a monkeypatch of either.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
import urllib.request
from collections.abc import Iterable
from pathlib import Path

import mcp.shared.version
import mcp.types

from kdive.mcp import MCP_PROTOCOL_VERSION, MCP_SUPPORTED_PROTOCOL_VERSIONS

_SCHEMA_LISTING_URL = (
    "https://api.github.com/repos/modelcontextprotocol/modelcontextprotocol/contents/schema"
)
_FETCH_TIMEOUT_S = 30

# A released revision directory: exactly YYYY-MM-DD. Excludes `draft` and any future
# `YYYY-MM-DD-<suffix>` pre-release, neither of which KDIVE could adopt.
#
# `\Z`, not `$`: `$` also matches before a single trailing newline, so `"2026-07-28\n"` would
# satisfy it. The value flows into $GITHUB_OUTPUT and from there into the workflow's issue
# title and its `--jq` program, where an embedded newline breaks the JSON literal. The
# workflow's no-injection argument rests on this pattern being exact, so it is.
_RECOGNIZED = re.compile(r"\d{4}-\d{2}-\d{2}\Z")

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_UNDETERMINED = 2


def newer_revisions(entries: Iterable[str], declared: str) -> list[str]:
    """Published revisions strictly newer than ``declared``, oldest first.

    ISO-8601 dates sort lexicographically, so the comparison is a plain string compare — no
    date parsing, and no timezone can enter.

    Args:
        entries: Directory names from the upstream ``schema/`` listing.
        declared: The revision KDIVE declares as its ceiling.

    Returns:
        The recognized entries above ``declared``, ascending.
    """
    recognized = sorted(entry for entry in entries if _RECOGNIZED.fullmatch(entry))
    return [entry for entry in recognized if entry > declared]


_REMEDIATION = (
    "edit src/kdive/mcp/__init__.py to match, then run 'just doc-constants' and "
    "'just resources-docs' and commit the regenerated files"
)


def check_offline() -> int:
    """Assert the pinned library still advertises the range :mod:`kdive.mcp` declares.

    Reads both values as module attributes at call time — see this module's docstring for
    why ``from ... import`` would be wrong.

    Returns:
        :data:`EXIT_OK` when both agree, :data:`EXIT_DRIFT` otherwise.
    """
    library_ceiling = mcp.types.LATEST_PROTOCOL_VERSION
    library_supported = tuple(mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS)

    problems: list[str] = []
    if library_ceiling != MCP_PROTOCOL_VERSION:
        problems.append(
            f"newest protocol revision: mcp advertises '{library_ceiling}', "
            f"kdive declares '{MCP_PROTOCOL_VERSION}'"
        )
    if library_supported != MCP_SUPPORTED_PROTOCOL_VERSIONS:
        dropped = sorted(set(MCP_SUPPORTED_PROTOCOL_VERSIONS) - set(library_supported))
        added = sorted(set(library_supported) - set(MCP_SUPPORTED_PROTOCOL_VERSIONS))
        detail = ", ".join(
            part
            for part in (
                f"no longer supported: {dropped}" if dropped else "",
                f"newly supported: {added}" if added else "",
                "order changed" if not dropped and not added else "",
            )
            if part
        )
        problems.append(
            f"supported protocol revisions: mcp advertises {list(library_supported)}, "
            f"kdive declares {list(MCP_SUPPORTED_PROTOCOL_VERSIONS)} ({detail})"
        )

    if not problems:
        print(
            f"MCP protocol range in sync: {MCP_PROTOCOL_VERSION} "
            f"(supported: {', '.join(MCP_SUPPORTED_PROTOCOL_VERSIONS)})"
        )
        return EXIT_OK

    for problem in problems:
        print(problem, file=sys.stderr)
    print(
        "The protocol range KDIVE advertises is set by the pinned mcp dependency and has "
        f"moved (ADR-0537). If the change is intended, {_REMEDIATION}.",
        file=sys.stderr,
    )
    return EXIT_DRIFT


def fetch_schema_entries() -> list[str]:
    """Return the directory names under the specification repository's ``schema/``.

    Sends ``Authorization`` when ``GITHUB_TOKEN`` is set: unauthenticated GitHub API calls are
    limited to 60/hour per IP, and Actions runners share addresses.

    Raises:
        urllib.error.URLError: The listing could not be fetched.
        KeyError, TypeError, ValueError: The payload was not the shape this reader expects.
    """
    request = urllib.request.Request(_SCHEMA_LISTING_URL)  # noqa: S310 - constant https URL
    request.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")

    with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:  # noqa: S310
        payload = json.load(response)

    return [entry["name"] for entry in payload if entry["type"] == "dir"]


def _emit_github_output(newest: str, declared: str) -> None:
    """Hand the workflow the two values it builds the issue title from.

    The workflow must not scrape them out of the human report: the title is the dedup key, so
    a later rewording of that prose would silently change it.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"newest={newest}\ndeclared={declared}\n")


def check_upstream() -> int:
    """Report specification revisions published above the declared ceiling.

    Returns:
        :data:`EXIT_OK` when nothing is newer, :data:`EXIT_DRIFT` when something is, and
        :data:`EXIT_UNDETERMINED` when the listing is not one this reader recognizes.
    """
    entries = fetch_schema_entries()
    recognized = [entry for entry in entries if _RECOGNIZED.fullmatch(entry)]

    # The sanity floor. Without it, an upstream restructuring that still returns HTTP 200
    # leaves every entry unmatched, the comparison finds nothing newer, and this job passes
    # green forever — a guard reporting success over nothing (ADR-0518).
    if MCP_PROTOCOL_VERSION not in recognized:
        print(
            f"upstream schema/ listing does not contain the declared revision "
            f"'{MCP_PROTOCOL_VERSION}'; recognized entries were {recognized or '[]'} out of "
            f"{list(entries)}. This is not the directory layout this check knows how to "
            f"read — it cannot tell whether a newer revision exists.",
            file=sys.stderr,
        )
        return EXIT_UNDETERMINED

    newer = newer_revisions(recognized, MCP_PROTOCOL_VERSION)
    if not newer:
        print(f"no upstream MCP revision newer than {MCP_PROTOCOL_VERSION}")
        return EXIT_OK

    newest = newer[-1]
    print(
        f"MCP specification revision {newest} is published upstream; KDIVE declares "
        f"{MCP_PROTOCOL_VERSION}.\n"
        f"Newer revisions: {', '.join(newer)}.\n"
        f"Adopting one requires a pinned mcp release that supports it; when that lands, "
        f"{_REMEDIATION}."
    )
    _emit_github_output(newest, MCP_PROTOCOL_VERSION)
    return EXIT_DRIFT


def main(argv: list[str] | None = None) -> int:
    """Run the requested mode, mapping anything unexpected to :data:`EXIT_UNDETERMINED`.

    The guard is what keeps :data:`EXIT_DRIFT` meaning only "drift was determined". An
    uncaught exception would otherwise exit 1 — Python's default — and the drift workflow
    would file an issue announcing drift it never measured.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        if args and args[0] == "--upstream":
            return check_upstream()
        if args:
            print(f"usage: {Path(__file__).name} [--upstream]", file=sys.stderr)
            return EXIT_UNDETERMINED
        return check_offline()
    except SystemExit, KeyboardInterrupt:
        raise
    except BaseException:  # noqa: BLE001 - deliberate: anything unclassified is "undetermined"
        traceback.print_exc()
        print(
            "could not determine MCP protocol drift (see traceback above)",
            file=sys.stderr,
        )
        return EXIT_UNDETERMINED


if __name__ == "__main__":  # pragma: no cover - exercised via `just mcp-spec-check`
    raise SystemExit(main())
