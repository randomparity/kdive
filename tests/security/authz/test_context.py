"""RequestContext derivation from verified token claims."""

from __future__ import annotations

import pytest

from kdive.security.authz.context import context_from_claims
from kdive.security.authz.errors import AuthError


def test_context_from_claims_rejects_non_string_projects() -> None:
    with pytest.raises(AuthError, match="projects claim entries must be non-empty strings"):
        context_from_claims({"sub": "alice", "projects": ["proj", 7]})


def test_context_from_claims_rejects_empty_project_names() -> None:
    with pytest.raises(AuthError, match="projects claim entries must be non-empty strings"):
        context_from_claims({"sub": "alice", "projects": ["proj", ""]})
