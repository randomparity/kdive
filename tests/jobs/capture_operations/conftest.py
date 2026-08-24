"""Shared fixtures for the capture-operations tests."""

from __future__ import annotations

import secrets

import pytest


@pytest.fixture
def launch_token() -> str:
    """A launch token unique to this test.

    ``scan_launch_token`` enumerates all of ``/proc`` and matches every process that
    shares this euid and interpreter and carries the token in its argv, and the
    launcher's cleanup path SIGKILLs each match. Production is safe because
    ``0112_capture_operation_supervision.sql`` declares ``launch_token`` UNIQUE, so no
    two operations ever share one.

    Tests must honour that same invariant. Under xdist every worker runs as this uid
    with this interpreter, so a token literal shared between tests lets one worker's
    cleanup kill another worker's live capture child, and the victim fails with
    ``-9`` (#2063). Take the token from this fixture rather than writing a literal;
    ``tests/guards/test_capture_operations_launch_tokens_are_unique.py`` enforces it.
    """
    return secrets.token_hex(32)
