"""No free-form profile reference string leaks into a recovery envelope (#568, ADR-0180)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from kdive.domain.capacity.state import RunState, SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle import Run, System
from kdive.mcp.tools.lifecycle.runs.common import envelope_for_run
from kdive.mcp.tools.lifecycle.systems.view import system_envelope

_PLANTED = "PLANTED-DO-NOT-LEAK"  # a benign marker; the test asserts it never leaks
_DT = datetime(2026, 6, 18, tzinfo=UTC)


def test_system_envelope_excludes_ssh_credential_ref() -> None:
    system = System(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="u",
        project="proj",
        allocation_id=uuid4(),
        state=SystemState.READY,
        provisioning_profile={
            "schema_version": 1,
            "arch": "x86_64",
            "boot_method": "direct-kernel",
            "vcpu": 2,
            "memory_mb": 4096,
            "disk_gb": 20,
            "provider": {"local-libvirt": {"ssh_credential_ref": f"file:///run/{_PLANTED}"}},
        },
    )
    resp = system_envelope(system, resource_kind="local-libvirt", resource_id=str(uuid4()))
    assert _PLANTED not in resp.model_dump_json()


def test_run_envelope_excludes_git_remote_token() -> None:
    run = Run(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        principal="u",
        project="proj",
        investigation_id=uuid4(),
        system_id=None,
        target_kind=ResourceKind.LOCAL_LIBVIRT,
        state=RunState.SUCCEEDED,
        build_profile={
            "source": "server",
            "build_host": "build-1",
            # Secret embedded as a path segment (not basic-auth userinfo, which the
            # detect-secrets hook would flag); the test asserts it does not leak.
            "kernel_source_ref": {"git": {"remote": f"https://h/{_PLANTED}/r.git", "ref": "main"}},
        },
    )
    resp = envelope_for_run(run)
    assert _PLANTED not in resp.model_dump_json()
