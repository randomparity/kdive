"""Pin the live.yml security + cleanup posture at the source (#1293, ADR-0389).

A future edit that re-exposes the self-hosted runner to fork PRs, or re-enables mid-boot
cancellation, must fail here — the analogue of test_live_vm_tcg_tier.py pinning the marker set.
"""

from __future__ import annotations

import pathlib

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LIVE = _ROOT / ".github" / "workflows" / "live.yml"
_CI = _ROOT / ".github" / "workflows" / "ci.yml"


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    # PyYAML parses the bare `on:` key as the boolean True; fall back to the "on" string key.
    return doc[True] if True in doc else doc["on"]


def test_live_yml_has_no_pull_request_trigger() -> None:
    triggers = _triggers(_load(_LIVE))
    assert "pull_request" not in triggers
    assert "pull_request_target" not in triggers


def test_native_job_uses_positive_event_allowlist() -> None:
    native = _load(_LIVE)["jobs"]["native"]
    cond = native["if"]
    assert "schedule" in cond and "workflow_dispatch" in cond
    assert "!=" not in cond  # a `!= 'pull_request'` guard would admit push — forbidden


def test_tcg_job_never_positively_runs_on_pull_request() -> None:
    # The workflow has no pull_request trigger, so no job runs on PRs; additionally pin that the tcg
    # guard never POSITIVELY admits a PR (it may exclude one via `!= 'pull_request'`).
    tcg = _load(_LIVE)["jobs"]["tcg"]
    assert "== 'pull_request'" not in tcg.get("if", "")


def test_both_jobs_disable_cancel_in_progress() -> None:
    jobs = _load(_LIVE)["jobs"]
    for name in ("tcg", "native"):
        assert jobs[name]["concurrency"]["cancel-in-progress"] is False


def test_ci_yml_no_longer_defines_a_live_vm_job() -> None:
    assert "live-vm" not in _load(_CI)["jobs"]
