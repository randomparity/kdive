"""Tests for the MCP protocol-version drift guard (ADR-0537).

The offline arm asserts the pinned ``mcp`` library still advertises the protocol range
KDIVE declares; the ``--upstream`` arm reports specification revisions newer than that
declaration. No test here performs network I/O: the comparison is pure and the fetch is
injected.
"""

from __future__ import annotations

import socket
from collections.abc import Sequence
from pathlib import Path
from urllib.error import URLError

import mcp.shared.version
import mcp.types
import pytest
import yaml

from kdive.mcp import MCP_PROTOCOL_VERSION, MCP_SUPPORTED_PROTOCOL_VERSIONS
from scripts import check_mcp_spec_version
from scripts.check_mcp_spec_version import main, newer_revisions

# The upstream `schema/` listing as published today, `draft` included.
_LISTING = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28", "draft")

_ROOT = Path(__file__).resolve().parents[2]
_CI_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"
_DRIFT_WORKFLOW = _ROOT / ".github" / "workflows" / "mcp-spec-drift.yml"


def test_declared_constants_match_the_pinned_library() -> None:
    """The declaration is the gate's fixture: it must start out true."""
    assert MCP_PROTOCOL_VERSION == mcp.types.LATEST_PROTOCOL_VERSION
    assert tuple(mcp.shared.version.SUPPORTED_PROTOCOL_VERSIONS) == MCP_SUPPORTED_PROTOCOL_VERSIONS


def test_newer_revisions_finds_a_later_published_revision() -> None:
    """The real listing against the real pin: 2026-07-28 is newer and nothing else is."""
    assert newer_revisions(_LISTING, "2025-11-25") == ["2026-07-28"]


def test_newer_revisions_ignores_draft_and_suffixed_entries() -> None:
    """`draft` and a pre-release suffix are not revisions KDIVE could adopt.

    Mutation control on the filter: without it the comparison returns `draft` as newest,
    since `d` sorts above any digit.
    """
    assert newer_revisions((*_LISTING, "2026-09-01-rc"), "2026-07-28") == []


def test_newer_revisions_is_empty_when_declared_is_newest() -> None:
    """The quiet path — nothing published above the declared ceiling."""
    assert newer_revisions(_LISTING, "2027-01-01") == []


def test_newer_revisions_is_empty_on_an_equal_revision() -> None:
    """Parity is not drift. This is the boundary a `>=` would break."""
    assert newer_revisions(("2026-07-28",), "2026-07-28") == []


def test_newer_revisions_returns_multiple_revisions_oldest_first() -> None:
    """Two revisions published between crons are both reported, in publication order."""
    listing = (*_LISTING, "2026-11-01")
    assert newer_revisions(listing, "2025-11-25") == ["2026-07-28", "2026-11-01"]


# --- offline mode: the `ci` gate ------------------------------------------------------


def test_offline_mode_passes_when_the_pinned_library_agrees() -> None:
    """Green against the real pin, which is this test's fixture."""
    assert main([]) == 0


def test_offline_mode_fails_when_the_ceiling_moves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bump that raises LATEST_PROTOCOL_VERSION fails until a person edits the constant."""
    monkeypatch.setattr(mcp.types, "LATEST_PROTOCOL_VERSION", "2026-07-28")

    assert main([]) == 1

    err = capsys.readouterr().err
    assert "2026-07-28" in err  # what the library now advertises
    assert MCP_PROTOCOL_VERSION in err  # what KDIVE declares
    assert "src/kdive/mcp/__init__.py" in err  # the remediation


def test_offline_mode_fails_when_a_supported_revision_is_dropped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Dropping an older supported revision is the direction that breaks pinned clients.

    The ceiling is untouched here, so an assertion over LATEST_PROTOCOL_VERSION alone passes
    this green — which is exactly why the declaration carries the whole set.
    """
    monkeypatch.setattr(
        mcp.shared.version,
        "SUPPORTED_PROTOCOL_VERSIONS",
        ["2025-03-26", "2025-06-18", "2025-11-25"],
    )

    assert main([]) == 1
    assert "2024-11-05" in capsys.readouterr().err


def test_offline_mode_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `ci` gate must not depend on reachability (ADR-0537: offline by design).

    Without this the offline property is a fact about the tests rather than about the gate,
    and a future network call would first surface on an air-gapped or rate-limited runner.
    """

    def _no_sockets(*args: object, **kwargs: object) -> object:
        raise AssertionError("offline mode must not open a socket")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)

    assert main([]) == 0


# --- wiring: the gate only bites if CI actually invokes it -----------------------------


def _ci_run_steps() -> list[str]:
    doc = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    return [
        step["run"]
        for job in doc["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]


def test_ci_workflow_runs_the_mcp_spec_check_recipe() -> None:
    """Criterion 1 rests on a hand-listed ci.yml step, not on the `ci` recipe.

    No workflow here invokes `just ci` — ci.yml runs each recipe as its own step. This repo
    has lost that wiring twice: ADR-0518 records schema-guard sitting in the `ci` aggregate
    and the prek hook while CI never invoked it, so it passed every PR (#1723), and ADR-0410
    hit the same thing. A comment is not a guard, and the failure mode is green.
    """
    assert any("just mcp-spec-check" in run for run in _ci_run_steps())


# --- upstream mode: the weekly cron, which gates nothing -------------------------------


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, entries: Sequence[str]) -> None:
    """Serve ``entries`` as the upstream ``schema/`` listing, without touching the network."""

    def _fetch() -> list[str]:
        return list(entries)

    monkeypatch.setattr(check_mcp_spec_version, "fetch_schema_entries", _fetch)


def _patch_fetch_error(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """Make the upstream fetch raise ``error``."""

    def _fetch() -> list[str]:
        raise error

    monkeypatch.setattr(check_mcp_spec_version, "fetch_schema_entries", _fetch)


def test_upstream_mode_passes_when_nothing_is_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parity is the quiet path: the cron files nothing and the job stays green."""
    _patch_fetch(monkeypatch, ("2024-11-05", "2025-03-26", "2025-06-18", MCP_PROTOCOL_VERSION))

    assert main(["--upstream"]) == 0


def test_upstream_mode_reports_drift_and_emits_github_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Drift exits 1 and hands the workflow the two values its issue title is built from."""
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    _patch_fetch(monkeypatch, _LISTING)

    assert main(["--upstream"]) == 1

    emitted = output.read_text(encoding="utf-8")
    assert "newest=2026-07-28" in emitted
    assert f"declared={MCP_PROTOCOL_VERSION}" in emitted


def test_upstream_mode_exits_2_on_a_fetch_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A network fault is "could not determine", never "drift".

    Exit 1 here would file an issue announcing drift that was never measured — the defect in
    the sibling rusty-imap-mcp workflow this design departs from.
    """
    _patch_fetch_error(monkeypatch, URLError("unreachable"))

    assert main(["--upstream"]) == 2
    assert "newer" not in capsys.readouterr().out


def test_upstream_mode_exits_2_on_an_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The top-level guard under test: without it this returns Python's default exit 1."""
    _patch_fetch_error(monkeypatch, KeyError("name"))

    assert main(["--upstream"]) == 2


def test_upstream_mode_exits_2_when_the_listing_has_no_recognized_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sanity floor: an unreadable listing is not the same as no drift.

    A restructured `schema/` that still returns HTTP 200 leaves every entry failing the date
    filter. Without the floor the comparison finds nothing newer and the job passes green
    permanently — ADR-0518's "a guard that reports success over nothing".
    """
    _patch_fetch(monkeypatch, ("v2026-07-28", "versions", "draft"))

    assert main(["--upstream"]) == 2


def test_upstream_mode_exits_2_when_the_declared_revision_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor is keyed on the declared revision, not merely on the listing being non-empty.

    A listing of well-formed dates that omits what KDIVE declares is equally unreadable, and
    a bare "any recognized entry" floor would pass it.
    """
    _patch_fetch(monkeypatch, ("2024-11-05", "2025-03-26"))

    assert main(["--upstream"]) == 2
