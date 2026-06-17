"""run_build_on_host admits a LOCAL warm-tree build only when KDIVE_KERNEL_SRC is usable."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from kdive.build_artifacts.results import BuildOutput
from kdive.db.build_hosts import BuildHost, BuildHostKind, BuildHostState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.shared.build_host.dispatch import run_build_on_host
from kdive.providers.shared.build_host.workspace import KERNEL_SRC_UNSET_DETAIL
from kdive.security.secrets.secret_registry import SecretRegistry

_RUN_ID = UUID("00000000-0000-0000-0000-0000000000d1")

_WARM_PROFILE = {
    "schema_version": 1,
    "kernel_source_ref": "linux-6.9",
    "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
}


def _local_host() -> BuildHost:
    return BuildHost(
        id=UUID("00000000-0000-0000-0000-0000000000d2"),
        name="worker-local",
        kind=BuildHostKind.LOCAL,
        address=None,
        ssh_credential_ref=None,
        base_image_volume=None,
        workspace_root="/build",
        max_concurrent=1,
        enabled=True,
        state=BuildHostState.READY,
    )


class _RecordingBuilder:
    def __init__(self) -> None:
        self.called = False

    def build(self, run_id: UUID, profile: ServerBuildProfile) -> BuildOutput:
        self.called = True
        return BuildOutput(kernel_ref="k", debuginfo_ref="d", build_id="b")


def _parsed() -> ServerBuildProfile:
    parsed = BuildProfile.parse(_WARM_PROFILE)
    assert isinstance(parsed, ServerBuildProfile)
    return parsed


def test_empty_kernel_src_rejected_before_builder_runs() -> None:
    builder = _RecordingBuilder()
    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(
            run_build_on_host(
                builder,
                _local_host(),
                _RUN_ID,
                _parsed(),
                secret_registry=SecretRegistry(),
                kernel_src="",
            )
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(excinfo.value) == KERNEL_SRC_UNSET_DETAIL
    assert builder.called is False


def test_usable_kernel_src_runs_builder(tmp_path: object) -> None:
    builder = _RecordingBuilder()
    out = asyncio.run(
        run_build_on_host(
            builder,
            _local_host(),
            _RUN_ID,
            _parsed(),
            secret_registry=SecretRegistry(),
            kernel_src=str(tmp_path),
        )
    )
    assert builder.called is True
    assert out.kernel_ref == "k"
