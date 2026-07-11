"""_docmeta: annotation constructors + the reviewed destructive set."""

from __future__ import annotations

import pytest

from kdive.mcp.tools import _docmeta


def test_read_only_sets_only_read_hint() -> None:
    a = _docmeta.read_only()
    assert a.readOnlyHint is True
    assert a.destructiveHint is not True


def test_destructive_sets_destructive_not_readonly() -> None:
    a = _docmeta.destructive()
    assert a.destructiveHint is True
    # An explicit False (not an absent/None hint) so clients see "definitely not
    # read-only" rather than "unspecified".
    assert a.readOnlyHint is False


def test_mutating_is_not_readonly_not_destructive() -> None:
    a = _docmeta.mutating()
    # Both hints are explicitly False — a mutating tool is neither read-only nor
    # destructive, stated positively rather than left unset.
    assert a.readOnlyHint is False
    assert a.destructiveHint is False


def test_destructive_tools_set_is_the_reviewed_set() -> None:
    assert (
        frozenset(
            {
                "control.force_crash",
                "systems.teardown",
                "ops.force_teardown",
                "ops.force_release",
                "ops.reconcile_systems",
                "resources.drain",
                "resources.deregister",
                "images.delete",
                "images.prune_expired",
                "images.extend",
                "tools.invoke",
            }
        )
        == _docmeta.DESTRUCTIVE_TOOLS
    )


def test_contributor_lifecycle_tools_are_mutating_not_destructive() -> None:
    assert "control.power" not in _docmeta.DESTRUCTIVE_TOOLS
    assert "systems.reprovision" not in _docmeta.DESTRUCTIVE_TOOLS


def test_maturity_values_include_partial_disclosure_state() -> None:
    assert {"implemented", "partial", "planned"} == _docmeta.TOOL_MATURITY_VALUES
    assert _docmeta.maturity_meta("partial") == {"maturity": "partial"}


def test_normalize_maturity_rejects_typos() -> None:
    with pytest.raises(ValueError, match="invalid tool maturity"):
        _docmeta.normalize_maturity("implemeted")
