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


def test_native_block_exports_warm_store_wiring() -> None:
    # emit_wiring prints bare (non-export) assignments, so the native run block must export the
    # warm-store wiring vars or the child mint-system.sh / preflight / pytest never see the rootfs.
    steps = _load(_LIVE)["jobs"]["native"]["steps"]
    run = next(s["run"] for s in steps if "run" in s)
    exported = " ".join(ln for ln in run.splitlines() if ln.strip().startswith("export"))
    for var in ("KDIVE_LIVE_VM_ROOTFS", "KDIVE_LIVE_VM_BZIMAGE", "KDIVE_LIVE_VM_VMLINUX"):
        assert var in exported, f"{var} not exported in the native run block"


def test_tcg_block_stages_into_a_runner_owned_dir() -> None:
    # /mnt is root-owned on the ubuntu-latest hosted runner, so stage-tcg-images.sh's `mkdir` there
    # fails (permission denied). The tcg block must sudo-create + chown a parent and stage into a
    # SUBDIR — the script rm -rf's + recreates its stage dir, which must not be the mount point.
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    run = next(s["run"] for s in steps if "run" in s and "spine" in s.get("name", "").lower())
    stage_subdir = "KDIVE_TCG_STAGE_DIR=/mnt/kdive-tcg/"  # pragma: allowlist secret
    assert "sudo chown" in run, "the tcg staging dir must be chowned to the runner user"
    assert stage_subdir in run, "stage into a runner-owned subdir, not /mnt"


def test_native_block_boots_provisioned_family_under_session() -> None:
    # The non-root, no-sudo runner cannot read qemu:///system's root-owned console log (ADR-0223);
    # the provisioned family must boot under qemu:///session (worker-owned QEMU) so console-reading
    # tests pass. A regression to qemu:///system would silently re-break every such test.
    steps = _load(_LIVE)["jobs"]["native"]["steps"]
    run = next(s["run"] for s in steps if "run" in s)
    # Scope to KDIVE_LIBVIRT_URI assignments: the reaper legitimately sweeps qemu:///system too
    # (legacy leftovers), so a blanket qemu:///system search would false-positive on the reaper.
    uri_assignments = [ln for ln in run.splitlines() if "KDIVE_LIBVIRT_URI=" in ln]
    assert uri_assignments, "the native block must set KDIVE_LIBVIRT_URI"
    assert all("qemu:///session" in ln for ln in uri_assignments), (
        "the provisioned family must boot under qemu:///session, not qemu:///system "
        "(the non-root, no-sudo runner cannot read a root-owned console log — ADR-0223)"
    )
