# cloud-init rootfs first-boot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace kdive's hand-rolled per-family rootfs first-boot glue with cloud-init, fed a build-time baked NoCloud seed, so every local-libvirt image DHCPs its NIC and answers SSH — fixing the live-proven debian SSH-unreachable bug.

**Architecture:** A single family-neutral helper in `_fedora_customize.py` bakes an authoritative `/etc/cloud/cloud.cfg.d/99-kdive.cfg` (network + datasource pin + root protection) plus a NoCloud seed, enables the full four-unit cloud-init pipeline, and seeds machine-id — for both families. The rhel/debian customizers drop their divergent fragments (NM keyfile + cloud-init mask; `cloud-init.disabled` + `kdive-sshd-keygen`) and call the shared helper. The build plane adds an offline guestfish self-check so CI catches silent no-ops it cannot boot.

**Tech Stack:** Python 3.14, `uv`/`.venv`, `pytest -q`, `ruff`, `ty`, libguestfs (`virt-customize`/`guestfish`), cloud-init NoCloud datasource.

## Global Constraints

- Absolute imports only; ≤100 lines/function; ≤100-char lines; Google-style docstrings on public APIs.
- Guardrails run individually in CI: `just lint`, `just type`, `just test`. Run all three before every commit (or the focused `pytest` + `ruff check` + `ty check` for the touched files, then the full suite before push).
- Conventional-commit subjects ≤72 chars, imperative; end every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Reference: spec `docs/superpowers/specs/2026-07-01-cloud-init-first-boot-design.md`, ADR `docs/adr/0288-cloud-init-first-boot.md`.
- YAML gotcha (pinned): write `mode: "off"` quoted — unquoted `off` is YAML boolean `false`, which cloud-init's growpart would not read as the string `"off"`.
- No migration, no domain-XML change, no provisioning change. Capability tags (ADR-0287) stay unchanged.

## File structure

- `src/kdive/images/families/_fedora_customize.py` — **add** the shared cloud-init helper + its content constants; **remove** `SSH_NIC_KEYFILE_PATH/CONTENT`, `_ssh_nic_keyfile_args`, and collapse `debug_image_args` to the drgn helper only. `readiness_unit()` is unchanged.
- `src/kdive/images/families/rhel.py` — drop `_CLOUD_INIT_MASK` + NM keyfile call + the cloud-image machine-id block; call the shared helper.
- `src/kdive/images/families/debian.py` — drop `_SSHD_KEYGEN_UNIT*` + `cloud-init.disabled` + machine-id block; call the shared helper.
- `src/kdive/providers/local_libvirt/rootfs_build.py` — add an offline `verify_cloud_init` build-stage seam and call it before publish.
- Tests: `tests/images/families/test_fedora_customize.py`, `test_rhel.py`, `test_debian.py`, `tests/providers/local_libvirt/test_rootfs_build.py`.

---

### Task 1: Shared cloud-init first-boot helper

**Files:**
- Modify: `src/kdive/images/families/_fedora_customize.py`
- Test: `tests/images/families/test_fedora_customize.py`

**Interfaces:**
- Consumes: `CustomizeContext` (`kind`, `is_cloud_image`, `cleanup`) from `kdive.images.families.base`; `SEED_MACHINE_ID` (already in this module).
- Produces: `cloud_init_first_boot_args(ctx: CustomizeContext) -> list[str]`; module constants `KDIVE_CLOUD_CFG_PATH: str`, `NOCLOUD_SEED_DIR: str`, `CLOUD_INIT_UNITS: str` (the four-unit space-joined string), used by Tasks 3, 4, 5.

- [ ] **Step 1: Write the failing tests** — append to `tests/images/families/test_fedora_customize.py`:

```python
from pathlib import Path

from kdive.images.families._fedora_customize import (
    CLOUD_INIT_UNITS,
    KDIVE_CLOUD_CFG_PATH,
    NOCLOUD_SEED_DIR,
    SEED_MACHINE_ID,
    cloud_init_first_boot_args,
)
from kdive.images.families.base import CustomizeContext


def _ci_ctx(tmp_path: Path, *, is_cloud_image: bool) -> CustomizeContext:
    return CustomizeContext(
        kind="debug",
        packages=("openssh-server",),
        authorized_key=tmp_path / "key.pub",
        readiness_unit_path=tmp_path / "u.service",
        is_cloud_image=is_cloud_image,
        cleanup=[],
        distro="fedora",
        version="44",
    )


def _uploads(argv: list[str]) -> dict[str, str]:
    # Map each `--upload LOCAL:REMOTE` to {REMOTE: text-of-LOCAL}.
    out: dict[str, str] = {}
    for flag, val in zip(argv, argv[1:]):
        if flag == "--upload":
            local, remote = val.split(":", 1)
            out[remote] = Path(local).read_text()
    return out


def test_cloud_init_helper_writes_authoritative_cfg(tmp_path: Path) -> None:
    argv = cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True))
    cfg = _uploads(argv)[KDIVE_CLOUD_CFG_PATH]
    assert "datasource_list: [ NoCloud ]" in cfg
    assert "disable_root: false" in cfg
    assert "dhcp4: true" in cfg and 'match: { name: "e*" }' in cfg
    assert 'mode: "off"' in cfg  # quoted so YAML does not read it as boolean false
    assert "resize_rootfs: false" in cfg


def test_cloud_init_helper_writes_nocloud_seed(tmp_path: Path) -> None:
    argv = cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True))
    uploads = _uploads(argv)
    assert uploads[f"{NOCLOUD_SEED_DIR}/meta-data"].startswith("instance-id:")
    assert uploads[f"{NOCLOUD_SEED_DIR}/user-data"].startswith("#cloud-config")
    assert f"--mkdir" in argv and NOCLOUD_SEED_DIR in argv


def test_cloud_init_helper_enables_full_pipeline_and_seeds_machine_id(tmp_path: Path) -> None:
    j = " ".join(cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True)))
    for unit in ("cloud-init-local.service", "cloud-init.service",
                 "cloud-config.service", "cloud-final.service"):
        assert unit in j
    assert f"systemctl unmask {CLOUD_INIT_UNITS}" in j
    assert f"systemctl enable {CLOUD_INIT_UNITS}" in j
    assert "rm -f /etc/cloud/cloud-init.disabled" in j  # harmless if absent (debian path)
    assert f"/etc/machine-id:{SEED_MACHINE_ID}" in j     # seeded on every image now


def test_cloud_init_helper_installs_cloud_init_only_on_non_cloud_base(tmp_path: Path) -> None:
    cloud = " ".join(cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=True)))
    scratch = " ".join(cloud_init_first_boot_args(_ci_ctx(tmp_path, is_cloud_image=False)))
    assert "--install cloud-init" not in cloud       # ships cloud-init already
    assert "--install cloud-init" in scratch         # virt-builder base needs it installed
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/images/families/test_fedora_customize.py -q`
Expected: FAIL (`ImportError: cannot import name 'cloud_init_first_boot_args'`).

- [ ] **Step 3: Implement the helper + constants** in `src/kdive/images/families/_fedora_customize.py`. Add near the other constants:

```python
# The authoritative kdive first-boot config. cloud-init's *system config* network setting
# outranks the datasource, so carrying the DHCP config here (not only in the seed) defeats a base
# image that ships `network: {config: disabled}`. `mode: "off"` is quoted — unquoted `off` is a
# YAML boolean. `match: {name: "e*"}` is interface-name-independent under the SLIRP NIC.
KDIVE_CLOUD_CFG_PATH = "/etc/cloud/cloud.cfg.d/99-kdive.cfg"
KDIVE_CLOUD_CFG_CONTENT = """\
datasource_list: [ NoCloud ]
disable_root: false
network:
  version: 2
  ethernets:
    kdive-dhcp:
      match: { name: "e*" }
      dhcp4: true
      dhcp-identifier: mac
growpart: { mode: "off" }
resize_rootfs: false
"""
NOCLOUD_SEED_DIR = "/var/lib/cloud/seed/nocloud"
_NOCLOUD_META_DATA = "instance-id: kdive-rootfs\nlocal-hostname: kdive\n"
_NOCLOUD_USER_DATA = "#cloud-config\n"
# The full cloud-init pipeline: cloud-init-local applies the datasource network config at the
# PRE-network stage, so enabling only cloud-init.service would leave the NIC unconfigured.
CLOUD_INIT_UNITS = (
    "cloud-init-local.service cloud-init.service cloud-config.service cloud-final.service"
)
# Best-effort strip of any base drop-in that disables cloud-init network management; the build
# self-check (rootfs_build.py) is the guard that asserts none remain.
_STRIP_NET_DISABLE_CMD = (
    "for f in /etc/cloud/cloud.cfg.d/*.cfg; do "
    "[ -e \"$f\" ] || continue; "
    "grep -qs 'config:[[:space:]]*disabled' \"$f\" && grep -qs 'network' \"$f\" "
    "&& rm -f \"$f\"; done; true"
)


def _staged_upload(content: str, suffix: str, dest: str, cleanup: list[Path]) -> list[str]:
    """Stage ``content`` to a tempfile (appended to ``cleanup``) and upload it to ``dest``."""
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as handle:
        handle.write(content)
        staged = Path(handle.name)
    cleanup.append(staged)
    return ["--upload", f"{staged}:{dest}"]


def cloud_init_first_boot_args(ctx: CustomizeContext) -> list[str]:
    """virt-customize fragment that makes cloud-init the uniform first-boot mechanism (ADR-0288).

    Bakes the authoritative kdive ``cloud.cfg.d`` drop-in (network + NoCloud pin + root
    protection) and a NoCloud seed, strips any base network-disabling drop-in, enables the full
    four-unit cloud-init pipeline, and seeds ``machine-id``. Family-neutral. Installs cloud-init
    on a non-cloud (virt-builder) base, which ships none.

    Args:
        ctx: The customize context; ``is_cloud_image`` gates the cloud-init install and
            ``cleanup`` receives the staged tempfiles for the caller to unlink.
    """
    argv: list[str] = []
    if not ctx.is_cloud_image:
        argv += ["--install", "cloud-init"]
    argv += ["--mkdir", NOCLOUD_SEED_DIR]
    argv += _staged_upload(KDIVE_CLOUD_CFG_CONTENT, ".cfg", KDIVE_CLOUD_CFG_PATH, ctx.cleanup)
    argv += _staged_upload(_NOCLOUD_META_DATA, ".md", f"{NOCLOUD_SEED_DIR}/meta-data", ctx.cleanup)
    argv += _staged_upload(_NOCLOUD_USER_DATA, ".ud", f"{NOCLOUD_SEED_DIR}/user-data", ctx.cleanup)
    argv += [
        "--run-command", _STRIP_NET_DISABLE_CMD,
        "--run-command", "rm -f /etc/cloud/cloud-init.disabled",
        "--run-command", f"systemctl unmask {CLOUD_INIT_UNITS}",
        "--run-command", f"systemctl enable {CLOUD_INIT_UNITS}",
        "--write", f"/etc/machine-id:{SEED_MACHINE_ID}",  # pragma: allowlist secret
    ]
    return argv
```

`CustomizeContext` is already importable in this module? It is NOT today — add the import at the top: extend the existing base import to `from kdive.images.families.base import CustomizeContext` **only if** `_fedora_customize.py` does not already import it. It does not, so add:

```python
from kdive.images.families.base import CustomizeContext
```

(Place it with the other `kdive.images.families` imports; `tempfile` and `Path` are already imported.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/images/families/test_fedora_customize.py -q && .venv/bin/ruff check src/kdive/images/families/_fedora_customize.py && .venv/bin/ty check src/kdive/images/families/_fedora_customize.py`
Expected: PASS, no lint/type errors.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/families/_fedora_customize.py tests/images/families/test_fedora_customize.py
git commit -m "feat(962): shared cloud-init first-boot helper (seed + drop-in + pipeline)"
```

---

### Task 2: Remove the NetworkManager SSH-NIC keyfile

**Files:**
- Modify: `src/kdive/images/families/_fedora_customize.py` (remove `SSH_NIC_KEYFILE_PATH`, `SSH_NIC_KEYFILE_CONTENT`, `_ssh_nic_keyfile_args`; collapse `debug_image_args`)
- Modify: `tests/providers/local_libvirt/test_rootfs_build.py` (it imports `SSH_NIC_KEYFILE_CONTENT` at line ~18)
- Test: `tests/images/families/test_rhel.py`

**Interfaces:**
- Produces: `debug_image_args(packages, cleanup)` now returns only the drgn helper fragment (no NM keyfile). Signature unchanged so rhel's caller is untouched.

- [ ] **Step 1: Write/adjust the failing test** — in `tests/images/families/test_rhel.py`, add (or update the analogous existing assertion):

```python
def test_rhel_argv_stages_no_nm_ssh_nic_keyfile(tmp_path: Path) -> None:
    # ADR-0288: cloud-init DHCPs the NIC now; the NetworkManager SSH-NIC keyfile is gone.
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "kdive-ssh-nic" not in j
    assert "NetworkManager/system-connections" not in j
```

(Use the `_ctx` helper already defined in `test_rhel.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/images/families/test_rhel.py::test_rhel_argv_stages_no_nm_ssh_nic_keyfile -q`
Expected: FAIL (the keyfile is still staged today).

- [ ] **Step 3: Remove the NM keyfile.** In `_fedora_customize.py` delete `SSH_NIC_KEYFILE_PATH`, `SSH_NIC_KEYFILE_CONTENT`, and `_ssh_nic_keyfile_args`, and simplify `debug_image_args`:

```python
def debug_image_args(packages: tuple[str, ...], cleanup: list[Path]) -> list[str]:
    """Stage the drgn helper for an ``rhel`` debug image (ADR-0220, #724).

    ``cleanup`` is retained for signature stability with the caller; the drgn helper stages no
    tempfile. Non-debug images (no ``drgn`` in ``packages``) get an empty fragment.
    """
    del cleanup
    if "drgn" not in packages:
        return []
    return drgn_helper_args()
```

In `tests/providers/local_libvirt/test_rootfs_build.py`, remove the now-dead import `from kdive.images.families._fedora_customize import SSH_NIC_KEYFILE_CONTENT` and any test body that references `SSH_NIC_KEYFILE_CONTENT` (the plane no longer stages it; replace such a test with an assertion on the cloud-init drop-in in Task 5, or delete it if it only pinned the keyfile).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/images/families/test_rhel.py tests/providers/local_libvirt/test_rootfs_build.py -q && .venv/bin/ruff check src/kdive/images/families/_fedora_customize.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/families/_fedora_customize.py tests/images/families/test_rhel.py tests/providers/local_libvirt/test_rootfs_build.py
git commit -m "refactor(962): drop the NetworkManager SSH-NIC keyfile (cloud-init DHCPs now)"
```

---

### Task 3: Route the rhel family through cloud-init

**Files:**
- Modify: `src/kdive/images/families/rhel.py`
- Test: `tests/images/families/test_rhel.py`

**Interfaces:**
- Consumes: `cloud_init_first_boot_args`, `KDIVE_CLOUD_CFG_PATH`, `CLOUD_INIT_UNITS` from Task 1; `debug_image_args` from Task 2.

- [ ] **Step 1: Write the failing tests** in `tests/images/families/test_rhel.py`:

```python
def test_rhel_argv_bakes_cloud_init_and_stops_masking(tmp_path: Path) -> None:
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "/etc/cloud/cloud.cfg.d/99-kdive.cfg" in j       # authoritative drop-in
    assert "systemctl enable cloud-init-local.service" in j  # full pipeline enabled
    assert "systemctl mask cloud-init" not in j              # no longer masked


def test_rhel_argv_still_injects_key_and_selinux(tmp_path: Path) -> None:
    # Anti-regression: the --ssh-inject managed key and SELinux permissive edit stay.
    argv = RhelFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert f"root:file:{tmp_path / 'key.pub'}" in j
    assert "SELINUX=permissive" in j
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/images/families/test_rhel.py -k "cloud_init or selinux" -q`
Expected: FAIL (mask still emitted; cfg absent).

- [ ] **Step 3: Edit `rhel.py`.** Remove the `_CLOUD_INIT_MASK` constant and the `if ctx.is_cloud_image:` block that emits `_CLOUD_INIT_MASK` + the machine-id write. Add the shared import and call the helper. The `customize_argv` becomes:

```python
from kdive.images.families._fedora_customize import (  # extend the existing import
    DEFAULT_BUILD_FS_PACKAGES,
    DEFAULT_DEBUG_FS_PACKAGES,
    FSTAB,
    KDUMP_FINAL_ACTION_CMD,
    KDUMP_SYSCTL_CONTENT,
    KDUMP_SYSCTL_PATH,
    READINESS_MARKER,
    cloud_init_first_boot_args,
    debug_image_args,
    makedumpfile_version_marker_args,
)
```

```python
    def customize_argv(self, ctx: CustomizeContext) -> list[str]:
        """Build the virt-customize argv that turns the base image into a kdive-ready rootfs."""
        argv: list[str] = []
        if _el_major(ctx.distro, ctx.version) == 8 and "drgn" in ctx.packages:
            argv += ["--run-command", _ENABLE_EPEL_CMD]
        argv += [
            "--install",
            ",".join(ctx.packages),
            "--run-command",
            "systemctl enable sshd.service",
        ]
        if "kexec-tools" in ctx.packages:
            argv += [
                "--run-command",
                "systemctl enable kdump.service",
                "--write",
                f"{KDUMP_SYSCTL_PATH}:{KDUMP_SYSCTL_CONTENT}",
                "--run-command",
                KDUMP_FINAL_ACTION_CMD,
            ]
        argv += cloud_init_first_boot_args(ctx)   # replaces the mask + machine-id block
        argv += debug_image_args(ctx.packages, ctx.cleanup)
        if ctx.kind == "debug":
            argv += makedumpfile_version_marker_args()
        argv += [
            "--ssh-inject",
            f"root:file:{ctx.authorized_key}",
            "--upload",
            f"{ctx.readiness_unit_path}:/etc/systemd/system/{READINESS_MARKER}.service",
            "--run-command",
            f"systemctl enable {READINESS_MARKER}.service",
            "--run-command",
            _SELINUX_PERMISSIVE_SED,
        ]
        return argv
```

Delete `_CLOUD_INIT_MASK` and the now-unused `SEED_MACHINE_ID` import from `rhel.py` (the seed moved into the helper). Update the module docstring's "masks cloud-init and seeds `/etc/machine-id`" sentence to "enables cloud-init via a baked NoCloud seed (ADR-0288)".

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/images/families/test_rhel.py -q && .venv/bin/ruff check src/kdive/images/families/rhel.py && .venv/bin/ty check src/kdive/images/families/rhel.py`
Expected: PASS. (Delete/adjust any stale test asserting `systemctl mask cloud-init` or the old machine-id-on-cloud gating.)

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/families/rhel.py tests/images/families/test_rhel.py
git commit -m "feat(962): route rhel family first-boot through cloud-init"
```

---

### Task 4: Route the debian family through cloud-init

**Files:**
- Modify: `src/kdive/images/families/debian.py`
- Test: `tests/images/families/test_debian.py`

**Interfaces:**
- Consumes: `cloud_init_first_boot_args` from Task 1.

- [ ] **Step 1: Write the failing tests** in `tests/images/families/test_debian.py`:

```python
def test_debian_argv_bakes_cloud_init_drops_sshd_keygen(tmp_path: Path) -> None:
    argv = DebianFamily().customize_argv(_ctx(tmp_path, is_cloud_image=True))
    j = " ".join(argv)
    assert "/etc/cloud/cloud.cfg.d/99-kdive.cfg" in j
    assert "systemctl enable cloud-init-local.service" in j
    assert "cloud-init.disabled" not in j            # no longer disabled
    assert "kdive-sshd-keygen" not in j              # cloud-init generates host keys
    assert "ssh-keygen -A" not in j
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/images/families/test_debian.py::test_debian_argv_bakes_cloud_init_drops_sshd_keygen -q`
Expected: FAIL.

- [ ] **Step 3: Edit `debian.py`.** Delete `_SSHD_KEYGEN_UNIT_PATH`, `_SSHD_KEYGEN_UNIT`, `_CLOUD_INIT_DISABLED_PATH`. Remove the `--write` of the keygen unit + its `systemctl enable`, and the `if ctx.is_cloud_image:` block that touched `cloud-init.disabled` + machine-id. Add the import and call the helper. The relevant portion of `customize_argv`:

```python
from kdive.images.families._fedora_customize import (  # extend the existing import
    FSTAB,
    KDUMP_SYSCTL_CONTENT,
    KDUMP_SYSCTL_PATH,
    READINESS_MARKER,
    SEED_MACHINE_ID,   # remove if now unused after the block below is deleted
    cloud_init_first_boot_args,
    drgn_helper_args,
    makedumpfile_version_marker_args,
)
```

```python
    def customize_argv(self, ctx: CustomizeContext) -> list[str]:
        """Build the virt-customize argv that turns the Debian base into a kdive-ready rootfs."""
        argv: list[str] = [
            "--install",
            ",".join(ctx.packages),
            "--run-command",
            "systemctl enable ssh.service",
        ]
        if "kdump-tools" in ctx.packages:
            argv += [
                "--run-command",
                "systemctl enable kdump-tools.service",
                "--run-command",
                _USE_KDUMP_CMD,
                "--write",
                f"{KDUMP_SYSCTL_PATH}:{KDUMP_SYSCTL_CONTENT}",
            ]
        argv += cloud_init_first_boot_args(ctx)   # cloud-init owns network + host keys now
        if ctx.kind == "debug":
            argv += drgn_helper_args()
            argv += makedumpfile_version_marker_args()
        argv += [
            "--ssh-inject",
            f"root:file:{ctx.authorized_key}",
            "--upload",
            f"{ctx.readiness_unit_path}:/etc/systemd/system/{READINESS_MARKER}.service",
            "--run-command",
            f"systemctl enable {READINESS_MARKER}.service",
        ]
        return argv
```

Remove the `import tempfile` / `Callable` / `run_guestfs_tool` bits only if they become unused (the `normalize` method still uses `tempfile` + `run_guestfs_tool`, so keep them). Update the module docstring's cloud-init-disable / sshd-keygen sentences to reflect ADR-0288 (cloud-init enabled via a baked NoCloud seed; host keys from cloud-init's ssh module).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/images/families/test_debian.py -q && .venv/bin/ruff check src/kdive/images/families/debian.py && .venv/bin/ty check src/kdive/images/families/debian.py`
Expected: PASS. Delete the now-false tests `test_debug_argv_stages_sshd_host_key_generation`, `test_cloud_image_disables_cloud_init_version_proof_and_seeds_machine_id`, and adjust `test_virt_builder_source_skips_cloud_init_and_machine_id` (a virt-builder base now `--install cloud-init` and IS seeded — rewrite it to assert `--install cloud-init` is present and machine-id is seeded).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/images/families/debian.py tests/images/families/test_debian.py
git commit -m "feat(962): route debian family first-boot through cloud-init"
```

---

### Task 5: Offline build self-check

> **Provisional until Task 6.** The seam wiring below is fully unit-tested, but the *real*
> `_real_verify_cloud_init` guestfish script (verbs `exists`/`! sh`/`systemctl`) is
> `pragma: no cover - live_vm` and is only exercised on the KVM host in Task 6. Treat this task
> as not-done until Task 6 confirms the script on a real built image; any verb correction lands
> back here and the unit seam tests below must stay green before the PR ships.

**Files:**
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py`
- Modify: `tests/providers/local_libvirt/test_rootfs_build.py` (the `_Recorder` + `_tools` helpers)

**Interfaces:**
- Consumes: `KDIVE_CLOUD_CFG_PATH`, `NOCLOUD_SEED_DIR`, `CLOUD_INIT_UNITS` from Task 1.
- Produces: a `verify_cloud_init: VerifyCloudInit` field on `RootfsBuildTools` (default
  `_real_verify_cloud_init`), called in `build()` after `family.normalize(staged)` and before
  `publish_qcow2`.

The test module already has a `_Recorder` (records `order`/`customize_argvs`/…), a `_tools(rec,
inspect_versions=…, probe_makedumpfile=…)` factory that builds `RootfsBuildTools(...)`
explicitly, a `_plane(tmp_path, rec, …)` factory, and a `_key(tmp_path)` helper. **Because
Task 5 adds a `verify_cloud_init` field whose default is the real guestfish runner, `_tools`
must be updated to pass a stub** — otherwise every existing build test would invoke real
guestfish.

- [ ] **Step 1: Extend `_Recorder` and `_tools`** in `tests/providers/local_libvirt/test_rootfs_build.py`.

Add to the `_Recorder` dataclass (alongside the other `list[...]` fields):

```python
    verify_calls: list[Path] = field(default_factory=list)

    def verify_cloud_init(self, qcow2: Path) -> None:
        self.order.append("verify")
        self.verify_calls.append(qcow2)
```

Update `_tools` to accept and forward the seam (defaulting to the recorder's stub so no test
touches real guestfish):

```python
def _tools(
    rec: _Recorder,
    inspect_versions: VersionInspectSeam = _no_versions,
    probe_makedumpfile: MakedumpfileProbeSeam = _no_makedumpfile,
    verify_cloud_init: object | None = None,
) -> RootfsBuildTools:
    return RootfsBuildTools(
        resolve_authorized_key=rec.resolve_authorized_key,
        acquire_base=rec.acquire_base,
        customize=rec.customize,
        repack_whole_disk_ext4=rec.repack_whole_disk_ext4,
        family_for=rec.family_for,
        inspect_versions=inspect_versions,
        probe_makedumpfile=probe_makedumpfile,
        verify_cloud_init=verify_cloud_init or rec.verify_cloud_init,  # ty: ignore[invalid-argument-type]
    )
```

- [ ] **Step 2: Write the failing tests** (append to the same test module):

```python
def test_build_runs_cloud_init_self_check_after_normalize(tmp_path: Path) -> None:
    # The plane must run verify_cloud_init on the staged image, after normalize, before publish.
    rec = _Recorder(authorized_key=_key(tmp_path))
    _plane(tmp_path, rec).build(_spec())
    assert rec.verify_calls, "verify_cloud_init must run on the built image"
    assert rec.order.index("verify") > rec.order.index("normalize")


def test_build_fails_when_cloud_init_self_check_rejects(tmp_path: Path) -> None:
    rec = _Recorder(authorized_key=_key(tmp_path))

    def _reject(_qcow2: Path) -> None:
        raise CategorizedError(
            "cloud-init self-check failed",
            category=ErrorCategory.PROVISIONING_FAILURE,
        )

    plane = LocalLibvirtRootfsBuildPlane(
        workspace=tmp_path / "work",
        tools=_tools(rec, verify_cloud_init=_reject),
    )
    with pytest.raises(CategorizedError) as err:
        plane.build(_spec())
    assert err.value.category is ErrorCategory.PROVISIONING_FAILURE
```

(`_plane` calls `_tools(rec, …)`, which now defaults `verify_cloud_init` to `rec.verify_cloud_init`, so the first test needs no extra wiring.)

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/providers/local_libvirt/test_rootfs_build.py -k "self_check or reject" -q`
Expected: FAIL (`TypeError: RootfsBuildTools got an unexpected keyword 'verify_cloud_init'`).

- [ ] **Step 4: Implement the seam + the real check** in `rootfs_build.py`. Add the type + field + default, and call it in `build()`:

```python
type VerifyCloudInit = Callable[[Path], None]


def _real_verify_cloud_init(qcow2: Path) -> None:  # pragma: no cover - live_vm
    """Assert cloud-init first-boot is correctly baked into the built image (ADR-0288).

    Fails the build if any cloud.cfg.d drop-in still disables cloud-init networking, if the four
    cloud-init units are not enabled, or if the kdive drop-in / NoCloud seed are missing — the
    offline guard for silent no-ops CI cannot catch by booting.
    """
    from kdive.images.families._fedora_customize import (
        CLOUD_INIT_UNITS,
        KDIVE_CLOUD_CFG_PATH,
        NOCLOUD_SEED_DIR,
    )

    units = " ".join(f"is-enabled {u}" for u in CLOUD_INIT_UNITS.split())
    # guestfish reads the offline image; `sh` runs inside it via the appliance.
    script = (
        f"exists {KDIVE_CLOUD_CFG_PATH}\n"
        f"exists {NOCLOUD_SEED_DIR}/meta-data\n"
        "! sh 'grep -rqs \"config:[[:space:]]*disabled\" /etc/cloud/cloud.cfg.d/'\n"
        f"sh 'systemctl {units}'\n"
    )
    run_guestfs_tool(
        ["guestfish", "--ro", "-a", str(qcow2), "-i"],
        stage="cloud-init-self-check",
        timeout_s=_REPACK_TIMEOUT_S,
        missing_message="guestfish is not installed; cannot verify cloud-init in the rootfs",
        failure_message="built image failed the cloud-init first-boot self-check (ADR-0288)",
        input_text=script,
    )
```

Add to `RootfsBuildTools`:

```python
    verify_cloud_init: VerifyCloudInit = _real_verify_cloud_init
```

In `build()`, after `family.normalize(staged)` and before `installed = self._inspect_installed(scratch)`:

```python
            family.normalize(staged)
            self._tools.verify_cloud_init(staged)
```

Note for the implementer: the exact `guestfish` verb spelling (`is-enabled` via `systemctl` in the appliance, `exists`, `! sh`) must be confirmed against a real built image during the live proof (Task 6); the seam is injected so unit tests do not depend on guestfish. If `systemctl is-enabled` is unreliable in the read-only appliance, assert instead on the presence of the enable symlinks under `/etc/systemd/system/cloud-init.target.wants/` (cloud-init units are `WantedBy=cloud-init.target`) and `cloud-init.target` under `multi-user.target.wants/`.

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/pytest tests/providers/local_libvirt/test_rootfs_build.py -q && .venv/bin/ruff check src/kdive/providers/local_libvirt/rootfs_build.py && .venv/bin/ty check src/kdive/providers/local_libvirt/rootfs_build.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/local_libvirt/rootfs_build.py tests/providers/local_libvirt/test_rootfs_build.py
git commit -m "feat(962): offline cloud-init self-check on the built rootfs"
```

---

### Task 6: Live end-to-end proof (operator-run, this host has KVM)

**Files:**
- Use: `scripts/live-stack/*.sh` (bring the stack up), the MCP flow driver.

This task is not unit-testable; it is the acceptance proof and is run on the KVM build host, not in CI.

- [ ] **Step 0: Prerequisites** — the provision→SSH flow (Step 3) drives the full MCP lifecycle, so it needs the live stack, a token, project funding, and a kernel tree. Without these, `allocations.request` is denied (zero quota/budget) or provision skips.

```bash
# Bring up the live stack (compose backends + host server/reconciler/worker + libvirt).
bash scripts/live-stack/up.sh                 # or scripts/live-stack/status.sh if already up
# The server env already exports these on this host; re-export for the driver shell:
export KDIVE_DATABASE_URL="postgresql://kdive:kdive@localhost:5432/kdive"  # pragma: allowlist secret
export KDIVE_KERNEL_SRC="/home/dave/src/linux"          # a prebuilt kernel tree (has arch/x86/boot/bzImage)
export KDIVE_STACK_BASE_URL="http://127.0.0.1:8000/mcp"
# Token: use the operator-supplied bearer, or mint one from the bundled OIDC issuer.
export KDIVE_TOKEN="$(cat <the operator token file>)"   # projects:["demo"], roles:{"demo":"admin"}
```

Seed the `demo` project's budget + quota so the first `allocations.request` is granted (idempotent upsert, mirrors `tests/integration/live_stack/spine.py::seed_metering`):

```python
import psycopg, os
with psycopg.connect(os.environ["KDIVE_DATABASE_URL"]) as c:
    c.execute("INSERT INTO budgets (project, limit_kcu) VALUES ('demo','1000000') "
              "ON CONFLICT (project) DO UPDATE SET limit_kcu=EXCLUDED.limit_kcu")
    c.execute("INSERT INTO quotas (project, max_concurrent_allocations, max_concurrent_systems) "
              "VALUES ('demo',4,4) ON CONFLICT (project) DO UPDATE SET "
              "max_concurrent_allocations=4, max_concurrent_systems=4")
    c.commit()
```

- [ ] **Step 1: Rebuild the affected images** with the new build path. NOTE: `build-fs` overwrites the staged qcow2 at `/var/lib/kdive/rootfs/local/<name>.qcow2` in place — on a broken build the previously-working image is gone (rebuild-only recovery). This is a devel host, so overwrite is acceptable; do not run on anything you cannot rebuild.

```bash
.venv/bin/python -m kdive build-fs --image debian-kdive-ready-13
.venv/bin/python -m kdive build-fs --image fedora-kdive-ready-44
```

- [ ] **Step 2: Offline-verify the built debian image** before booting:

```bash
LIBGUESTFS_BACKEND=direct virt-cat -a /var/lib/kdive/rootfs/local/debian-kdive-ready-13.qcow2 /etc/cloud/cloud.cfg.d/99-kdive.cfg
LIBGUESTFS_BACKEND=direct virt-ls -a /var/lib/kdive/rootfs/local/debian-kdive-ready-13.qcow2 /etc/systemd/system/cloud-init.target.wants
```
Expected: the drop-in prints; the four cloud-init unit symlinks are present.

- [ ] **Step 3: Run the provision → authorize → SSH proof** (the flow that fails on `main`): allocate a System from `debian-kdive-ready-13`, `systems.provision` to ready, `systems.ssh_info`, `systems.authorize_ssh_key` (must succeed), then `ssh root@<host> -p <port>` runs `cat /etc/os-release; uname -r; systemctl is-active ssh; ip -4 addr`. Expected: an IPv4 address is present, host keys exist, sshd answers, the command returns Debian 13 output. Tear down the System and release the allocation afterward.

- [ ] **Step 4: Record the proof** in the PR body (image digests, the in-guest command output) and, if it revealed guestfish verb corrections for Task 5, fold them back into `rootfs_build.py` and re-run the unit tests.

---

## Post-merge follow-on (not part of this PR)

- Rebuild all nine local images and re-point the operator's `~/.config/kdive/systems.toml` if any capability tag or digest changes (tags are unchanged by ADR-0288, so this is digest-refresh only).
- The S2 `ssh_reachable` boot-probe (#956) verifies SSH-answered efficacy end-to-end and can then gate readiness on reachability.
