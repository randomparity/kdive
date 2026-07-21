"""Curated catalog of non-registry ``KDIVE_*`` environment variables.

The 48 runtime settings the processes read flow through the config registry
(:func:`kdive.config.all_settings`) and are auto-documented by
``scripts/gen_config_reference.py``. A second class of ``KDIVE_*`` variables is read **outside**
the registry — by the gated test suites, the operator setup/live-stack shell scripts, the in-guest
capture/install helpers, and the image/wheel build. Those cannot go through ``kdive.config`` (a
bash helper has no Python import; a build arg is not a process setting), so they are catalogued
here by hand.

This module is the single source of truth for that second class. The config-reference generator
renders it into a second section of ``docs/guide/reference/config.md``, and
``scripts/check_env_documented.py`` treats every name here as documented — so a new non-registry
variable fails CI until it is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EnvScope = Literal["test", "script", "guest", "build"]


@dataclass(frozen=True, slots=True)
class ExternalEnvVar:
    """A ``KDIVE_*`` variable read outside the config registry.

    Attributes:
        name: The environment variable name (``KDIVE_...``).
        scope: Where it is read — ``test`` (gated suites), ``script`` (operator shell scripts),
            ``guest`` (in-guest capture/install helpers), or ``build`` (image/wheel provenance
            inputs consumed at build time).
        default: The fallback when unset, or ``None`` when unset means "skip / required".
        help: One line describing what reads it and what it controls.
    """

    name: str
    scope: EnvScope
    default: str | None
    help: str


EXTERNAL_ENV_VARS: tuple[ExternalEnvVar, ...] = (
    # --- test-only (gated suites) ---------------------------------------------------------
    ExternalEnvVar(
        "KDIVE_GUEST_IMAGE",
        "test",
        None,
        "Path to the operator-built local-libvirt guest rootfs qcow2 the live_stack spine boots; "
        "unset → the live_stack suite skips.",
    ),
    ExternalEnvVar(
        "KDIVE_GUEST_IMAGE_DEBIAN",
        "test",
        None,
        "Path to a debian-family *-kdive-ready qcow2 for the per-family SSH-reachability "
        "live_stack test (#956, ADR-0294); unset → the debian parameter of that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_GUEST_IMAGE_RHEL",
        "test",
        None,
        "Path to a rhel-family (rocky/centos/fedora) *-kdive-ready qcow2 for the per-family "
        "SSH-reachability live_stack test (#956, ADR-0294); unset → the rhel parameter skips.",
    ),
    ExternalEnvVar(
        "KDIVE_GUEST_IMAGE_PPC64LE",
        "test",
        None,
        "Path to a Fedora ppc64le kdive-ready qcow2 for the live TCG boot proof live_stack test "
        "(#1144, epic #1139); unset (or no qemu-system-ppc64) → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_PPC64LE_BUNDLE",
        "test",
        None,
        "Directory holding kernel.tar.gz (the ADR-0343 combined tar: ELF boot/vmlinuz + "
        "lib/modules/<ver>/) and initrd.img for the #1146 uploaded-bundle boot proof live_stack "
        "test (epic #1139); unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_PPC64LE_VMCORE",
        "test",
        None,
        "Path to the retained real #1148 ppc64le vmcore for the live_vm drgn-open proof (#1150, "
        "ADR-0348, epic #1139); unset → that test skips (a set-but-missing/mismatched path fails).",
    ),
    ExternalEnvVar(
        "KDIVE_TEST_BUILD_CONFIG",
        "test",
        None,
        "Path or file:// URL to a kernel .config (kdump + debuginfo) for the live_vm real-make "
        "build-id test; unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_TEST_PG_URL",
        "test",
        None,
        "Postgres server URL (with credentials) the db-test fixtures reuse instead of starting a "
        "per-run container (ADR-0400); unset → one shared testcontainer is started per run. Each "
        "worker creates its own kdive_test_<worker>_<token> database on it.",
    ),
    ExternalEnvVar(
        "KDIVE_TEST_S3_URL",
        "test",
        None,
        "MinIO/S3 endpoint the store-test fixtures reuse instead of starting a per-run container "
        "(ADR-0400); unset → one shared testcontainer is started per run. Each worker creates its "
        "own kdive-test-<worker>-<token> bucket on it.",
    ),
    ExternalEnvVar(
        "KDIVE_TEST_S3_ACCESS_KEY",
        "test",
        "minioadmin",
        "Access key for the KDIVE_TEST_S3_URL override MinIO/S3 (ADR-0400); defaults to the "
        "just compose-up minioadmin root.",
    ),
    ExternalEnvVar(
        "KDIVE_TEST_S3_SECRET_KEY",
        "test",
        "minioadmin",  # pragma: allowlist secret - local dev default
        "Secret key for the KDIVE_TEST_S3_URL override MinIO/S3 (ADR-0400); defaults to the "
        "just compose-up minioadmin root.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_SSH_TARGET",
        "test",
        None,
        "SSH target gating the criterion-5 live_stack tier; unset → the live_stack suite skips.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_VM_SYSTEM_ID",
        "test",
        None,
        "System id of a pre-provisioned live VM for the gated local-libvirt install test.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_VM_BZIMAGE",
        "test",
        None,
        "Path to a kernel image that panics early in boot (no usable rootfs) for the gated "
        "local-libvirt preserve-crash live-attach test (#747); unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_VM_ROOTFS",
        "test",
        None,
        "Path to a bootable rootfs qcow2 for the gated live_vm snapshot/revert/resume proof "
        "(#1254); unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_VM_VMCORE",
        "test",
        None,
        "Path to a real captured vmcore for the live_vm crash(8) postmortem test (#816); "
        "paired with KDIVE_LIVE_VM_VMLINUX. Unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_VM_VMLINUX",
        "test",
        None,
        "Path to the vmlinux debuginfo matching KDIVE_LIVE_VM_VMCORE for the live_vm crash(8) "
        "postmortem test (#816). Unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_VM_GDBMI_VMLINUX",
        "test",
        None,
        "Path to the vmlinux debuginfo matching KDIVE_LIVE_VM_BZIMAGE for the gated gdb-MI "
        "debug tool smoke. Unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_VM_GDBMI_MODULE_KO",
        "test",
        None,
        "Path to a loaded module .ko for the optional gated gdb-MI module-symbol load smoke. "
        "Unset → that portion of the test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_VM_GDBMI_MODULE_NAME",
        "test",
        None,
        "Loaded module name matching KDIVE_LIVE_VM_GDBMI_MODULE_KO for the optional gated "
        "gdb-MI module-symbol load smoke. Defaults to the .ko path stem.",
    ),
    ExternalEnvVar(
        "KDIVE_REQUIRE_DOCKER",
        "test",
        "0",
        "Set to 1 to fail (not skip) the disposable-Postgres/MinIO fixtures when Docker is absent.",
    ),
    ExternalEnvVar(
        "KDIVE_IMAGE",
        "test",
        None,
        "Container image ref under test for the image smoke test; unset → the smoke test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_OIDC_IMAGE",
        "test",
        None,
        "docker-compose oidc override: set to the published GHCR mirror digest to PULL it "
        "(ADR-0358); unset → compose builds deploy/mock-oidc locally as kdive-mock-oidc:dev.",
    ),
    ExternalEnvVar(
        "KDIVE_STACK_BASE_URL",
        "test",
        None,
        "Base URL of a running kdive server for the live_stack HTTP tier; unset → that tier skips.",
    ),
    ExternalEnvVar(
        "KDIVE_ARTIFACT_DIR",
        "test",
        None,
        "Directory the live_stack spine writes run artifacts to (default: an out-of-tree "
        "temp dir).",
    ),
    ExternalEnvVar(
        "KDIVE_OIDC_CLIENT_ID",
        "test",
        "kdive-test",
        "OIDC client id the live_stack harness presents to the mock issuer.",
    ),
    ExternalEnvVar(
        "KDIVE_SEAM_DOMAIN",
        "test",
        None,
        "libvirt domain name for the in-target guest-agent seam live test.",
    ),
    ExternalEnvVar(
        "KDIVE_SEAM_URI",
        "test",
        None,
        "libvirt connection URI for the in-target guest-agent seam live test.",
    ),
    ExternalEnvVar(
        "KDIVE_REMOTE_BASE_IMAGE_VOLUME",
        "test",
        None,
        "Name of the prebuilt remote-libvirt base-image storage volume for the remote live_stack "
        "test; unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_SSH_PARITY_DOMAIN",
        "test",
        None,
        "Running, agent-ready remote-libvirt domain name for the SSH-parity bootstrap-key "
        "injection live test (#966); unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_SSH_PARITY_URI",
        "test",
        None,
        "libvirt qemu+tls connection URI for the SSH-parity injection live test (#966).",
    ),
    ExternalEnvVar(
        "KDIVE_EL9_REACHABILITY_DOMAIN",
        "test",
        None,
        "Running, agent-ready EL9/RHEL-family remote-libvirt domain name for the host-model CPU "
        "reachability live test (#975, ADR-0297); unset → that test skips.",
    ),
    ExternalEnvVar(
        "KDIVE_EL9_REACHABILITY_URI",
        "test",
        None,
        "libvirt qemu+tls connection URI for the EL9 host-model CPU reachability live test "
        "(#975, ADR-0297).",
    ),
    # --- operator shell scripts -----------------------------------------------------------
    # live-vm guest-image stores (#1292, ADR-0388): scripts/live-vm/{warm-store,stage-tcg-images}.sh
    ExternalEnvVar(
        "KDIVE_WARM_STORE_DIR",
        "script",
        "/var/lib/kdive/warm-store",
        "Persistent warm-store directory `warm-store.sh` refreshes (rootfs + kernel + matching "
        "debuginfo); the `live_vm_host` Ansible role owns it.",
    ),
    ExternalEnvVar(
        "KDIVE_WARM_STORE_TARGET_NVR",
        "script",
        None,
        "Supplied pinned guest-kernel NVR the warm-store refresh keys freshness on (the "
        "operator/CI computes it from the base image; no live distro query). Unset → the script "
        "dies. Same-NVR distro rebuilds need `KDIVE_WARM_STORE_FORCE`.",
    ),
    ExternalEnvVar(
        "KDIVE_WARM_STORE_IMAGE",
        "script",
        None,
        "Catalog rootfs image `warm-store.sh` passes to `python -m kdive build-fs`. Unset → the "
        "script dies.",
    ),
    ExternalEnvVar(
        "KDIVE_WARM_STORE_FORCE",
        "script",
        "0",
        "When `1`, `warm-store.sh` skips the warm fast-path and rebuilds — the escape hatch for a "
        "distro that rebuilt the kernel under an unchanged NVR.",
    ),
    ExternalEnvVar(
        "KDIVE_TCG_STAGE_DIR",
        "script",
        "/mnt/kdive-tcg",
        "Hosted-runner `/mnt` scratch directory `stage-tcg-images.sh` stages the ephemeral ppc64le "
        "TCG image set into.",
    ),
    ExternalEnvVar(
        "KDIVE_TCG_IMAGE",
        "script",
        None,
        "ppc64le catalog rootfs image `stage-tcg-images.sh` passes to `python -m kdive build-fs`. "
        "Unset → the script dies.",
    ),
    ExternalEnvVar(
        "KDIVE_TCG_BUDGET_BYTES",
        "script",
        "7000000000",
        "Enforced `/mnt` disk-budget ceiling for the hosted TCG set (~7 GB): a pre-stage "
        "free-space check for the whole budget and a post-stage staged-set footprint cap, each "
        "failing loud.",
    ),
    ExternalEnvVar(
        "KDIVE_KVM_NODE",
        "script",
        "/dev/kvm",
        "KVM device node `check-local-libvirt.sh` and `check-setup-deps.sh` probe for hardware "
        "virtualization (the latter for its native-arch advisory line).",
    ),
    ExternalEnvVar(
        "KDIVE_BOOT_DIR",
        "script",
        "/boot",
        "Boot directory `check-local-libvirt.sh` scans for readable `vmlinuz-*` host kernels "
        "(libguestfs build-fs appliance, ADR-0222).",
    ),
    ExternalEnvVar(
        "KDIVE_EFFECTIVE_UID",
        "script",
        "$EUID",
        "Effective uid `check-local-libvirt.sh` uses for its non-root-worker readability advisory "
        "(ADR-0223); overrides `$EUID` so the gate is testable independent of the runner's uid.",
    ),
    ExternalEnvVar(
        "KDIVE_REMOTE_SSH_PORT",
        "script",
        "22",
        "SSH port `check-remote-libvirt.sh` connects on.",
    ),
    ExternalEnvVar(
        "KDIVE_REMOTE_PKI_DIR",
        "script",
        "/etc/pki/libvirt",
        "TLS PKI directory `check-remote-libvirt.sh` validates.",
    ),
    ExternalEnvVar(
        "KDIVE_GUEST_HELPERS_DIR",
        "script",
        "deploy/remote-libvirt-guest-helpers",
        "Guest-helper source directory `check-remote-libvirt.sh` inspects.",
    ),
    ExternalEnvVar(
        "KDIVE_OS_RELEASE",
        "script",
        "/etc/os-release",
        "os-release file `check-setup-deps.sh` reads to detect the host distro.",
    ),
    ExternalEnvVar(
        "KDIVE_GUESTFS_SYS_SITE",
        "script",
        "/usr/lib/python3/dist-packages",
        "System dir `check-setup-deps.sh` looks in for the libguestfs binding (guestfs.py) when "
        "deciding its three-state guestfs remedy and performing the venv symlink (ADR-0393).",
    ),
    ExternalEnvVar(
        "KDIVE_SYSTEM_PY_MINOR",
        "script",
        "$(python3 --version)",
        "System Python `X.Y` minor `check-setup-deps.sh` compares against the venv's for the "
        "libguestfs ABI check before symlinking the binding (ADR-0393).",
    ),
    ExternalEnvVar(
        "KDIVE_KERNEL_REPO",
        "script",
        "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git",
        "Kernel git remote `fetch-kernel-tree.sh` clones.",
    ),
    ExternalEnvVar(
        "KDIVE_KERNEL_REF",
        "script",
        "v6.9",
        "Kernel ref (tag/branch/sha) `fetch-kernel-tree.sh` checks out.",
    ),
    ExternalEnvVar(
        "KDIVE_LIVE_SSH_PORT",
        "script",
        "22",
        "SSH port `check-ssh-reachable.sh` probes.",
    ),
    ExternalEnvVar(
        "KDIVE_STACK_PID_FILE",
        "script",
        "~/.local/state/kdive/local-stack.pid",
        "PID file managed by `examples/local-libvirt/up.sh` (written) and "
        "`examples/local-libvirt/down.sh` (read); path is example-scoped, defaulting to "
        "`$XDG_STATE_HOME/kdive/local-stack.pid`.",
    ),
    ExternalEnvVar(
        "KDIVE_STACK_LOG_DIR",
        "script",
        "<repo>/.live-stack-logs",
        "Log directory written by `scripts/live-stack/lib.sh`; also consumed by "
        "`examples/local-libvirt/up.sh`, which overrides the default to an XDG state path "
        "via `examples/local-libvirt/env.sh`.",
    ),
    ExternalEnvVar(
        "KDIVE_ROOTFS_DIR",
        "script",
        "/var/lib/kdive/rootfs",
        "Per-System qcow2 overlay directory for the local-libvirt provider; `scripts/live-stack/"
        "lib.sh` reads this to locate and create guest disk overlays.",
    ),
    ExternalEnvVar(
        "KDIVE_SKIP_OBS",
        "script",
        "0",
        "When set to 1, `scripts/live-stack/up.sh` skips the prometheus/grafana observability "
        "tier; the essential backend services (postgres, minio, oidc) still start.",
    ),
    ExternalEnvVar(
        "KDIVE_WORKER_AS_ROOT",
        "script",
        "1",
        "Whether `restart_host_processes()` in `scripts/live-stack/lib.sh` starts the worker "
        "as root via sudo (1) or as the current user (0).",
    ),
    # Host-published ports for the compose backends. Each is read by BOTH `docker-compose.yml`
    # (the publish side) and `scripts/live-stack/env.sh` (the client-facing DSN/endpoint), so an
    # override relocates the host mapping and the URL that reaches it together. Container-internal
    # ports never change; only the host mapping does.
    ExternalEnvVar(
        "KDIVE_POSTGRES_PORT",
        "script",
        "5432",
        "Host port the compose `postgres` service publishes; `scripts/live-stack/env.sh` folds it "
        "into the default `KDIVE_DATABASE_URL`.",
    ),
    ExternalEnvVar(
        "KDIVE_MINIO_PORT",
        "script",
        "9000",
        "Host port the compose `minio` S3 API publishes; `scripts/live-stack/env.sh` folds it into "
        "the default `KDIVE_S3_ENDPOINT_URL`.",
    ),
    ExternalEnvVar(
        "KDIVE_MINIO_CONSOLE_PORT",
        "script",
        "9001",
        "Host port the compose `minio` web console publishes (no client URL derives from it).",
    ),
    ExternalEnvVar(
        "KDIVE_OIDC_PORT",
        "script",
        "8090",
        "Host port the compose `oidc` mock issuer publishes; `scripts/live-stack/env.sh` folds it "
        "into the default `KDIVE_OIDC_ISSUER` and `KDIVE_OIDC_JWKS_URI`.",
    ),
    ExternalEnvVar(
        "KDIVE_PROMETHEUS_PORT",
        "script",
        "9090",
        "Host port the compose `prometheus` service publishes (obs profile); an off-host grafana "
        "points at this port (#1261).",
    ),
    ExternalEnvVar(
        "KDIVE_GRAFANA_PORT",
        "script",
        "3000",
        "Host port the compose `grafana` service publishes (obs profile).",
    ),
    ExternalEnvVar(
        "KDIVE_DEMO_NAMESPACE",
        "script",
        "kdive-demo",
        "Release namespace `demo-token.sh` targets when minting a bundled-demo bearer token.",
    ),
    ExternalEnvVar(
        "KDIVE_DEMO_FULLNAME",
        "script",
        "kdive-kdive",
        "Chart fullname (`<release>-kdive`) `demo-token.sh` uses to address the server/oidc pods.",
    ),
    ExternalEnvVar(
        "KDIVE_DEMO_CONTEXT",
        "script",
        None,
        "kube context `demo-token.sh` uses (unset → the current context).",
    ),
    ExternalEnvVar(
        "KDIVE_PYTHON",
        "script",
        "python3",
        "Python interpreter the setup-*-libvirt.sh scripts invoke (set to the project venv, "
        "e.g. /opt/kdive/.venv/bin/python, when not running inside the venv).",
    ),
    ExternalEnvVar(
        "KDIVE_SETUP_AUDITED",
        "script",
        "0",
        "When 1, setup-local-libvirt.sh onboards via the audited MCP admin tools instead of "
        "seed-project (requires KDIVE_MCP_BASE and a project-admin KDIVE_TOKEN).",
    ),
    ExternalEnvVar(
        "KDIVE_MCP_BASE",
        "script",
        None,
        "Server MCP endpoint (must end in /mcp) the setup-*-libvirt.sh onboarding calls target.",
    ),
    ExternalEnvVar(
        "KDIVE_PROJECT",
        "script",
        "demo",
        "Project the setup-*-libvirt.sh scripts and `scripts/live-stack/onboard.sh` onboard.",
    ),
    ExternalEnvVar(
        "KDIVE_ROLE",
        "script",
        "admin",
        "Role `scripts/live-stack/onboard.sh` writes into the minted token's `roles` claim and "
        "the printed binding contract; a sub-CONTRIBUTOR value warns (allocations.request needs "
        "CONTRIBUTOR+).",
    ),
    ExternalEnvVar(
        "KDIVE_TOKEN_TTL",
        "script",
        "2592000",
        "Lifetime in seconds of the demo token `scripts/live-stack/onboard.sh` and "
        "`examples/local-libvirt/mint-token.sh` mint; default from `scripts/live-stack/env.sh` "
        "(default 30d). Positive integer; the mock issuer enforces no maximum.",
    ),
    ExternalEnvVar(
        "KDIVE_LIMIT_KCU",
        "script",
        "1000000",
        "Budget ceiling (KCU) the setup-*-libvirt.sh scripts set for the project.",
    ),
    ExternalEnvVar(
        "KDIVE_MAX_ALLOC",
        "script",
        "4",
        "max_concurrent_allocations quota the setup-*-libvirt.sh scripts set.",
    ),
    ExternalEnvVar(
        "KDIVE_MAX_SYS",
        "script",
        "4",
        "max_concurrent_systems quota the setup-*-libvirt.sh scripts set.",
    ),
    # --- in-guest capture/install helpers -------------------------------------------------
    ExternalEnvVar(
        "KDIVE_VMCORE_PATH",
        "guest",
        "/var/crash/*/vmcore",
        "Override the vmcore path `kdive-capture-vmcore` reads (default: the kdump-utils path).",
    ),
    ExternalEnvVar(
        "KDIVE_DMESG_CAP_BYTES",
        "guest",
        "1048576",
        "Byte cap on the inline dmesg `kdive-capture-vmcore` emits (default 1 MiB).",
    ),
    ExternalEnvVar(
        "KDIVE_TITLE",
        "guest",
        "kdive",
        "grub menu title the `kdive-install-kernel` helper assigns the kdive boot slot.",
    ),
    ExternalEnvVar(
        "KDIVE_BTF_PATH",
        "guest",
        "/sys/kernel/btf/vmlinux",
        "BTF path the `kdive-drgn` helper passes to `drgn -s` when readable, so live symbol/type "
        "resolution does not depend on the guest drgn build's BTF auto-load (BBR F1, #1090); "
        "test-only override, unset on production guests.",
    ),
    # --- build-time provenance inputs (ADR-0370) ------------------------------------------
    ExternalEnvVar(
        "KDIVE_BUILDINFO_COMMIT",
        "build",
        None,
        "Short commit SHA `scripts/stamp-buildinfo.sh` bakes into `_buildinfo.py` when set — the "
        "container build passes it in, having no `.git`; unset → derived from live git (ADR-0370).",
    ),
    ExternalEnvVar(
        "KDIVE_COMMIT",
        "build",
        None,
        "Docker build arg carrying the short commit SHA the image bakes as provenance; passed by "
        "ci.yml and release-image.yml. Empty → the stamp is skipped, image reports X.Y.Z-dev "
        "(ADR-0370).",
    ),
    ExternalEnvVar(
        "KDIVE_RELEASE",
        "build",
        "false",
        "Docker build arg: `true` on a `vX.Y.Z` tag build (image reports X.Y.Z+g<sha>), `false` "
        "otherwise (X.Y.Z-dev+g<sha>) (ADR-0370).",
    ),
)


def external_env_names() -> frozenset[str]:
    """Return the set of documented non-registry ``KDIVE_*`` variable names."""
    return frozenset(var.name for var in EXTERNAL_ENV_VARS)
