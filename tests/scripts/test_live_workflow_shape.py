"""Pin the live.yml security + cleanup posture at the source (#1293, ADR-0389).

A future edit that re-exposes the self-hosted runner to fork PRs, or re-enables mid-boot
cancellation, must fail here — the analogue of test_live_vm_tcg_tier.py pinning the marker set.
"""

from __future__ import annotations

import pathlib
import re

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


def _catalog_names() -> set[str]:
    import tomllib

    catalog_path = _ROOT / "fixtures" / "local-libvirt" / "rootfs_catalog.toml"
    catalog = tomllib.loads(catalog_path.read_text(encoding="utf-8"))
    return {img["name"] for img in catalog.get("image", [])}


def _tcg_image_input_default() -> str:
    return _triggers(_load(_LIVE))["workflow_dispatch"]["inputs"]["tcg_image"]["default"]


def _tcg_image_run_fallback() -> str:
    # The tcg run block resolves the image on schedule/push as
    #   export KDIVE_TCG_IMAGE="${TCG_IMAGE_INPUT:-<fallback>}"
    # because TCG_IMAGE_INPUT is empty off workflow_dispatch. Pull that bash default out.
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    run = next(s["run"] for s in steps if "run" in s and "spine" in s.get("name", "").lower())
    match = re.search(r'KDIVE_TCG_IMAGE="\$\{TCG_IMAGE_INPUT:-([^}"]+)\}"', run)
    assert match, "could not find the KDIVE_TCG_IMAGE fallback assignment in the tcg run block"
    return match.group(1)


def test_tcg_default_image_is_a_real_catalog_entry() -> None:
    # On workflow_dispatch (no override) the tcg gate builds from the tcg_image input default. A
    # name absent from the rootfs catalog produces no rootfs and fails deep (virt-ls: No such file).
    default = _tcg_image_input_default()
    assert default in _catalog_names(), (
        f"tcg_image default {default!r} is not a rootfs_catalog.toml entry"
    )


def test_tcg_schedule_fallback_image_is_a_real_catalog_entry() -> None:
    # On schedule/push TCG_IMAGE_INPUT is empty, so the run block's bash fallback is the built one.
    # A bogus fallback (the old `fedora-ppc64le`) breaks every non-dispatch run; pin it to catalog.
    fallback = _tcg_image_run_fallback()
    assert fallback in _catalog_names(), (
        f"tcg schedule/push fallback image {fallback!r} is not a rootfs_catalog.toml entry"
    )


def test_tcg_input_default_and_schedule_fallback_agree() -> None:
    # Two independently-maintained defaults (the workflow_dispatch input and the bash fallback) must
    # not drift: a dispatch and a scheduled run must build the same ppc64le guest.
    assert _tcg_image_input_default() == _tcg_image_run_fallback()


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


def test_tcg_job_makes_the_host_kernel_readable_for_supermin() -> None:
    """libguestfs builds its supermin appliance from the host kernel (ADR-0222 cause 1).

    ubuntu-latest ships /boot/vmlinuz-* as 0600 root:root, so the non-root runner cannot read it
    and `virt-tar-out` dies with "supermin exited with error status 1" — build-fs never produces a
    rootfs. The self-hosted runner gets this from deploy/ansible/roles/live_vm_host; the hosted
    runner has no provisioning step, so the workflow must do it before staging.
    """
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    joined = "\n".join(s["run"] for s in steps if "run" in s)
    assert "chmod" in joined and "/boot/vmlinuz-" in joined, (
        "the tcg job must make /boot/vmlinuz-* readable before build-fs runs"
    )
    # It has to happen before the spine stages the image, not after.
    order = [i for i, s in enumerate(steps) if "run" in s and "/boot/vmlinuz-" in s["run"]]
    spine = next(i for i, s in enumerate(steps) if "spine" in s.get("name", "").lower())
    assert order and min(order) <= spine, "the kernel chmod must precede the staging spine"


def test_tcg_job_runs_on_the_image_that_ships_a_matching_guestfs_binding() -> None:
    """kdive pins Python 3.14 and `guestfs` is a C extension, so the ABI must match (ADR-0387).

    Only Ubuntu 26.04 ships a system Python 3.14 with a matching python3-guestfs; on 24.04 the
    binding is built for 3.12 and cannot be imported by the 3.14 venv at all. `ubuntu-latest`
    tracks the GA image, so it must not be used here — it silently regresses to 24.04.
    """
    assert _load(_LIVE)["jobs"]["tcg"]["runs-on"] == "ubuntu-26.04"


def test_tcg_job_builds_its_venv_against_the_system_interpreter() -> None:
    """uv's managed CPython would not ABI-match the distro's binding; pin the system one."""
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    joined = "\n".join(s["run"] for s in steps if "run" in s)
    assert "python3-guestfs" in joined, "the system libguestfs binding must be installed"
    assert "--python /usr/bin/python3" in joined, (
        "the venv must be pinned to the system interpreter"
    )


def test_tcg_job_links_the_guestfs_binding_into_the_venv_and_proves_it_imports() -> None:
    """No PyPI wheel exists, so the binding is symlinked in — and the import is verified here.

    build-fs only reaches `import guestfs` several minutes into the image build, so a setup-time
    proof is what keeps a broken link from costing a whole run to diagnose.
    """
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    joined = "\n".join(s["run"] for s in steps if "run" in s)
    assert "libguestfsmod" in joined, "the native module must be linked, not just guestfs.py"
    assert "import guestfs" in joined, "the tcg job must prove the binding imports before staging"


def _tcg_spine() -> str:
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    return next(s["run"] for s in steps if "run" in s and "spine" in s.get("name", "").lower())


def test_tcg_job_installs_the_libvirt_daemon_not_just_the_headers() -> None:
    """libvirt-dev is headers for building libvirt-python; the daemon is a separate package.

    build-fs opens a libvirt connection to resolve the customization-boot accelerator, so without
    a daemon it dies on "Failed to connect socket to /var/run/libvirt/libvirt-sock" — minutes into
    the build. Mirrors libvirt_stack's Debian package set.
    """
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    joined = "\n".join(s["run"] for s in steps if "run" in s)
    for pkg in ("libvirt-daemon-system", "libvirt-clients", "qemu-utils"):
        assert pkg in joined, f"the tcg job must install {pkg}"


def test_tcg_job_pins_the_session_libvirt_uri() -> None:
    """Session mode, as the native job uses: worker-owned QEMU with a readable console (ADR-0223).

    It also sidesteps libvirt group membership, which a `usermod` inside a job cannot grant to the
    already-running shell.
    """
    assert 'KDIVE_LIBVIRT_URI="qemu:///session"' in _tcg_spine()


def test_tcg_job_preflights_the_host_before_staging() -> None:
    """The whole point is ordering: a missing daemon must fail in seconds, not mid-build."""
    spine = _tcg_spine()
    host_check = spine.index("preflight-env.sh host")
    staging = spine.index("stage-tcg-images.sh")
    assert host_check < staging, "the host preflight must run before the staging spine"
