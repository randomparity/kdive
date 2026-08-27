"""Pin the live.yml security + cleanup posture at the source (#1293, ADR-0389).

A future edit that re-exposes the self-hosted runner to fork PRs, or re-enables mid-boot
cancellation, must fail here — the analogue of test_live_vm_tcg_tier.py pinning the marker set.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest
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
    run = _native_spine()
    exported = " ".join(ln for ln in run.splitlines() if ln.strip().startswith("export"))
    for var in ("KDIVE_LIVE_VM_ROOTFS", "KDIVE_LIVE_VM_BZIMAGE", "KDIVE_LIVE_VM_VMLINUX"):
        assert var in exported, f"{var} not exported in the native run block"


def _native_spine() -> str:
    """The native job's one big shell, selected by name — not just its first run block."""
    steps = _load(_LIVE)["jobs"]["native"]["steps"]
    return next(s["run"] for s in steps if s.get("name", "").startswith("Run both native families"))


def test_native_block_preflights_debug_stepping_with_both_native_families() -> None:
    run = _native_spine()
    assert "preflight-env.sh throwaway provisioned debug-stepping" in run


def _tcg_stage_dir() -> str:
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    run = next(s["run"] for s in steps if "run" in s and "spine" in s.get("name", "").lower())
    prefix = "export KDIVE_TCG_STAGE_DIR="
    line = next(ln.strip() for ln in run.splitlines() if ln.strip().startswith(prefix))
    return line[len(prefix) :]


def test_tcg_block_stages_inside_the_provider_allowed_root() -> None:
    """The provisioner only accepts rootfs paths under its allowed root (ADR-0224, #731).

    ``LocalLibvirtProvisioning.from_env`` hardcodes ``allowed_roots=[Path(ROOTFS_DIR)]`` with no
    env override, so a set staged anywhere else is rejected at provision time with "local component
    path is outside provider allowed roots" — minutes into the run, after the whole image build.
    Read ROOTFS_DIR from src rather than repeating the literal, so moving the constant fails here.
    """
    from kdive.providers.local_libvirt.lifecycle import storage

    stage = _tcg_stage_dir()
    assert stage.startswith(f"{storage.ROOTFS_DIR}/"), (
        f"KDIVE_TCG_STAGE_DIR={stage} is outside the provider's allowed root "
        f"{storage.ROOTFS_DIR}; provision would reject the staged rootfs"
    )
    # A SUBDIR, never the root itself: stage-tcg-images.sh rm -rf's + recreates its stage dir, and
    # the provider writes every per-System overlay into the root alongside it.
    assert stage.rstrip("/") != storage.ROOTFS_DIR, (
        "stage into a subdirectory: the stager deletes and recreates KDIVE_TCG_STAGE_DIR, "
        "which would take the provider's overlay dir with it"
    )


def test_tcg_allowed_root_is_backed_by_the_large_scratch_disk() -> None:
    """~7 GB of staged set (#1292) must not land on the runner's small root filesystem.

    The provider's allowed root is a fixed path, so the only way to keep the budget on /mnt is to
    back that path with the scratch disk. validate_local_component_path resolves both the candidate
    and the roots, so a symlinked root still matches; `df` follows it too, which keeps
    stage-tcg-images.sh's pre-stage free-space check measuring the disk the bytes actually land on.
    """
    from kdive.providers.local_libvirt.lifecycle import storage

    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    joined = "\n".join(s["run"] for s in steps if "run" in s)
    link = next(
        (ln.strip() for ln in joined.splitlines() if "ln -" in ln and storage.ROOTFS_DIR in ln),
        None,
    )
    assert link is not None, (
        f"{storage.ROOTFS_DIR} must be backed by the /mnt scratch disk, not the root filesystem"
    )
    assert "/mnt/" in link, f"the allowed root must point at /mnt; got {link!r}"


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


@pytest.mark.parametrize("job", ("tcg", "native"))
def test_live_job_loads_the_provisioned_libvirt_uri_without_hardcoding(job: str) -> None:
    runs = _job_run_blocks(job)
    assert "load_published_libvirt_uri" in runs
    assert "scripts/live-stack/libvirt-uri.sh" in runs
    parser = (_ROOT / "scripts/live-stack/libvirt-uri.sh").read_text()
    assert "readonly LIBVIRT_ENV=/etc/kdive/live-worker-libvirt.env" in parser
    assert 'KDIVE_LIBVIRT_URI="qemu:///session"' not in runs
    assert "KDIVE_LIBVIRT_URI=qemu:///session" not in runs


@pytest.mark.parametrize("job", ("tcg", "native"))
def test_live_job_propagates_the_published_uri_through_spine_and_cleanup(job: str) -> None:
    steps = _load(_LIVE)["jobs"][job]["steps"]
    test_run = next(
        step["run"]
        for step in steps
        if "run" in step and ("-m live_vm_tcg" in step["run"] or "not live_vm_tcg" in step["run"])
    )
    cleanup = next(step["run"] for step in steps if step.get("name") == "Clean up live stack")
    loader = 'KDIVE_LIBVIRT_URI="$(load_published_libvirt_uri)"'
    assert loader in test_run
    assert loader in cleanup
    assert "export KDIVE_LIBVIRT_URI" in test_run
    assert "export KDIVE_LIBVIRT_URI" in cleanup


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


def test_tcg_job_uses_the_published_session_libvirt_uri() -> None:
    """Every actor must use the same dedicated session daemon as the fixed workers."""
    assert 'KDIVE_LIBVIRT_URI="$(load_published_libvirt_uri)"' in _tcg_spine()
    assert "export KDIVE_LIBVIRT_URI" in _tcg_spine()


def test_hosted_job_installs_fixed_lifecycle_contract_after_uv_sync() -> None:
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    sync = next(i for i, step in enumerate(steps) if "uv sync --locked" in step.get("run", ""))
    install = next(
        i
        for i, step in enumerate(steps)
        if "install-live-worker-lifecycle.sh" in step.get("run", "")
    )
    assert sync < install
    command = steps[install]["run"]
    assert '--operator "$(id -un)" --source "$GITHUB_WORKSPACE"' in command
    assert "printf" in command and "| sudo" in command
    assert "kdive-witness-member" in command
    assert "kdive-witness-local" in command
    assert "KDIVE_DATABASE_URL" not in command
    assert "--witness-dsn" not in command


def test_native_job_installs_fixed_lifecycle_contract_before_stack_up() -> None:
    """#2050: the persistent native box carries a stale installed witness revision unless the

    native arm re-installs the contract from its OWN checkout before stack-up — the same gap the
    tcg job closed with its identically named step (run 32585991555 died at host-process start
    with "installed lifecycle witness revision does not match this checkout").
    """
    steps = _load(_LIVE)["jobs"]["native"]["steps"]
    install = next(
        i
        for i, step in enumerate(steps)
        if "install-live-worker-lifecycle.sh" in step.get("run", "")
    )
    spine = next(i for i, step in enumerate(steps) if step.get("name", "").startswith("Run both"))
    assert install < spine, "the native install must precede the single shell that runs up.sh"
    command = steps[install]["run"]
    # Same installer invocation as the tcg step: DSN over stdin only, operator + source pinned.
    assert '--operator "$(id -un)" --source "$GITHUB_WORKSPACE"' in command
    assert "printf" in command and "| sudo" in command
    assert "kdive-witness-member" in command and "kdive-witness-local" in command
    assert "KDIVE_DATABASE_URL" not in command
    assert "--witness-dsn" not in command


def test_native_install_step_is_named_like_the_tcg_one() -> None:
    _, _ = _named_step("native", "Install the fixed live-worker lifecycle host contract")


def test_hosted_spine_enters_refreshed_control_group_and_probes_socket() -> None:
    spine = _tcg_spine()
    assert "sudo --preserve-env" in spine
    assert '--user="$operator_name" --group=kdive-live-control' in " ".join(spine.split())
    assert "id -G" in spine
    assert "kdive-live-control" in spine and "kdive-live-libvirt" in spine
    assert "live-worker-lifecycle.sock" in spine
    assert ".connect(" in spine


@pytest.mark.parametrize(
    ("step_name", "job"),
    (
        ("Prove systemd worker lifecycle against disposable Postgres", "tcg"),
        ("Run the live_vm_tcg spine (stage -> up -> preflight -> test, one shell)", "tcg"),
    ),
)
def test_hosted_tcg_shell_reinitializes_all_operator_groups_once(step_name: str, job: str) -> None:
    _, step = _named_step(job, step_name)
    run = step["run"]
    assert "sudo --preserve-env" in run
    assert '--user="$operator_name" --group=kdive-live-control' in " ".join(run.split())
    assert "kdive-live-control" in run and "kdive-live-libvirt" in run
    assert "sg kdive-live-libvirt" not in run
    assert "sg kdive-live-control" not in run


def test_tcg_job_preflights_the_host_before_staging() -> None:
    """The whole point is ordering: a missing daemon must fail in seconds, not mid-build."""
    spine = _tcg_spine()
    host_check = spine.index("preflight-env.sh host")
    staging = spine.index("stage-tcg-images.sh")
    assert host_check < staging, "the host preflight must run before the staging spine"


def test_tcg_job_provisions_the_hardcoded_runtime_directories() -> None:
    """The provider cannot be pointed elsewhere for these, and /var/lib/kdive is root-owned.

    The console dir is the one that actually broke a run; pcap and rootfs are the rest of the same
    class, provisioned together so the next System-provisioning proof does not rediscover them.
    """
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    joined = "\n".join(s["run"] for s in steps if "run" in s)
    for path in ("/var/lib/kdive/console", "/var/lib/kdive/pcap", "/var/lib/kdive/rootfs"):
        assert path in joined, f"the tcg job must provision {path}"


def test_tcg_runtime_dirs_become_fixed_worker_writable_after_account_install() -> None:
    _, install = _named_step("tcg", "Install the fixed live-worker lifecycle host contract")
    run = install["run"]

    for path in ("/var/lib/kdive/console", "/var/lib/kdive/pcap", "/mnt/kdive-rootfs"):
        assert path in run
    assert ":kdive-live-libvirt" in run
    assert "chmod 2770" in run
    assert "--user=kdive-worker-1 --group=kdive-live-libvirt test -w" in " ".join(run.split())
    assert "fixed worker cannot write provider data directory" in run
    assert "--user=kdive-worker-1 --group=kdive-live-libvirt id" in " ".join(run.split())


# --- app-tier topology: host processes, never containers -------------------------------------
#
# The local-libvirt provider is explicitly NOT containerized (Dockerfile header): the compose
# image carries no /dev/kvm, no libvirt socket and no privileged flag, so a containerized worker
# cannot boot the ppc64le guest at all. It also cannot see the host-side resources this job
# provisions (/var/lib/kdive/*, the staged rootfs under /mnt, qemu-system-ppc64).
#
# The auth symptom is the same root cause: the mock issuer is host-header-relative, so a token
# minted by the host-side test carries iss=http://localhost:8090/default while a compose `server`
# is configured with iss=http://oidc:8080/default and JWTVerifier rejects it (401). Host processes
# read scripts/live-stack/env.sh, so minting side and verifying side share one identity.

_APP_TIER_SERVICES = ("server", "worker", "reconciler")


def _job_run_blocks(job: str) -> str:
    return "\n".join(s["run"] for s in _load(_LIVE)["jobs"][job]["steps"] if "run" in s)


def _named_step(job: str, name: str) -> tuple[int, dict]:
    steps = _load(_LIVE)["jobs"][job]["steps"]
    index = next(index for index, step in enumerate(steps) if step.get("name") == name)
    return index, steps[index]


def test_tcg_installs_manifest_for_the_fixed_worker_before_starting_it() -> None:
    install_index, _ = _named_step("tcg", "Install the fixed live-worker lifecycle host contract")
    manifest_index, manifest = _named_step("tcg", "Build and install fixed-worker capture manifest")
    proof_index, _ = _named_step(
        "tcg", "Prove systemd worker lifecycle against disposable Postgres"
    )
    spine_index, _ = _named_step(
        "tcg", "Run the live_vm_tcg spine (stage -> up -> preflight -> test, one shell)"
    )
    run = manifest["run"]

    assert "/opt/kdive-live-worker-lifecycle/.venv/bin/python" in run
    assert 'sysconfig.get_path("purelib")' in run
    assert "build-capture-bootstrap-manifest.py build" in run
    assert "build-capture-bootstrap-manifest.py install" in run
    assert "build-capture-bootstrap-manifest.py verify" in run
    assert "/usr/share/kdive/capture-bootstrap-manifest.json" in run
    assert "0:0:644" in run
    assert "--user=kdive-worker-1 --group=kdive-worker-1" in run
    assert "verify_capture_bootstrap_manifest" in run
    assert "sudo stat -Lc '%u %g %a' /usr/share/kdive" in run
    assert "*[!0-9:]*" in run
    assert "${#parent_uid} > 10 || ${#parent_gid} > 10" in run
    assert (
        "capture_manifest_parent component=capture_manifest_parent uid=%s gid=%s mode=%s"
    ) in run
    assert "capture_manifest_parent path=" not in run
    assert "fingerprint_path_not_safely_openable" in run
    assert "capture_manifest_verification status=rejected reason={reason}" in run
    assert "capture_manifest_verification status=accepted reason=none" in run
    assert "print(error)" not in run
    assert install_index < manifest_index < proof_index < spine_index


def test_tcg_fixed_worker_verifier_sanitizes_import_failure(tmp_path: pathlib.Path) -> None:
    package = tmp_path / "kdive" / "jobs" / "capture_operations"
    package.mkdir(parents=True)
    for parent in (package.parents[2], package.parents[1], package):
        (parent / "__init__.py").write_text("", encoding="utf-8")
    (package / "launcher.py").write_text(
        'raise RuntimeError("import failed at /sensitive/import/path")\n',
        encoding="utf-8",
    )
    _, manifest = _named_step("tcg", "Build and install fixed-worker capture manifest")
    verifier = manifest["run"].split("\"$worker_python\" - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    result = subprocess.run(
        [sys.executable, "-c", verifier],
        cwd=_ROOT,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 1
    assert result.stdout == ("capture_manifest_verification status=rejected reason=unclassified\n")
    assert result.stderr == ""
    assert "/sensitive/import/path" not in result.stdout
    assert "/sensitive/import/path" not in result.stderr


def test_tcg_job_captures_exact_provision_boundary_on_every_outcome() -> None:
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    spine_index, spine_step = _named_step(
        "tcg", "Run the live_vm_tcg spine (stage -> up -> preflight -> test, one shell)"
    )
    evidence_index, evidence = _named_step("tcg", "Capture persisted provision boundary")
    diagnostic_index, _ = _named_step("tcg", "Capture worker lifecycle diagnostics")
    cleanup_index, _ = _named_step("tcg", "Clean up live stack")
    target = "$RUNNER_TEMP/kdive-provision-evidence.target"

    assert f'export KDIVE_PROVISION_EVIDENCE_TARGET="{target}"' in spine_step["run"]
    assert evidence["if"] == "always()"
    assert target in evidence["run"]
    assert "timeout --signal=TERM --kill-after=2s 12s" in evidence["run"]
    assert "scripts/live-stack/provision-queue-diagnostics.sh" in evidence["run"]
    assert "::stop-commands::" in evidence["run"]
    assert "provision boundary evidence unavailable" in evidence["run"]
    assert "exit 0" in evidence["run"]
    assert spine_index < evidence_index < diagnostic_index < cleanup_index
    assert steps[evidence_index]["name"] == "Capture persisted provision boundary"


def test_tcg_job_captures_bounded_worker_readiness_components() -> None:
    spine_index, _ = _named_step(
        "tcg", "Run the live_vm_tcg spine (stage -> up -> preflight -> test, one shell)"
    )
    readiness_index, readiness = _named_step("tcg", "Capture worker readiness components")
    diagnostic_index, _ = _named_step("tcg", "Capture worker lifecycle diagnostics")
    cleanup_index, _ = _named_step("tcg", "Clean up live stack")
    run = readiness["run"]

    assert readiness["if"] == "always()"
    assert "http://127.0.0.1:9465/readyz" in run
    assert "--max-time 8" in run
    assert "--max-filesize 4096" in run
    assert "scripts/live-stack/filter-worker-readiness-evidence.py" in run
    assert "::stop-commands::" in run
    assert "worker readiness evidence unavailable" in run
    assert "exit 0" in run
    assert spine_index < readiness_index < diagnostic_index < cleanup_index


@pytest.mark.parametrize(
    ("job", "condition"),
    (("tcg", "always()"), ("native", "failure() || cancelled()")),
)
def test_live_job_captures_lifecycle_diagnostics_before_cleanup(job: str, condition: str) -> None:
    """Diagnostics are observational and must run before destructive teardown (#1939).

    The diagnostics step never fails the job (`exit 0`), neutralizes workflow-command
    injection from journal text (::stop-commands:: token), and degrades to a warning when
    the witness withholds evidence. Cleanup runs on every outcome; diagnostics must have
    their chance first — after teardown there is nothing left to read.
    """
    diagnostic_index, diagnostic = _named_step(job, "Capture worker lifecycle diagnostics")
    cleanup_index, cleanup = _named_step(job, "Clean up live stack")

    assert diagnostic["if"] == condition
    assert "scripts/live-stack/worker-lifecycle.sh diagnostics" in diagnostic["run"]
    assert "|| diagnostic_status=$?" in diagnostic["run"]
    assert "::stop-commands::" in diagnostic["run"]
    assert "printf '::%s::" in diagnostic["run"]
    assert "::${" not in diagnostic["run"]
    assert "exit 0" in diagnostic["run"]
    assert cleanup["if"] == "always()"
    assert "scripts/live-stack/down.sh" in cleanup["run"]
    assert diagnostic_index < cleanup_index


@pytest.mark.parametrize("job", ("tcg", "native"))
def test_live_job_diagnostics_capture_terminated_worker_journals(job: str) -> None:
    """Diagnostics must read the worker journal even when the fleet already stopped (#2056).

    The witness reports ``diagnostics=null`` for a phase=terminated fleet, which is exactly
    the state a red proof reaches — so the step also reads the worker units' journal
    directly (system units per ADR-0574, so a ``--user`` read would see nothing), inside
    the ::stop-commands:: guard because journal text can carry workflow-command-shaped
    lines, and best-effort like every other capture (#1939). Only the hosted TCG proof
    applies #2056's fixed-record filter; native retains its pre-existing direct diagnostics.
    """
    _, diagnostic = _named_step(job, "Capture worker lifecycle diagnostics")
    run = diagnostic["run"]
    assert "journalctl -u 'kdive-live-worker@*'" in run
    if job == "tcg":
        assert "--output=cat" in run
        assert "scripts/live-stack/filter-worker-journal-evidence.py" in run
        assert "--output=cat 2>/dev/null |" in run
    else:
        assert "--output=cat" not in run
        assert "scripts/live-stack/filter-worker-journal-evidence.py" not in run
    assert "--no-pager" in run
    assert "--since" in run
    assert (
        run.index("printf '::stop-commands::%s\\n'")
        < run.index("journalctl -u")
        < run.index("printf '::%s::\\n'")
    )
    assert "worker journal capture was unavailable or withheld" in run


def test_tcg_journal_capture_discards_untrusted_upstream_stderr(tmp_path: pathlib.Path) -> None:
    journalctl = tmp_path / "journalctl"
    journalctl.write_text(
        '#!/bin/sh\nprintf "journal failure at /sensitive/journal/path\\n" >&2\nexit 9\n',
        encoding="utf-8",
    )
    journalctl.chmod(0o755)
    sg = tmp_path / "sg"
    sg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    sg.chmod(0o755)
    _, diagnostic = _named_step("tcg", "Capture worker lifecycle diagnostics")

    result = subprocess.run(
        ["/bin/bash", "-c", diagnostic["run"]],
        cwd=_ROOT,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    assert "/sensitive/journal/path" not in result.stdout
    assert "/sensitive/journal/path" not in result.stderr
    assert result.stderr == "worker journal capture was unavailable or withheld\n"


@pytest.mark.parametrize("job", ("tcg", "native"))
def test_live_job_keeps_test_step_authoritative_before_diagnostics(job: str) -> None:
    """The tier's own proof decides the verdict; diagnostics only observe its wreckage."""
    steps = _load(_LIVE)["jobs"][job]["steps"]
    diagnostic_index, _ = _named_step(job, "Capture worker lifecycle diagnostics")
    cleanup_index, _ = _named_step(job, "Clean up live stack")
    proof = "-m live_vm_tcg" if job == "tcg" else 'pytest -m "live_vm and not live_vm_tcg"'
    test_index = next(index for index, step in enumerate(steps) if proof in step.get("run", ""))

    assert test_index < diagnostic_index < cleanup_index


def test_both_live_jobs_start_the_app_tier_with_up_sh() -> None:
    """One vehicle for both gates: the host-process path scripts/live-stack/up.sh owns.

    The native job has always used it. The tcg job briefly ran the app tier as compose services,
    which put the worker in a container with no libvirt and gave the server a different OIDC
    issuer identity than the host-side test mints against. Pinning both jobs to up.sh keeps the
    two gates from drifting onto different topologies again.
    """
    for job in ("tcg", "native"):
        assert "scripts/live-stack/up.sh" in _job_run_blocks(job), (
            f"the {job} job must start the app tier via up.sh (host processes), "
            "not as compose containers"
        )


def test_tcg_job_does_not_containerize_the_app_tier() -> None:
    """A `docker compose up` of server/worker/reconciler re-breaks the boot AND the auth."""
    for line in _tcg_spine().splitlines():
        stripped = line.strip()
        if not stripped.startswith("docker compose"):
            continue
        # `docker compose rm -sf` / `down` of the app tier is the CLEANUP direction and is fine;
        # only bringing the services UP puts the worker in a container.
        if "up" not in stripped.split():
            continue
        for service in _APP_TIER_SERVICES:
            assert service not in stripped.split(), (
                f"the tcg spine must not `docker compose up` {service}: the local-libvirt "
                "provider is not containerized (Dockerfile header) — the container has no "
                f"libvirt and no /dev/kvm. Offending line: {stripped!r}"
            )


def test_live_jobs_do_not_restore_the_retired_root_worker_mode() -> None:
    assert "KDIVE_WORKER_AS_ROOT" not in _job_run_blocks("tcg")
    assert "KDIVE_WORKER_AS_ROOT" not in _job_run_blocks("native")


def test_tcg_job_resolves_the_kernel_tree_before_the_app_tier_starts() -> None:
    """Ordering: restart_host_processes captures KDIVE_KERNEL_SRC when it forks the worker.

    up.sh's restart_host_processes reads KDIVE_KERNEL_SRC at fork time and defaults it to
    ${HOME}/src/linux, which does not exist on a hosted runner. Exporting the fetched tree after
    up.sh would leave the worker permanently pointed at that nonexistent path.
    """
    spine = _tcg_spine()
    assert spine.index("fetch-kernel-tree.sh") < spine.index("scripts/live-stack/up.sh"), (
        "KDIVE_KERNEL_SRC must be resolved before up.sh forks the worker, which captures it"
    )
    assert "fetch-kernel-tree.sh /var/lib/kdive/build/" in spine


def test_native_job_resolves_the_kernel_tree_before_the_app_tier_starts() -> None:
    native = _job_run_blocks("native")
    assert native.index("fetch-kernel-tree.sh") < native.index("scripts/live-stack/up.sh"), (
        "KDIVE_KERNEL_SRC must be resolved before native up.sh forks the fixed worker"
    )
    assert "fetch-kernel-tree.sh /var/lib/kdive/build/" in native


def test_hosted_lifecycle_proof_is_a_separate_no_skip_step_before_tcg() -> None:
    proof_index, proof = _named_step(
        "tcg", "Prove systemd worker lifecycle against disposable Postgres"
    )
    spine_index, _ = _named_step(
        "tcg", "Run the live_vm_tcg spine (stage -> up -> preflight -> test, one shell)"
    )
    install_index, _ = _named_step("tcg", "Install the fixed live-worker lifecycle host contract")
    run = proof["run"]

    assert install_index < proof_index < spine_index
    assert "if" not in proof
    assert "scripts/live-stack/up.sh --reset-db --skip-obs --skip-libvirt" in run
    assert "source scripts/live-stack/env.sh" in run
    assert "KDIVE_RUN_SYSTEMD_WORKER_PROOF=1" in run
    assert "tests/live_vm/test_systemd_worker_lifecycle.py" in run
    assert "-m live_vm --strict-markers -q" in " ".join(run.split())


def test_hosted_lifecycle_proof_uses_worker_accessible_absolute_kernel_source() -> None:
    _, proof = _named_step("tcg", "Prove systemd worker lifecycle against disposable Postgres")
    run = proof["run"]
    fetch = "scripts/fetch-kernel-tree.sh /var/lib/kdive/build/"
    assert fetch in run
    assert "export KDIVE_KERNEL_SRC" in run
    assert run.index(fetch) < run.index("scripts/live-stack/up.sh")


def test_hosted_lifecycle_proof_cleanup_preserves_failure_diagnostics() -> None:
    proof_index, _ = _named_step(
        "tcg", "Prove systemd worker lifecycle against disposable Postgres"
    )
    cleanup_index, cleanup = _named_step("tcg", "Clean up lifecycle proof stack")
    spine_index, _ = _named_step(
        "tcg", "Run the live_vm_tcg spine (stage -> up -> preflight -> test, one shell)"
    )
    diagnostic_index, _ = _named_step("tcg", "Capture worker lifecycle diagnostics")
    final_index, final = _named_step("tcg", "Clean up live stack")

    assert proof_index < cleanup_index < spine_index < diagnostic_index < final_index
    assert cleanup["if"] == "success()"
    assert "scripts/live-stack/down.sh" in cleanup["run"]
    assert final["if"] == "always()"


def test_hosted_lifecycle_proof_refreshes_control_and_libvirt_groups() -> None:
    _, proof = _named_step("tcg", "Prove systemd worker lifecycle against disposable Postgres")
    run = proof["run"]
    assert "sudo --preserve-env" in run
    assert '--user="$operator_name" --group=kdive-live-control' in " ".join(run.split())
    assert "id -G" in run
    assert "kdive-live-control" in run and "kdive-live-libvirt" in run


# --- hosted tcg pre-clean: stale /run/kdive/live-libvirt residue (#2033) ----------------------
#
# A reused hosted VM can carry an operator-owned session daemon plus socket/pid residue from an
# earlier run; a self-contradictory scene makes the installer exit 1 by design
# (_reconcile_libvirt_tuple). The hygiene belongs in the job, before the install step — never
# behind an installer recovery flag.

_PRECLEAN_STEP = "Pre-clean stale live-libvirt runtime residue"
_INSTALL_STEP = "Install the fixed live-worker lifecycle host contract"


def test_tcg_job_precleans_stale_runtime_before_install() -> None:
    """Ordering is the whole fix: the installer must reconcile a clean slate."""
    preclean_index, _ = _named_step("tcg", _PRECLEAN_STEP)
    install_index, _ = _named_step("tcg", _INSTALL_STEP)
    assert preclean_index < install_index


def test_preclean_is_hosted_tcg_only() -> None:
    """The native job's box is persistent and operator-managed; it must not gain this step."""
    assert _PRECLEAN_STEP not in [s.get("name") for s in _load(_LIVE)["jobs"]["native"]["steps"]]


def test_preclean_stops_the_recorded_session_daemon_as_its_owner() -> None:
    """The daemon is operator-owned: stop the recorded pid after identity checks, no sudo kill."""
    _, preclean = _named_step("tcg", _PRECLEAN_STEP)
    run = preclean["run"]
    assert "runtime_root=/run/kdive/live-libvirt" in run
    assert 'pid_file="$runtime_root/libvirt/libvirtd.pid"' in run
    assert '--user="$operator_name" --group=kdive-live-libvirt' in " ".join(run.split())
    # The signal is gated on the recorded process being the operator's own libvirtd...
    assert 'daemon_comm != "libvirtd"' in run
    # ...graceful first (SIGTERM with a bounded wait), escalating only on refusal.
    assert "kill -TERM" in run
    assert "kill -KILL" in run


def test_preclean_never_touches_state_roots_or_follows_symlinks() -> None:
    """Hygiene scope is the /run runtime hierarchy only; /var/lib/kdive stays untouched."""
    _, preclean = _named_step("tcg", _PRECLEAN_STEP)
    run = preclean["run"]
    assert "/var/lib/kdive" not in run
    assert "/run/kdive/live-libvirt" in run
    # A symlink at the hierarchy root is unlinked as a link (rm never traverses one), and the
    # fresh-host early exit treats a dangling link as residue rather than following it.
    assert "[[ ! -e $runtime_root && ! -L $runtime_root ]]" in run


# --- hosted tcg spine: fund, alias, and never green-light an empty tier (#2048) ---------------
#
# Run 32577345199 concluded SUCCESS without ever invoking pytest: the spine ended at up.sh's
# "next: fund a project" advisory and nothing failed on "proofs could not run". And once funded,
# the proof suite reads bare KDIVE_DATABASE_URL, which env.sh stopped exporting post-#2021 — so
# every ppc64le proof would silently SKIP and the tier would still exit green.


def test_hosted_spine_onboards_the_project_before_the_proofs() -> None:
    """The proofs need a funded project; funding must precede pytest, not follow it."""
    spine = _tcg_spine()
    assert spine.index("scripts/live-stack/onboard.sh") < spine.index("-m live_vm_tcg")


def test_hosted_spine_exports_a_minted_token_and_dies_when_the_mint_fails() -> None:
    """onboard.sh prints `export KDIVE_TOKEN=...` on success and only a WARN otherwise."""
    spine = _tcg_spine()
    assert '''eval "$(grep '^export KDIVE_TOKEN=' <<<"$onboard_wiring")"''' in spine
    assert "onboard.sh minted no KDIVE_TOKEN" in spine


def test_hosted_spine_aliases_the_bare_database_url_for_the_proof_suite() -> None:
    """The suite's preflights read bare KDIVE_DATABASE_URL, which env.sh no longer exports."""
    assert 'export KDIVE_DATABASE_URL="${KDIVE_SERVER_DATABASE_URL}"' in _tcg_spine()


def test_hosted_spine_runs_the_tcg_tier_directly_not_just_test_live_tcg() -> None:
    """`just test-live-tcg` tolerates exit 5 ("no tests collected") as a clean skip — exactly
    the silent-green hole: run pytest directly so the summary is inspectable in the spine."""
    assert "just test-live-tcg" not in _tcg_spine()
    assert "-m live_vm_tcg --strict-markers -q" in " ".join(_tcg_spine().split())


def test_hosted_spine_fails_loud_on_a_zero_proof_tier() -> None:
    """pytest exits 0 when every test skips; pin the '<N> passed' summary gate that makes an
    all-skip or zero-collect live_vm_tcg tier RED naming the tier instead of green."""
    spine = _tcg_spine()
    assert "[1-9][0-9]* passed" in spine
    assert "ran ZERO live_vm_tcg proofs" in spine


# --- spine stdin hygiene: materialize the script, never share bash's stdin (#2054) -----------
#
# A stdin-fed spine (`bash -s` over a heredoc, or GitHub piping the run block to `bash {0}`)
# is consumed incrementally: any child that drains stdin swallows the not-yet-read script
# bytes. Run 32589578907's tcg tier exited 0 right after up.sh's banner — a
# libvirt-provisioning child had drained the heredoc feeding `bash -s`, so onboard,
# preflight-tcg and pytest NEVER ran while the job read green.

_TCG_SPINE_FILE = "$RUNNER_TEMP/spine-tcg.sh"
_NATIVE_SPINE_FILE = "$RUNNER_TEMP/spine-native.sh"


def test_tcg_spine_is_executed_from_a_materialized_file() -> None:
    """The tcg spine must write its body to a file and execute that file — no `bash -s`."""
    spine = _tcg_spine()
    assert f"cat >\"{_TCG_SPINE_FILE}\" <<'KDIVE_LIVE_SPINE'" in spine
    assert f'/bin/bash -e -u -o pipefail "{_TCG_SPINE_FILE}"' in spine
    assert "bash -s" not in spine


def test_native_spine_is_executed_from_a_materialized_file() -> None:
    """Same hardening for the native family shell: no stdin-sharing with its children."""
    spine = _native_spine()
    assert f"cat >\"{_NATIVE_SPINE_FILE}\" <<'KDIVE_NATIVE_SPINE'" in spine
    assert f'/bin/bash -e -u -o pipefail "{_NATIVE_SPINE_FILE}"' in spine
    assert "bash -s" not in spine


@pytest.mark.parametrize(
    ("spine_of", "file", "delimiter"),
    (
        (_tcg_spine, "$RUNNER_TEMP/spine-tcg.sh", "KDIVE_LIVE_SPINE"),
        (_native_spine, "$RUNNER_TEMP/spine-native.sh", "KDIVE_NATIVE_SPINE"),
    ),
    ids=("tcg", "native"),
)
def test_spine_body_is_delimited_before_execution(spine_of, file: str, delimiter: str) -> None:
    """The heredoc must be closed before anything executes the materialized file."""
    spine = spine_of()
    closing = re.search(rf"^{delimiter}$", spine, flags=re.MULTILINE)
    assert closing is not None
    execute = f'/bin/bash -e -u -o pipefail "{file}"'
    assert closing.end() < spine.index(execute)
