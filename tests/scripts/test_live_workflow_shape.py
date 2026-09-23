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
import textwrap

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


def test_native_spine_checks_lifecycle_compatibility_before_destructive_setup() -> None:
    """A stale persistent host must fail before the reaper or stack can mutate it (#2548)."""
    spine = _native_spine()
    compatibility = "scripts/live-stack/worker-lifecycle.sh compatibility"

    assert spine.count(compatibility) == 1
    assert spine.index(compatibility) < spine.index(
        'for uri in "$KDIVE_LIBVIRT_URI" qemu:///system'
    )
    assert spine.index(compatibility) < spine.index("docker compose down -v")
    assert spine.index(compatibility) < spine.index("live-stack/stack-services.sh --skip-obs")

    native_steps = _load(_LIVE)["jobs"]["native"]["steps"]
    cleanup = next(step for step in native_steps if step.get("name") == "Clean up live stack")
    assert cleanup["if"] == "always() && steps.native-spine.outputs.cleanup_required == 'true'"
    cleanup_marker = 'echo "cleanup_required=true" >> "$GITHUB_OUTPUT"'
    assert cleanup_marker in spine
    assert spine.index(compatibility) < spine.index(cleanup_marker)
    assert spine.index(cleanup_marker) < spine.index(
        'for uri in "$KDIVE_LIBVIRT_URI" qemu:///system'
    )


def _native_guest_image() -> str:
    prefix = "export KDIVE_GUEST_IMAGE="
    found = [ln.strip() for ln in _native_spine().splitlines() if ln.strip().startswith(prefix)]
    assert len(found) == 1, (
        f"expected exactly one `{prefix}` line in the native spine, found {len(found)}; "
        "without it the console-part proof fails at its guest-image gate (#2518)"
    )
    return found[0][len(prefix) :].split(" #")[0].strip().strip("\"'")


def test_native_guest_image_names_the_rootfs_mint_system_stages() -> None:
    """The native spine's KDIVE_GUEST_IMAGE must name the file mint-system.sh actually stages.

    mint-system.sh hardlinks the warm-store rootfs into the provider's allowed root under a fixed
    basename, and live.yml repeats that basename to point the console-part proof at it (#2518).
    Nothing else ties the two. Since #2518 an unresolvable KDIVE_GUEST_IMAGE *fails* that proof
    instead of skipping it, so a rename in the script alone turns the native job red rather than
    green — parse the basename from the script rather than repeating the literal a third time.
    """
    mint = (_ROOT / "scripts" / "live-vm" / "mint-system.sh").read_text(encoding="utf-8")

    def _one(prefix: str, text: str, where: str) -> str:
        # Tolerate a `readonly`/`declare`/`local` qualifier: adding one is a benign edit that
        # must not read as a rename.
        stripped = (
            re.sub(r"^(readonly|declare|local)\s+", "", ln.strip()) for ln in text.splitlines()
        )
        found = [ln for ln in stripped if ln.startswith(prefix)]
        assert len(found) == 1, f"expected exactly one `{prefix}` line in {where}, got {len(found)}"
        return found[0][len(prefix) :].split(" #")[0].strip().strip("\"'")

    # Compare the two shell sources to EACH OTHER, never to a resolved constant: ROOTFS_DIR is
    # `config.require(LIBVIRT_ROOTFS_ROOT)`, so asserting against it would make this test's verdict
    # depend on the runner's own environment and go red on an untouched workflow.
    rootfs_dir = _one("rootfs_dir=", mint, "mint-system.sh")
    staged = _one("staged_rootfs=", mint, "mint-system.sh")
    assert staged.startswith("${rootfs_dir}/"), (
        f"mint-system.sh stages {staged}, no longer under its own ${{rootfs_dir}}; the workflow "
        "export below cannot mirror a path this test can no longer derive"
    )
    expected = staged.replace("${rootfs_dir}", rootfs_dir, 1)

    exported = _native_guest_image()
    assert exported == expected, (
        f"live.yml exports KDIVE_GUEST_IMAGE={exported}, but mint-system.sh stages {expected}. "
        "Renaming one side, or factoring either literal into a variable, fails the native "
        "live_vm job at its guest-image gate — keep the two in step."
    )


def test_native_spine_aliases_the_bare_database_url_for_the_proof_suite() -> None:
    """The proofs read bare KDIVE_DATABASE_URL, which env.sh deliberately does not export.

    One DSN per authority since #1929, so the alias belongs in the spine, not in env.sh and not in
    preflight-env.sh. The tcg spine has carried it since #2046; the native spine did not, so the
    console-part proof skipped on the database gate even once its guest image was wired (#2518).
    """
    assert 'export KDIVE_DATABASE_URL="${KDIVE_SERVER_DATABASE_URL}"' in _native_spine()


_ALLOCATION_CAP_EXPORT = "export KDIVE_LIBVIRT_ALLOCATION_CAP="
# What the native tier holds against the local-libvirt host at once (#2560):
#   1  mint-system.sh's allocation, held for the whole job
# + 1  the one live_vm proof that requests an allocation of its own
_MINT_ALLOCATIONS = 1
_SUITE_ALLOCATIONS = 1


def test_native_spine_raises_the_allocation_cap_above_the_long_lived_mint() -> None:
    """The native tier mints a System that holds an allocation for the whole job (#2560).

    `LocalLibvirtDiscovery.from_env` defaults `KDIVE_LIBVIRT_ALLOCATION_CAP` to 1, so discovery
    advertised `concurrent_allocation_cap: 1` and the mint took the only slot. Every later
    `allocations.request` was then denied `at_capacity` mid-suite — which is what the console-part
    proof hit once #2518 let it execute on this tier for the first time.

    The value is the tier's actual concurrency, not the smallest number that unblocks one test:
    the long-lived mint (2 vcpu / 4 GB) plus the single `live_vm` proof that allocates for itself
    (2 vcpu / 2 GB), run serially because the spine passes no `-n`. The 4 vcpu / 6 GB that admits
    sits well inside the 8 vcpus / ~31 GB the host advertised. No per-project quota bounds it:
    the cap is counted per resource across projects, and these two allocations are funded in
    different projects (`demo` for the mint, `console-parts-proof` for the proof).
    """
    spine = _native_spine()
    lines = [
        ln.strip() for ln in spine.splitlines() if ln.strip().startswith(_ALLOCATION_CAP_EXPORT)
    ]
    assert len(lines) == 1, (
        f"expected exactly one `{_ALLOCATION_CAP_EXPORT}` line in the native spine, "
        f"found {len(lines)}; without it the tier runs at the fail-closed default of 1 and the "
        "minted System holds the only slot (#2560)"
    )
    cap = int(lines[0][len(_ALLOCATION_CAP_EXPORT) :].split(" #")[0].strip().strip("\"'"))
    assert cap == _MINT_ALLOCATIONS + _SUITE_ALLOCATIONS, (
        f"the native spine caps concurrent allocations at {cap}, but the tier holds "
        f"{_MINT_ALLOCATIONS} (mint) + {_SUITE_ALLOCATIONS} (suite) at once; a new proof that "
        "allocates for itself raises the second term rather than leaving the tier to fail "
        "`at_capacity` mid-suite"
    )

    # Anchor on stack-services.sh, NOT mint-system.sh. Per ADR-0384 the cap is operator-owned:
    # discovery honors this variable when it INSERTS the resources row and preserves the stored
    # value on every refresh. The reconciler stack-services.sh starts registers discovery at
    # startup, so it inserts first — an export placed after stack-services.sh but before the mint
    # reads as correct and silently no-ops, which is how #2560 would come back.
    assert spine.index(_ALLOCATION_CAP_EXPORT) < spine.index("live-stack/stack-services.sh"), (
        "KDIVE_LIBVIRT_ALLOCATION_CAP is exported after stack-services.sh; the reconciler it "
        "starts has already inserted the resource at the old cap, and ADR-0384 keeps that stored "
        "value through every later refresh"
    )


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
    Compare against ``LIBVIRT_ROOTFS_ROOT.default``, not the runtime-resolved ``ROOTFS_DIR``:
    the latter is ``config.require(LIBVIRT_ROOTFS_ROOT)``, so it follows whatever the developer
    running this test has set, while live.yml's literal is the setting's default (#2549).
    """
    from kdive.providers.local_libvirt.settings import LIBVIRT_ROOTFS_ROOT

    rootfs_dir = LIBVIRT_ROOTFS_ROOT.default
    stage = _tcg_stage_dir()
    assert stage.startswith(f"{rootfs_dir}/"), (
        f"KDIVE_TCG_STAGE_DIR={stage} is outside the provider's allowed root "
        f"{rootfs_dir}; provision would reject the staged rootfs"
    )
    # A SUBDIR, never the root itself: stage-tcg-images.sh rm -rf's + recreates its stage dir, and
    # the provider writes every per-System overlay into the root alongside it.
    assert stage.rstrip("/") != rootfs_dir, (
        "stage into a subdirectory: the stager deletes and recreates KDIVE_TCG_STAGE_DIR, "
        "which would take the provider's overlay dir with it"
    )


def test_tcg_allowed_root_is_backed_by_the_large_scratch_disk() -> None:
    """~7 GB of staged set (#1292) must not land on the runner's small root filesystem.

    The provider's allowed root is a fixed path, so the only way to keep the budget on /mnt is to
    back that path with the scratch disk. validate_local_component_path resolves both the candidate
    and the roots, so a symlinked root still matches; `df` follows it too, which keeps
    stage-tcg-images.sh's pre-stage free-space check measuring the disk the bytes actually land on.
    Compare against ``LIBVIRT_ROOTFS_ROOT.default``, not the runtime-resolved ``ROOTFS_DIR``: see
    ``test_tcg_block_stages_inside_the_provider_allowed_root`` above (#2549).
    """
    from kdive.providers.local_libvirt.settings import LIBVIRT_ROOTFS_ROOT

    rootfs_dir = LIBVIRT_ROOTFS_ROOT.default
    steps = _load(_LIVE)["jobs"]["tcg"]["steps"]
    joined = "\n".join(s["run"] for s in steps if "run" in s)
    link = next(
        (ln.strip() for ln in joined.splitlines() if "ln -" in ln and rootfs_dir in ln),
        None,
    )
    assert link is not None, (
        f"{rootfs_dir} must be backed by the /mnt scratch disk, not the root filesystem"
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
    # The published path is the parser's default, not a literal the job supplies. `readonly` gave
    # way to `:=` in #2480 so the file survives the second source per shell that lib.sh, env.sh and
    # worker-lifecycle.sh together make ordinary; the default itself is what this pins.
    assert ': "${LIBVIRT_ENV:=/etc/kdive/live-worker-libvirt.env}"' in parser
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


def test_native_job_never_installs_privileged_lifecycle_contract() -> None:
    """ADR-0582: a workflow-selected checkout must never gain root installation authority."""
    steps = _load(_LIVE)["jobs"]["native"]["steps"]
    joined = "\n".join(step.get("run", "") for step in steps)

    assert "install-live-worker-lifecycle.sh" not in joined
    assert "sudo" not in joined


def test_native_worker_readiness_gates_system_mint() -> None:
    """The actual fixed worker must be ready before the expensive mint poll begins."""
    spine = _native_spine()
    stack = spine.index("scripts/live-stack/stack-services.sh --skip-obs")
    readiness = spine.index("http://127.0.0.1:9465/readyz")
    mint = spine.index('KDIVE_LIVE_VM_SYSTEM_ID="$(scripts/live-vm/mint-system.sh)"')
    assert stack < readiness < mint
    assert "filter-worker-readiness-evidence.py" in spine[readiness:mint]
    assert '[[ "$readiness_line" == "worker_readiness ready=true "* ]]' in spine[readiness:mint]
    assert "for attempt in {1..10}" in spine[stack:mint]
    assert "--max-time 4" in spine[stack:readiness]
    assert "sleep 2" in spine[readiness:mint]
    assert "capture_manifest_probe_identity=operator" not in spine


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
    assert (
        "capture_manifest_destination_ancestor component=usr_share uid=%u gid=%g mode=%a"
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


def test_native_job_captures_exact_provision_boundary_before_cleanup() -> None:
    spine_index, spine_step = _named_step(
        "native",
        "Run both native families (reaper -> stack -> mint -> preflight -> test, one shell)",
    )
    evidence_index, evidence = _named_step("native", "Capture persisted provision boundary")
    readiness_index, readiness = _named_step("native", "Capture worker readiness components")
    cleanup_index, _ = _named_step("native", "Clean up live stack")
    target = "$RUNNER_TEMP/kdive-provision-evidence.target"

    assert f'export KDIVE_PROVISION_EVIDENCE_TARGET="{target}"' in spine_step["run"]
    assert evidence["if"] == "always()"
    assert target in evidence["run"]
    assert "timeout --signal=TERM --kill-after=2s 12s" in evidence["run"]
    assert "scripts/live-stack/provision-queue-diagnostics.sh" in evidence["run"]
    assert readiness["if"] == "always()"
    assert "filter-worker-readiness-evidence.py" in readiness["run"]
    assert spine_index < evidence_index < readiness_index < cleanup_index


def test_native_mint_timeout_does_not_log_system_identifier() -> None:
    mint = (_ROOT / "scripts" / "live-vm" / "mint-system.sh").read_text(encoding="utf-8")
    assert 'print("System did not reach ready in time", file=sys.stderr)' in mint


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


def test_tcg_readiness_capture_discards_pipeline_stderr(tmp_path: pathlib.Path) -> None:
    curl = tmp_path / "curl"
    curl.write_text(
        '#!/bin/sh\nprintf "curl failed at /sensitive/readiness/path\\n" >&2\nexit 9\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    _, readiness = _named_step("tcg", "Capture worker readiness components")

    result = subprocess.run(
        ["/bin/bash", "-c", readiness["run"]],
        cwd=_ROOT,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 0
    assert "/sensitive/readiness/path" not in result.stdout
    assert "/sensitive/readiness/path" not in result.stderr
    assert result.stderr == "worker readiness evidence unavailable\n"


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
    ("job", "condition", "cleanup_condition"),
    (
        ("tcg", "always()", "always()"),
        (
            "native",
            "failure() || cancelled()",
            "always() && steps.native-spine.outputs.cleanup_required == 'true'",
        ),
    ),
)
def test_live_job_captures_lifecycle_diagnostics_before_cleanup(
    job: str, condition: str, cleanup_condition: str
) -> None:
    """Diagnostics are observational and must run before destructive teardown (#1939).

    The diagnostics step never fails the job (`exit 0`), neutralizes workflow-command
    injection from journal text (::stop-commands:: token), and degrades to a warning when
    the witness withholds evidence. Once cleanup is armed, diagnostics must have their chance
    first — after teardown there is nothing left to read.
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
    assert cleanup["if"] == cleanup_condition
    assert "scripts/live-stack/stack-down.sh" in cleanup["run"]
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
    if job == "tcg":
        assert "journalctl -u kdive-live-worker@1.service" in run
        assert "--output=cat" in run
        assert "scripts/live-stack/filter-worker-journal-evidence.py" in run
        assert "--output=cat 2>/dev/null |" in run
        assert "sudo --non-interactive journalctl" in run
    else:
        assert "journalctl -u 'kdive-live-worker@*'" in run
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


def test_tcg_journal_producer_preselects_only_fixed_evidence_families() -> None:
    _, diagnostic = _named_step("tcg", "Capture worker lifecycle diagnostics")
    run = diagnostic["run"]
    match = re.search(r"--grep='([^']+)'", run)
    assert match is not None
    producer_filter = re.compile(match.group(1))
    worker = "local-systemd:kdive-live-worker@1.service:" + "0" * 32
    safe_messages = (
        f'{{"msg": "worker {worker} accepting dispatch lanes: default"}}',
        (
            f'{{"msg": "worker {worker} claimed provision job '
            '11111111-1111-1111-1111-111111111111"}'
        ),
        ('{"msg": "local-libvirt provision system=22222222-2222-2222-2222-222222222222"}'),
        (f'{{"msg": "worker {worker} claim loop failure lane=default reason=timeout"}}'),
    )
    raw_messages = (
        '{"msg": "run_once failed on lane default; continuing after 1.0s", '
        '"exc": "Traceback: /private/path"}',
        '{"msg": "unrelated", "exc": "worker claim loop failure lane=default"}',
        (f'{{"msg": "worker {worker} CLAIM LOOP FAILURE lane=default reason=timeout"}}'),
    )

    assert "--case-sensitive=yes" in run
    assert all(producer_filter.search(message) for message in safe_messages)
    assert not any(producer_filter.search(message) for message in raw_messages)
    assert run.index("--grep=") < run.index("filter-worker-journal-evidence.py")


def test_tcg_journal_capture_discards_untrusted_upstream_stderr(tmp_path: pathlib.Path) -> None:
    journalctl = tmp_path / "journalctl"
    journalctl.write_text(
        '#!/bin/sh\nprintf "journal failure at /sensitive/journal/path\\n" >&2\nexit 9\n',
        encoding="utf-8",
    )
    journalctl.chmod(0o755)
    sudo = tmp_path / "sudo"
    sudo.write_text(
        '#!/bin/sh\n[ "$1" = "--non-interactive" ] && shift\nexec "$@"\n',
        encoding="utf-8",
    )
    sudo.chmod(0o755)
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


def test_tcg_journal_capture_bounds_a_hanging_pipeline(tmp_path: pathlib.Path) -> None:
    journalctl = tmp_path / "journalctl"
    journalctl.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    journalctl.chmod(0o755)
    sudo = tmp_path / "sudo"
    sudo.write_text(
        '#!/bin/sh\n[ "$1" = "--non-interactive" ] && shift\nexec "$@"\n',
        encoding="utf-8",
    )
    sudo.chmod(0o755)
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
        timeout=12,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("::stop-commands::kdive-")
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
    """One vehicle for both gates: the host-process path scripts/live-stack/stack-services.sh owns.

    The native job has always used it. The tcg job briefly ran the app tier as compose services,
    which put the worker in a container with no libvirt and gave the server a different OIDC
    issuer identity than the host-side test mints against. Pinning both jobs to
    stack-services.sh keeps the two gates from drifting onto different topologies again.
    """
    for job in ("tcg", "native"):
        assert "scripts/live-stack/stack-services.sh" in _job_run_blocks(job), (
            f"the {job} job must start the app tier via stack-services.sh (host processes), "
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

    stack-services.sh's restart_host_processes reads KDIVE_KERNEL_SRC at fork time and defaults
    it to ${HOME}/src/linux, which does not exist on a hosted runner. Exporting the fetched tree
    after
    stack-services.sh would leave the worker permanently pointed at that nonexistent path.
    """
    spine = _tcg_spine()
    assert spine.index("fetch-kernel-tree.sh") < spine.index(
        "scripts/live-stack/stack-services.sh"
    ), (
        "KDIVE_KERNEL_SRC must be resolved before stack-services.sh forks the worker, which "
        "captures it"
    )
    assert "fetch-kernel-tree.sh /var/lib/kdive/build/" in spine


def test_native_job_resolves_the_kernel_tree_before_the_app_tier_starts() -> None:
    native = _job_run_blocks("native")
    assert native.index("fetch-kernel-tree.sh") < native.index(
        "scripts/live-stack/stack-services.sh"
    ), "KDIVE_KERNEL_SRC must be resolved before native stack-services.sh forks the fixed worker"
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
    assert "scripts/live-stack/stack-services.sh --reset-db --skip-obs --skip-libvirt" in run
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
    assert run.index(fetch) < run.index("scripts/live-stack/stack-services.sh")


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
    assert "scripts/live-stack/stack-down.sh" in cleanup["run"]
    assert final["if"] == "always()"


def test_hosted_lifecycle_proof_refreshes_control_and_libvirt_groups() -> None:
    _, proof = _named_step("tcg", "Prove systemd worker lifecycle against disposable Postgres")
    run = proof["run"]
    assert "sudo --preserve-env" in run
    assert '--user="$operator_name" --group=kdive-live-control' in " ".join(run.split())
    assert "id -G" in run
    assert "kdive-live-control" in run and "kdive-live-libvirt" in run


_SYSTEMD_PROOF_FILE = "$RUNNER_TEMP/systemd-worker-proof.sh"


def test_hosted_lifecycle_proof_is_executed_from_a_materialized_file() -> None:
    """A child cannot drain proof commands that Bash reads from a regular file (#2565)."""
    _, proof = _named_step("tcg", "Prove systemd worker lifecycle against disposable Postgres")
    run = proof["run"]
    delimiter = "KDIVE_" + "SYSTEMD_PROOF"  # Keep the env-name guard from parsing test data.
    assert f"cat >\"{_SYSTEMD_PROOF_FILE}\" <<'{delimiter}'" in run
    assert f'/bin/bash -e -u -o pipefail "{_SYSTEMD_PROOF_FILE}"' in run
    assert "bash -s" not in run

    closing = re.search(rf"^{delimiter}$", run, flags=re.MULTILINE)
    assert closing is not None
    execute = f'/bin/bash -e -u -o pipefail "{_SYSTEMD_PROOF_FILE}"'
    assert closing.end() < run.index(execute)


def test_hosted_lifecycle_proof_captures_and_checks_its_pytest_summary() -> None:
    """The proof preserves pytest's status and rejects a successful run with no passes."""
    _, proof = _named_step("tcg", "Prove systemd worker lifecycle against disposable Postgres")
    run = " ".join(proof["run"].replace("\\\n", " ").split())
    assert '-m live_vm --strict-markers -q | tee "$systemd_summary" || rc=$?' in run
    assert 'pytest-terminal-summary-has-passes.sh "$systemd_summary"' in run
    assert "ran ZERO systemd worker lifecycle proofs" in run
    assert 'exit "$rc"' in run


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
# Run 32577345199 concluded SUCCESS without ever invoking pytest: the spine ended at
# stack-services.sh's "next: fund a project" advisory and nothing failed on "proofs could not
# run". And once funded,
# the proof suite reads bare KDIVE_DATABASE_URL, which env.sh stopped exporting post-#2021 — so
# every ppc64le proof would silently SKIP and the tier would still exit green.


def test_hosted_spine_onboards_the_project_before_the_proofs() -> None:
    """A preflight failure must exit before the normal hosted proof path starts."""
    spine = _tcg_spine()
    onboarding = 'onboard_wiring="$(ONBOARD_PREFLIGHT=required scripts/live-stack/onboard.sh)"'
    assert onboarding in spine
    assert "set -euo pipefail" in spine
    assert spine.index(onboarding) < spine.index("if ! grep -q '^export KDIVE_TOKEN='")
    assert spine.index(onboarding) < spine.index("-m live_vm_tcg")


def test_hosted_spine_exports_a_minted_token_and_dies_when_the_mint_fails() -> None:
    """onboard.sh prints `export KDIVE_TOKEN=...` on success and only a WARN otherwise."""
    spine = _tcg_spine()
    assert '''eval "$(grep '^export KDIVE_TOKEN=' <<<"$onboard_wiring")"''' in spine
    assert "onboard.sh minted no KDIVE_TOKEN" in spine


def test_hosted_spine_aliases_the_bare_database_url_for_the_proof_suite() -> None:
    """The suite's preflights read bare KDIVE_DATABASE_URL, which env.sh no longer exports."""
    assert 'export KDIVE_DATABASE_URL="${KDIVE_SERVER_DATABASE_URL}"' in _tcg_spine()


def test_hosted_spine_runs_the_tcg_tier_directly_not_just_test_live_tcg() -> None:
    """The spine runs pytest directly so it owns the summary it inspects. The recipe now carries
    an equivalent zero-proof gate (#2517), but the spine's own env wiring, `tee` capture and
    error text live here and must stay readable in the job log, not one `just` level away."""
    assert "just test-live-tcg" not in _tcg_spine()
    assert "-m live_vm_tcg --strict-markers -q" in " ".join(_tcg_spine().split())


def test_hosted_spine_fails_loud_on_a_zero_proof_tier() -> None:
    """pytest exits 0 when every test skips; pin the '<N> passed' summary gate that makes an
    all-skip or zero-collect live_vm_tcg tier RED naming the tier instead of green."""
    spine = _tcg_spine()
    assert 'pytest-terminal-summary-has-passes.sh "$tcg_summary"' in spine
    assert "ran ZERO live_vm_tcg proofs" in spine


def _proof_guard(spine: str, summary_var: str) -> str:
    lines = spine.splitlines()
    call = f'if ! scripts/pytest-terminal-summary-has-passes.sh "${summary_var}"; then'
    start = next(i for i, line in enumerate(lines) if line.strip() == call)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "fi")
    return textwrap.dedent("\n".join(lines[start : end + 1]))


@pytest.mark.parametrize(
    ("proof_name", "summary_var"),
    [
        ("tcg", "tcg_summary"),
        ("native", "native_summary"),
        ("systemd", "systemd_summary"),
    ],
)
@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ("SKIPPED [1] test.py: previous run had 1 passed\n4 skipped in 0.01s\n", False),
        ("1 passed in 0.01s\n", True),
    ],
)
def test_workflow_proof_guards_execute_the_shared_predicate(
    tmp_path: pathlib.Path,
    proof_name: str,
    summary_var: str,
    stream: str,
    expected: bool,
) -> None:
    summary = tmp_path / f"{proof_name}.summary"
    summary.write_text(stream, encoding="utf-8")
    if proof_name == "tcg":
        proof = _tcg_spine()
    elif proof_name == "native":
        proof = _native_spine()
    else:
        _, step = _named_step("tcg", "Prove systemd worker lifecycle against disposable Postgres")
        proof = step["run"]
    shell = f'{summary_var}="$1"\nrc=0\n{_proof_guard(proof, summary_var)}\n'
    result = subprocess.run(
        ["/bin/bash", "-e", "-u", "-o", "pipefail", "-c", shell, "proof-guard", str(summary)],
        cwd=_ROOT,
        check=False,
    )
    assert (result.returncode == 0) is expected


def test_native_spine_fails_loud_on_a_zero_proof_tier() -> None:
    """The same gate on the native tier, which shipped without one (#2540).

    `pytest -m "live_vm and not live_vm_tcg"` exits 0 when every proof skips and 5 when none is
    collected, so neither code separates "the tier passed" from "the tier never ran". ADR-0389
    exists to kill exactly that green: the native family is its decision point 2.

    Pin the *whole* guard, not just fragments of it. A deleted guard is the obvious regression;
    the likelier one is a guard still present but defanged — `if !` dropped to `if`, or `exit 1`
    softened to `exit 0` — either of which leaves every individual substring in place while
    inverting what the gate does. Matching the block as one string catches both.
    """
    spine = " ".join(_native_spine().split())
    guard = (
        'if ! scripts/pytest-terminal-summary-has-passes.sh "$native_summary"; then '
        'echo "native live_vm spine: ran ZERO native live_vm proofs '
        "(no '<N> passed' summary, pytest rc=$rc); "
        'a skipped tier must never read green" >&2 '
        "exit 1 "
        "fi"
    )
    assert guard in spine, "the native spine's zero-proof gate is missing, inverted, or defanged"


def test_native_spine_captures_the_summary_it_greps_under_pipefail() -> None:
    """The gate reads a `tee`-captured summary, and the capture must not eat pytest's status.

    Without `pipefail` the pipeline would report `tee`'s exit code, so a genuinely failing proof
    run would satisfy the `<N> passed` gate on its partial summary and then exit 0 — trading one
    silent green for another. `pipefail` itself is pinned by
    `test_native_spine_is_executed_from_a_materialized_file`, which asserts the
    `bin/bash -e -u -o pipefail` exec line; what this pins is the capture-and-propagate shape
    that depends on it, including the `|| rc=$?` that keeps pytest's status alive under `-e`.
    """
    # Join the shell line continuation before normalizing, or the trailing `\` survives as its
    # own token and the pipeline reads as `... "$native_summary" \ || rc=$?`.
    spine = " ".join(_native_spine().replace("\\\n", " ").split())
    assert '-m "live_vm and not live_vm_tcg" -q | tee "$native_summary" || rc=$?' in spine
    assert 'exit "$rc"' in spine


# --- spine stdin hygiene: materialize the script, never share bash's stdin (#2054) -----------
#
# A stdin-fed spine (`bash -s` over a heredoc, or GitHub piping the run block to `bash {0}`)
# is consumed incrementally: any child that drains stdin swallows the not-yet-read script
# bytes. Run 32589578907's tcg tier exited 0 right after stack-services.sh's banner — a
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


# The regex that generated the rename's file map, so the guard's reach equals the problem's:
# path-qualified names AND bare basenames. A literal list would miss the latter.
#
# The negative lookbehind is load-bearing. Every replacement name embeds its predecessor
# (`stack-down.sh`, `demo-up.sh`), and `-` is a word boundary, so a plain `\bdown\.sh\b` matches
# inside the new name and the guard can never go green.
#
# It excludes `-` and nothing else. Adding `/` would exclude `scripts/live-stack/up.sh` — the
# path-qualified form Success criterion 2 enumerates and the one a dangling CI call site takes.
_OLD_ENTRY_POINT_RE = re.compile(r"(?<![-\w])(?:up|down|status)\.sh\b|(?<![-\w])stack-up\b")
# This module necessarily contains the pattern it searches for, so it excludes itself.
_SELF = "tests/scripts/test_live_workflow_shape.py"
# Append-only records (the `records` gate) and point-in-time records keep citing the old names
# on purpose: ADR-0655 is where a reader learns the current ones.
_RECORD_ROOTS = (
    "docs/adr/",
    "docs/debt/",
    "docs/archive/",
    "docs/superpowers/",
    "docs/design/",
    "docs/workflow/",
)


def _tracked_live_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return [p for p in out if p not in ("CHANGELOG.md", _SELF) and not p.startswith(_RECORD_ROOTS)]


def test_no_live_file_names_a_renamed_entry_point() -> None:
    offenders: list[str] = []
    for path in _tracked_live_files():
        full = _ROOT / path
        if not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _OLD_ENTRY_POINT_RE.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, "renamed entry points still referenced:\n" + "\n".join(offenders)


def test_workflow_script_paths_exist() -> None:
    workflow = _LIVE.read_text(encoding="utf-8")
    for match in re.findall(r"scripts/live-stack/[\w.-]+\.sh", workflow):
        assert (_ROOT / match).is_file(), f"live.yml names a missing script: {match}"
