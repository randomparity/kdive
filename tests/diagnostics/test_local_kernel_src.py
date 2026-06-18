"""`local_kernel_src` check + probe-adapter tests (ADR-0163, #533/#532).

The check is server-vantage: it resolves `KDIVE_KERNEL_SRC` and reports three-state over the
single shared `warm_tree_source_error` predicate (ADR-0161). An unset or invalid warm-tree
source is a contract `fail` with the build-lane fix; a usable absolute tree is a `pass`. There
is no `error` branch — a config read plus a local stat always reaches a verdict. The check
logic is driven by an injected probe; the probe adapter is driven by an injected `source` (and
once through the real config read) so neither test needs a live build host.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import kdive.config as config
from kdive.diagnostics.checks import (
    LOCAL_KERNEL_SRC_FIX,
    LOCAL_KERNEL_SRC_ID,
    CheckStatus,
    LocalKernelSrcCheck,
    Vantage,
    WarmTreeSourceOutcome,
    WarmTreeSourceProbe,
)
from kdive.diagnostics.kernel_src import warm_tree_source_probe


def _probe(outcome: WarmTreeSourceOutcome) -> WarmTreeSourceProbe:
    async def probe() -> WarmTreeSourceOutcome:
        return outcome

    return probe


def _enabled(value: bool):
    async def probe() -> bool:
        return value

    return probe


def _run_probe(probe: WarmTreeSourceProbe) -> WarmTreeSourceOutcome:
    async def _drive() -> WarmTreeSourceOutcome:
        return await probe()

    return asyncio.run(_drive())


# ---- check logic --------------------------------------------------------------------


def test_id_and_vantage() -> None:
    check = LocalKernelSrcCheck(probe=_probe(WarmTreeSourceOutcome.USABLE))
    assert check.id == LOCAL_KERNEL_SRC_ID == "local_kernel_src"
    assert check.vantage is Vantage.SERVER


def test_usable_is_pass() -> None:
    check = LocalKernelSrcCheck(probe=_probe(WarmTreeSourceOutcome.USABLE))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS
    assert result.fix is None
    assert result.failure_category is None
    assert result.provider is None
    assert "existing absolute tree" in result.detail


def test_unset_is_fail_with_the_build_lane_fix() -> None:
    check = LocalKernelSrcCheck(probe=_probe(WarmTreeSourceOutcome.UNSET))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    assert result.fix == LOCAL_KERNEL_SRC_FIX
    assert result.failure_category == "configuration_error"
    assert result.provider is None
    assert "KDIVE_KERNEL_SRC" in result.detail
    assert "unset" in result.detail


def test_invalid_is_fail_with_the_build_lane_fix() -> None:
    check = LocalKernelSrcCheck(probe=_probe(WarmTreeSourceOutcome.INVALID))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    assert result.fix == LOCAL_KERNEL_SRC_FIX
    assert result.failure_category == "configuration_error"
    assert result.provider is None
    assert "absolute path" in result.detail


def test_both_fail_cases_carry_the_same_fix() -> None:
    unset = asyncio.run(LocalKernelSrcCheck(probe=_probe(WarmTreeSourceOutcome.UNSET)).run())
    invalid = asyncio.run(LocalKernelSrcCheck(probe=_probe(WarmTreeSourceOutcome.INVALID)).run())
    assert unset.fix == invalid.fix == LOCAL_KERNEL_SRC_FIX


def test_fix_names_both_build_lanes() -> None:
    # The remediation must name the two ways forward so an operator self-corrects from the
    # verdict alone: stage a warm tree + set KDIVE_KERNEL_SRC, or register a git build host.
    assert "KDIVE_KERNEL_SRC" in LOCAL_KERNEL_SRC_FIX
    assert "build_hosts.register" in LOCAL_KERNEL_SRC_FIX


# ---- enabled-gate (ADR-0167) --------------------------------------------------------


def test_disabled_local_host_is_na_pass_even_when_source_unset() -> None:
    # When the seeded worker-local host is disabled, the local warm-tree lane has no contract to
    # violate, so an unset KDIVE_KERNEL_SRC is a pass (n/a), not a fail — clears the ADR-0163
    # exit-code regression for git/SSH/ephemeral-only deployments.
    check = LocalKernelSrcCheck(
        probe=_probe(WarmTreeSourceOutcome.UNSET), enabled_probe=_enabled(False)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS
    assert result.fix is None
    assert result.failure_category is None
    assert "disabled" in result.detail.lower()


def test_enabled_local_host_unset_still_fails() -> None:
    check = LocalKernelSrcCheck(
        probe=_probe(WarmTreeSourceOutcome.UNSET), enabled_probe=_enabled(True)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL


def test_default_enabled_probe_runs_the_warm_tree_verdict() -> None:
    # No enabled_probe supplied → default always-enabled → existing behavior unchanged.
    check = LocalKernelSrcCheck(probe=_probe(WarmTreeSourceOutcome.UNSET))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL


# ---- probe adapter (source injected) ------------------------------------------------


def test_probe_unset_for_empty_and_whitespace() -> None:
    for value in ("", "   ", "\t\n"):
        probe = warm_tree_source_probe(source=lambda v=value: v)
        assert _run_probe(probe) is WarmTreeSourceOutcome.UNSET


def test_probe_usable_for_existing_absolute_dir(tmp_path: Path) -> None:
    probe = warm_tree_source_probe(source=lambda: str(tmp_path))
    assert _run_probe(probe) is WarmTreeSourceOutcome.USABLE


def test_probe_invalid_for_relative_path() -> None:
    probe = warm_tree_source_probe(source=lambda: "linux")
    assert _run_probe(probe) is WarmTreeSourceOutcome.INVALID


def test_probe_invalid_for_nonexistent_absolute_path() -> None:
    probe = warm_tree_source_probe(source=lambda: "/nonexistent/kdive-no-such-tree")
    assert _run_probe(probe) is WarmTreeSourceOutcome.INVALID


def test_probe_invalid_for_a_file_not_a_dir(tmp_path: Path) -> None:
    file_path = tmp_path / "vmlinux"
    file_path.write_text("not a tree")
    probe = warm_tree_source_probe(source=lambda: str(file_path))
    assert _run_probe(probe) is WarmTreeSourceOutcome.INVALID


# ---- probe adapter (default config source, deferred to probe time) ------------------


def test_default_source_reads_kernel_src_from_config(monkeypatch, tmp_path: Path) -> None:
    # The default source reads KDIVE_KERNEL_SRC from the config snapshot, and resolution is
    # deferred to probe time: building the probe before loading the env still reflects the value
    # the env carries when the probe runs.
    probe = warm_tree_source_probe()
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path))
    config.load()
    assert _run_probe(probe) is WarmTreeSourceOutcome.USABLE


def test_default_source_unset_is_unset(monkeypatch) -> None:
    monkeypatch.delenv("KDIVE_KERNEL_SRC", raising=False)
    config.load()
    probe = warm_tree_source_probe()
    assert _run_probe(probe) is WarmTreeSourceOutcome.UNSET
