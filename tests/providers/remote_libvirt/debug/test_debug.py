"""Unit tests for the remote-libvirt gdb-MI attach seam (issue #205, ADR-0083).

The real attach + debuginfo resolution are ``live_vm``-gated; the unit test asserts the
off-gate ``MISSING_DEPENDENCY`` contract and the host-policy inversion (ACL-remote accepts a
non-loopback host the local loopback policy would reject).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.debug.gdbmi import remote_attach_seam
from kdive.providers.shared.debug_common.hostpolicy import allow_acl_remote, require_loopback


def test_remote_attach_seam_off_gate_reports_missing_dependency():
    with pytest.raises(CategorizedError) as exc:
        remote_attach_seam(
            host="10.0.0.5", port=47002, run_id="r1", transcript_path=Path("/tmp/t.jsonl")
        )
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY
    # The failure originates in the debuginfo resolver, which names the live_vm gate and
    # carries the offending run_id so an operator can trace which Run was refused.
    assert str(exc.value) == (
        "resolving a remote Run's debuginfo object runs only under the live_vm gate"
    )
    assert exc.value.details == {"run_id": "r1"}


def test_remote_policy_accepts_non_loopback_but_loopback_policy_would_reject():
    allow_acl_remote("10.0.0.5")  # remote policy: OK
    with pytest.raises(CategorizedError):
        require_loopback("10.0.0.5")  # the local policy would reject — proves the inversion
