"""Remote real-provider capture-operation ordering carrier (ADR-0558)."""

from __future__ import annotations

import pytest

from tests.live_vm import require_live_vm_remote


@pytest.mark.live_vm
@pytest.mark.live_vm_remote
def test_remote_capture_operation_waits_for_fresh_monitor_ordering() -> None:
    """Run only with a Resource-bound remote FIFO/file-operation fixture.

    The current remote live contract provides TLS, base-image, object-store, and reconciler
    fixtures, but no Resource-bound remote file-operation/SSH fixture capable of creating and
    opening the test-owned FIFO on the libvirt host. Collection is intentional: the remote live
    recipe executes this exact carrier and reports it unavailable instead of mistaking an empty
    marker family or a local fake for cross-connection proof.
    """
    require_live_vm_remote()
    pytest.skip("Resource-bound remote FIFO/file-operation fixture is unavailable")
