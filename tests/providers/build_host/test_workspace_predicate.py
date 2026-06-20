"""warm_tree_source_error predicate: the single source of the unset/invalid rule."""

from __future__ import annotations

import pytest

from kdive.db.build_host_policy import (
    KERNEL_SRC_INVALID_DETAIL,
    KERNEL_SRC_UNSET_DETAIL,
    warm_tree_source_error,
)


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_unset_or_whitespace_returns_unset_detail(value: str) -> None:
    assert warm_tree_source_error(value) == KERNEL_SRC_UNSET_DETAIL


@pytest.mark.parametrize("value", ["relative/path", "/", "/does/not/exist/kdive-xyz"])
def test_present_but_unusable_returns_invalid_detail(value: str) -> None:
    assert warm_tree_source_error(value) == KERNEL_SRC_INVALID_DETAIL


def test_usable_absolute_dir_returns_none(tmp_path: object) -> None:
    assert warm_tree_source_error(str(tmp_path)) is None


@pytest.mark.parametrize("detail", [KERNEL_SRC_UNSET_DETAIL, KERNEL_SRC_INVALID_DETAIL])
def test_detail_cites_the_staging_doc_as_an_mcp_resource_uri(detail: str) -> None:
    # The guidance reaches an MCP client, which cannot open a bare filesystem path; it must cite
    # the doc as the fetchable resource URI (ADR-0151), not "docs/operating/...".
    assert "resource://kdive/docs/operating/build-source-staging.md" in detail
    assert "see docs/operating/" not in detail
