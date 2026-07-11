"""Unit tests for the remote-libvirt gdb-MI attach seam (issue #205, ADR-0083).

``remote_attach_seam`` itself is ``live_vm``-gated (real DB read, object-store fetch, gdb spawn);
its debuginfo resolution + private staging orchestration is the provider-neutral shared seam,
unit-tested in ``tests/providers/debug_common/test_debuginfo.py``. Here we pin the remote's
host-policy inversion: ACL-remote accepts a non-loopback host the local loopback policy rejects.
"""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError
from kdive.providers.shared.debug_common.gdbmi.hostpolicy import allow_acl_remote, require_loopback


def test_remote_policy_accepts_non_loopback_but_loopback_policy_would_reject():
    allow_acl_remote("10.0.0.5")  # remote policy: OK
    with pytest.raises(CategorizedError):
        require_loopback("10.0.0.5")  # the local policy would reject — proves the inversion
