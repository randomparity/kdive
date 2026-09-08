"""Tests for the module-staging toolchain worker-vantage check (ADR-0635, #2339)."""

from __future__ import annotations

import asyncio

from kdive.diagnostics.checks import DEPMOD_TOOLCHAIN_ID, CheckStatus, Vantage
from kdive.diagnostics.contributions.depmod_toolchain import default_depmod_toolchain_probe
from kdive.diagnostics.provider_checks import DepmodToolchainCheck
from kdive.domain.errors import ErrorCategory
from kdive.providers.shared.module_staging_tools import (
    DEPMOD,
    DEPMOD_SEARCH_DIRS,
    DEPMOD_SEARCH_PATH,
)

_PROVIDER = "local-libvirt"


def _check(resolved: str | None) -> DepmodToolchainCheck:
    async def _probe() -> str | None:
        return resolved

    return DepmodToolchainCheck(provider=_PROVIDER, probe=_probe)


def test_check_id_and_vantage() -> None:
    check = _check("/usr/sbin/depmod")
    assert check.id == DEPMOD_TOOLCHAIN_ID == "depmod_toolchain"
    assert check.vantage is Vantage.WORKER


def test_resolved_depmod_passes() -> None:
    result = asyncio.run(_check("/usr/sbin/depmod").run())
    assert result.status is CheckStatus.PASS
    assert result.check_id == DEPMOD_TOOLCHAIN_ID
    assert result.provider == _PROVIDER
    assert "/usr/sbin/depmod" in result.detail
    assert result.fix is None
    assert result.failure_category is None


def test_missing_depmod_fails_naming_the_searched_dirs() -> None:
    result = asyncio.run(_check(None).run())
    assert result.status is CheckStatus.FAIL
    assert result.failure_category is ErrorCategory.MISSING_DEPENDENCY
    assert "depmod" in result.detail
    for directory in DEPMOD_SEARCH_DIRS:
        assert directory in result.detail
    assert result.fix is not None
    assert "kmod" in result.fix


def test_default_probe_searches_the_shared_dirs() -> None:
    seen: list[tuple[str, str]] = []

    def _which(cmd: str, *, path: str) -> str | None:
        seen.append((cmd, path))
        return "/sbin/depmod"

    assert asyncio.run(default_depmod_toolchain_probe(which=_which)()) == "/sbin/depmod"
    assert seen == [(DEPMOD, DEPMOD_SEARCH_PATH)]
