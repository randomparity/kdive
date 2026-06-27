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
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import kdive.config as config
import kdive.diagnostics.kernel_src as kernel_src
from kdive.diagnostics.checks import (
    LOCAL_KERNEL_SRC_FIX,
    LOCAL_KERNEL_SRC_ID,
    CheckStatus,
    LocalKernelSrcCheck,
    Vantage,
    WarmTreeSourceOutcome,
    WarmTreeSourceProbe,
    WarmTreeSourceProbeResult,
)
from kdive.diagnostics.kernel_src import _git_head, warm_tree_source_probe
from kdive.domain.errors import ErrorCategory

# Fake commit fixtures. Deliberately non-hex (so the secret scanner does not flag them) while
# still long enough to exercise the 12-char short-commit slice _git_head performs.
_FAKE_FULL_SHA = "headshaaaaaa-fixture"
_FAKE_SHORT_SHA = _FAKE_FULL_SHA[:12]


def _probe(result: WarmTreeSourceProbeResult) -> WarmTreeSourceProbe:
    async def probe() -> WarmTreeSourceProbeResult:
        return result

    return probe


def _outcome_probe(outcome: WarmTreeSourceOutcome) -> WarmTreeSourceProbe:
    return _probe(WarmTreeSourceProbeResult(outcome=outcome))


def _enabled(value: bool):
    async def probe() -> bool:
        return value

    return probe


def _run_probe(probe: WarmTreeSourceProbe) -> WarmTreeSourceProbeResult:
    async def _drive() -> WarmTreeSourceProbeResult:
        return await probe()

    return asyncio.run(_drive())


class _FakeProc:
    """A stand-in for ``subprocess.CompletedProcess`` (returncode + stdout only)."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def _fake_git(
    *, commit: str, branch: str, commit_rc: int = 0, branch_rc: int = 0
) -> Callable[..., _FakeProc]:
    """A ``subprocess.run`` replacement that answers the two `git rev-parse` argvs we issue."""

    def run(argv: list[str], **_kwargs: object) -> _FakeProc:
        if "--abbrev-ref" in argv:
            return _FakeProc(branch_rc, branch)
        return _FakeProc(commit_rc, commit)

    return run


# ---- check logic --------------------------------------------------------------------


def test_id_and_vantage() -> None:
    check = LocalKernelSrcCheck(probe=_outcome_probe(WarmTreeSourceOutcome.USABLE))
    assert check.id == LOCAL_KERNEL_SRC_ID == "local_kernel_src"
    assert check.vantage is Vantage.SERVER


def test_usable_is_pass() -> None:
    check = LocalKernelSrcCheck(probe=_outcome_probe(WarmTreeSourceOutcome.USABLE))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS
    assert result.fix is None
    assert result.failure_category is None
    assert result.provider is None
    assert "existing absolute tree" in result.detail


def test_pass_detail_discloses_server_vantage_not_the_build_worker() -> None:
    # The check reads the SERVER process's KDIVE_KERNEL_SRC (ADR-0163). On a split deployment the
    # build worker can carry a different env, so a confident "ok on the build worker" PASS would be
    # actively misleading (#701: green check while every build fails). The PASS must disclose that
    # it reflects the server's env and is not authoritative for a split-deployment build worker, and
    # must not claim the source was verified ON the build worker.
    check = LocalKernelSrcCheck(probe=_outcome_probe(WarmTreeSourceOutcome.USABLE))
    result = asyncio.run(check.run())
    assert "server" in result.detail.lower()
    assert "on the build worker" not in result.detail.lower()


def test_unset_is_fail_with_the_build_lane_fix() -> None:
    check = LocalKernelSrcCheck(probe=_outcome_probe(WarmTreeSourceOutcome.UNSET))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    assert result.fix == LOCAL_KERNEL_SRC_FIX
    assert result.failure_category is ErrorCategory.CONFIGURATION_ERROR
    assert result.provider is None
    assert "KDIVE_KERNEL_SRC" in result.detail
    assert "unset" in result.detail


def test_invalid_is_fail_with_the_build_lane_fix() -> None:
    check = LocalKernelSrcCheck(probe=_outcome_probe(WarmTreeSourceOutcome.INVALID))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL
    assert result.fix == LOCAL_KERNEL_SRC_FIX
    assert result.failure_category is ErrorCategory.CONFIGURATION_ERROR
    assert result.provider is None
    assert "absolute path" in result.detail


def test_both_fail_cases_carry_the_same_fix() -> None:
    unset_check = LocalKernelSrcCheck(probe=_outcome_probe(WarmTreeSourceOutcome.UNSET))
    invalid_check = LocalKernelSrcCheck(probe=_outcome_probe(WarmTreeSourceOutcome.INVALID))
    unset = asyncio.run(unset_check.run())
    invalid = asyncio.run(invalid_check.run())
    assert unset.fix == invalid.fix == LOCAL_KERNEL_SRC_FIX


def test_fix_names_both_build_lanes() -> None:
    # The remediation must name the two ways forward so an operator self-corrects from the
    # verdict alone: stage a warm tree + set KDIVE_KERNEL_SRC, or register a git build host.
    assert "KDIVE_KERNEL_SRC" in LOCAL_KERNEL_SRC_FIX
    assert "build_hosts.register" in LOCAL_KERNEL_SRC_FIX


def test_fix_cites_the_staging_doc_as_an_mcp_resource_uri() -> None:
    # The remediation reaches an MCP client, which cannot open a bare filesystem path; it must
    # cite the doc as the fetchable resource URI (ADR-0151), not "docs/operating/...".
    assert "resource://kdive/docs/operating/build-source-staging.md" in LOCAL_KERNEL_SRC_FIX
    assert "(docs/operating/" not in LOCAL_KERNEL_SRC_FIX


# ---- enabled-gate (ADR-0167) --------------------------------------------------------


def test_disabled_local_host_is_na_pass_even_when_source_unset() -> None:
    # When the seeded worker-local host is disabled, the local warm-tree lane has no contract to
    # violate, so an unset KDIVE_KERNEL_SRC is a pass (n/a), not a fail — clears the ADR-0163
    # exit-code regression for git/SSH/ephemeral-only deployments.
    check = LocalKernelSrcCheck(
        probe=_outcome_probe(WarmTreeSourceOutcome.UNSET), enabled_probe=_enabled(False)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS
    assert result.fix is None
    assert result.failure_category is None
    assert "disabled" in result.detail.lower()


def test_enabled_local_host_unset_still_fails() -> None:
    check = LocalKernelSrcCheck(
        probe=_outcome_probe(WarmTreeSourceOutcome.UNSET), enabled_probe=_enabled(True)
    )
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL


def test_default_enabled_probe_runs_the_warm_tree_verdict() -> None:
    # No enabled_probe supplied → default always-enabled → existing behavior unchanged.
    check = LocalKernelSrcCheck(probe=_outcome_probe(WarmTreeSourceOutcome.UNSET))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.FAIL


# ---- probe adapter (source injected) ------------------------------------------------


def test_probe_unset_for_empty_and_whitespace() -> None:
    for value in ("", "   ", "\t\n"):
        probe = warm_tree_source_probe(source=lambda v=value: v)
        assert _run_probe(probe).outcome is WarmTreeSourceOutcome.UNSET


def test_probe_usable_for_existing_absolute_dir(tmp_path: Path) -> None:
    # git_head stubbed to (None, None) so the classification test stays git-free; the USABLE
    # result discloses the resolved absolute path it was given.
    probe = warm_tree_source_probe(source=lambda: str(tmp_path), git_head=lambda _t: (None, None))
    result = _run_probe(probe)
    assert result.outcome is WarmTreeSourceOutcome.USABLE
    assert result.resolved_path == str(tmp_path)


def test_probe_usable_discloses_git_head_when_a_checkout(tmp_path: Path) -> None:
    # On a usable git checkout the probe carries the injected (short_commit, branch).
    probe = warm_tree_source_probe(
        source=lambda: str(tmp_path), git_head=lambda _t: (_FAKE_SHORT_SHA, "main")
    )
    result = _run_probe(probe)
    assert result.outcome is WarmTreeSourceOutcome.USABLE
    assert result.resolved_path == str(tmp_path)
    assert result.head_commit == _FAKE_SHORT_SHA
    assert result.branch == "main"


def test_probe_usable_omits_git_head_for_a_non_git_tree(tmp_path: Path) -> None:
    probe = warm_tree_source_probe(source=lambda: str(tmp_path), git_head=lambda _t: (None, None))
    result = _run_probe(probe)
    assert result.head_commit is None
    assert result.branch is None


def test_probe_verdict_survives_a_hung_git_read(tmp_path: Path, monkeypatch) -> None:
    # A slow/hung git read must never push the check past its budget or change the verdict: the
    # bound fires, the git fields stay unset, and USABLE stands (#845).
    monkeypatch.setattr(kernel_src, "_GIT_READ_TIMEOUT", 0.05)

    def _hang(_tree: str) -> tuple[str | None, str | None]:
        time.sleep(0.5)
        return "never", "never"

    probe = warm_tree_source_probe(source=lambda: str(tmp_path), git_head=_hang)
    result = _run_probe(probe)
    assert result.outcome is WarmTreeSourceOutcome.USABLE
    assert result.resolved_path == str(tmp_path)
    assert result.head_commit is None
    assert result.branch is None


def test_probe_verdict_survives_a_raising_git_read(tmp_path: Path) -> None:
    def _boom(_tree: str) -> tuple[str | None, str | None]:
        raise RuntimeError("git blew up")

    probe = warm_tree_source_probe(source=lambda: str(tmp_path), git_head=_boom)
    result = _run_probe(probe)
    assert result.outcome is WarmTreeSourceOutcome.USABLE
    assert result.head_commit is None
    assert result.branch is None


def test_probe_invalid_for_relative_path() -> None:
    probe = warm_tree_source_probe(source=lambda: "linux")
    assert _run_probe(probe).outcome is WarmTreeSourceOutcome.INVALID


def test_probe_invalid_for_nonexistent_absolute_path() -> None:
    probe = warm_tree_source_probe(source=lambda: "/nonexistent/kdive-no-such-tree")
    assert _run_probe(probe).outcome is WarmTreeSourceOutcome.INVALID


def test_probe_invalid_for_a_file_not_a_dir(tmp_path: Path) -> None:
    file_path = tmp_path / "vmlinux"
    file_path.write_text("not a tree")
    probe = warm_tree_source_probe(source=lambda: str(file_path))
    assert _run_probe(probe).outcome is WarmTreeSourceOutcome.INVALID


# ---- probe adapter (default config source, deferred to probe time) ------------------


def test_default_source_reads_kernel_src_from_config(monkeypatch, tmp_path: Path) -> None:
    # The default source reads KDIVE_KERNEL_SRC from the config snapshot, and resolution is
    # deferred to probe time: building the probe before loading the env still reflects the value
    # the env carries when the probe runs.
    probe = warm_tree_source_probe(git_head=lambda _t: (None, None))
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path))
    config.load()
    assert _run_probe(probe).outcome is WarmTreeSourceOutcome.USABLE


def test_default_source_unset_is_unset(monkeypatch) -> None:
    monkeypatch.delenv("KDIVE_KERNEL_SRC", raising=False)
    config.load()
    probe = warm_tree_source_probe()
    assert _run_probe(probe).outcome is WarmTreeSourceOutcome.UNSET


# ---- check data disclosure (#845) ---------------------------------------------------


def test_usable_check_data_carries_path_and_git_head() -> None:
    probe = _probe(
        WarmTreeSourceProbeResult(
            outcome=WarmTreeSourceOutcome.USABLE,
            resolved_path="/abs/linux",
            head_commit=_FAKE_SHORT_SHA,
            branch="main",
        )
    )
    result = asyncio.run(LocalKernelSrcCheck(probe=probe).run())
    assert result.status is CheckStatus.PASS
    assert result.data == {
        "vantage": "server",
        "resolved_path": "/abs/linux",
        "head_commit": _FAKE_SHORT_SHA,
        "branch": "main",
    }


def test_usable_check_data_omits_absent_git_fields() -> None:
    # A usable non-git tree carries the path + server vantage but no commit/branch keys.
    probe = _probe(
        WarmTreeSourceProbeResult(outcome=WarmTreeSourceOutcome.USABLE, resolved_path="/abs/linux")
    )
    result = asyncio.run(LocalKernelSrcCheck(probe=probe).run())
    assert result.data == {"vantage": "server", "resolved_path": "/abs/linux"}


def test_fail_outcomes_disclose_no_data() -> None:
    for outcome in (WarmTreeSourceOutcome.UNSET, WarmTreeSourceOutcome.INVALID):
        result = asyncio.run(LocalKernelSrcCheck(probe=_outcome_probe(outcome)).run())
        assert result.status is CheckStatus.FAIL
        assert result.data is None


def test_disabled_na_pass_discloses_no_data() -> None:
    result = asyncio.run(
        LocalKernelSrcCheck(
            probe=_outcome_probe(WarmTreeSourceOutcome.UNSET), enabled_probe=_enabled(False)
        ).run()
    )
    assert result.status is CheckStatus.PASS
    assert result.data is None


# ---- git reader (subprocess patched; no real checkout) ------------------------------


def test_git_head_returns_short_commit_and_branch(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_git(commit=f"{_FAKE_FULL_SHA}\n", branch="main\n"))
    assert _git_head("/abs/linux") == (_FAKE_SHORT_SHA, "main")


def test_git_head_none_for_a_non_git_tree(monkeypatch) -> None:
    # rev-parse HEAD fails (rc != 0) → no commit, and the branch read is skipped entirely.
    monkeypatch.setattr(subprocess, "run", _fake_git(commit="", branch="x", commit_rc=128))
    assert _git_head("/abs/linux") == (None, None)


def test_git_head_branch_none_on_detached_head(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", _fake_git(commit=f"{_FAKE_FULL_SHA}\n", branch="HEAD\n"))
    assert _git_head("/abs/linux") == (_FAKE_SHORT_SHA, None)


def test_git_head_handles_missing_git_binary(monkeypatch) -> None:
    def _no_git(argv: list[str], **_kwargs: object):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", _no_git)
    assert _git_head("/abs/linux") == (None, None)
