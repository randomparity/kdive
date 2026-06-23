# Ubuntu build-fs libguestfs diagnostics (#694) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the two opaque libguestfs `build-fs` failures on Ubuntu 24.04 (host kernel unreadable; passt appliance-network exit 1) into actionable `CONFIGURATION_ERROR`s, and catch the kernel case in the preflight before the slow build. Implements ADR-0222 (#694).

**Architecture:** Detect-and-guide, not auto-fix. (1) `run_guestfs_tool` (the shared build helper) gains stderr-signature classification: on a non-zero tool exit, two anchored signatures map to `CONFIGURATION_ERROR` with a `remediation` detail; every other non-zero exit keeps the existing generic `PROVISIONING_FAILURE`. (2) `check-local-libvirt.sh` gains a host-kernel-readability probe over all `/boot/vmlinuz-*`. (3) The walkthrough cross-links the ADR.

**Tech Stack:** Python 3.14 (`uv`, `ruff`, `ty`, `pytest`), Bash (`shellcheck`, `shfmt`), Markdown docs.

## Global Constraints

- **No KVM/libguestfs in CI.** Everything must be unit-testable without an appliance: `run_guestfs_tool` via a monkeypatched `subprocess.run` returning synthetic stderr; `check-local-libvirt.sh` via PATH stubs + tmp dirs (existing patterns in `tests/images/planes/test_build_common.py` and `tests/scripts/test_check_local_libvirt.py`).
- **Error taxonomy:** use the existing `ErrorCategory` values only (`CONFIGURATION_ERROR`, `PROVISIONING_FAILURE`); never invent strings. `domain/errors.py`.
- **Additive classification only:** any non-zero exit that does not match a signature must behave exactly as today (generic `PROVISIONING_FAILURE`, truncated `stderr[-2000:]`). Do not change the `failure_message`/`missing`/`timeout`/`OSError` branches.
- **Anchored signatures:** the kernel signature MUST bind the permission failure to a `vmlinuz`/`/boot` token on a single logical match — never bare `Permission denied`. A negative test (unrelated `Permission denied`) MUST fall through to `PROVISIONING_FAILURE`.
- **Style:** ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` whole-tree; plain factual prose (no "critical"/"robust"/"comprehensive"; use "Milestone" not "Sprint"). Bash starts `set -euo pipefail`; `shellcheck` + `shfmt -d` clean.
- **Guardrails before each commit:** `just lint`, `just type`, the focused test(s); `just lint-shell` for the shell task. Run full `just ci` once before pushing.
- **Cite ADR-0222** in the docstring of any `src/` code you change for this work (the ADR is already `Accepted`, satisfying the `adr-status-check` gate).

---

### Task 1: Signature remap in `run_guestfs_tool`

**Files:**
- Modify: `src/kdive/images/planes/_build_common.py` (the non-zero-exit branch of `run_guestfs_tool`, lines 67-72)
- Test: `tests/images/planes/test_build_common.py`

**Interfaces:**
- Consumes: `CategorizedError`, `ErrorCategory` (already imported); `re` (already imported); `subprocess` (already imported).
- Produces: no signature change to `run_guestfs_tool`. New module-level constants `_KERNEL_UNREADABLE_RE`, `_PASST_FAILURE_RE`, and a private helper `_remediation_for_stderr(stderr: str) -> tuple[str, str] | None` returning `(message, remediation)` or `None`. Later tasks do not depend on these.

**Context:** `run_guestfs_tool` runs every libguestfs tool for both the local and remote rootfs build planes. Today every non-zero exit becomes a generic `PROVISIONING_FAILURE`. We add signature classification *before* that fallback. The two #694 stderr signatures, verbatim:
- kernel: `cp: cannot open '/boot/vmlinuz-6.8.0-124-generic' for reading: Permission denied` (and the trailing `supermin: ... command failed` / `libguestfs: error: ... supermin exited with error status 1`).
- passt: `virt-builder: error: libguestfs error: passt exited with status 1`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/images/planes/test_build_common.py` (the helper `_failed` pattern already exists in the file — define a small local factory):

```python
def _stub_failed_run(
    monkeypatch: pytest.MonkeyPatch, stderr: str, returncode: int = 1
) -> None:
    def _failed(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(_build_common.subprocess, "run", _failed)


def test_run_guestfs_tool_maps_unreadable_host_kernel_to_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = (
        "cp: cannot open '/boot/vmlinuz-6.8.0-124-generic' for reading: Permission denied\n"
        "supermin: aborting: command failed\n"
        "libguestfs: error: /usr/bin/supermin exited with error status 1"
    )
    _stub_failed_run(monkeypatch, stderr)

    with pytest.raises(CategorizedError) as caught:
        run_guestfs_tool(
            ["virt-builder", "fedora-43"],
            stage="virt-builder",
            timeout_s=60,
            missing_message="virt-builder is not installed",
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "vmlinuz" in str(caught.value)
    assert "chmod 0644 /boot/vmlinuz-*" in caught.value.details["remediation"]
    assert caught.value.details["stage"] == "virt-builder"
    assert "Permission denied" in caught.value.details["stderr"]


def test_run_guestfs_tool_maps_passt_failure_to_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "virt-builder: error: libguestfs error: passt exited with status 1"
    _stub_failed_run(monkeypatch, stderr)

    with pytest.raises(CategorizedError) as caught:
        run_guestfs_tool(
            ["virt-builder", "fedora-43"],
            stage="virt-builder",
            timeout_s=60,
            missing_message="virt-builder is not installed",
        )

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "passt" in str(caught.value)
    assert "apparmor_parser -R /etc/apparmor.d/usr.bin.passt" in caught.value.details["remediation"]


def test_run_guestfs_tool_unrelated_permission_denied_stays_provisioning_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A permission error NOT on the host kernel must fall through to the generic failure,
    # so the operator is not wrongly told to chmod /boot/vmlinuz-*.
    stderr = "virt-make-fs: error: cannot open output '/var/lib/kdive/out.qcow2': Permission denied"
    _stub_failed_run(monkeypatch, stderr)

    with pytest.raises(CategorizedError) as caught:
        run_guestfs_tool(
            ["virt-make-fs"],
            stage="repack",
            timeout_s=60,
            missing_message="virt-make-fs is not installed",
            failure_message="repack failed",
        )

    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert str(caught.value) == "repack failed"
    assert "remediation" not in caught.value.details
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/images/planes/test_build_common.py -k "kernel or passt or unrelated_permission" -q`
Expected: FAIL — the two positive tests get `PROVISIONING_FAILURE` instead of `CONFIGURATION_ERROR` / `KeyError` on `details["remediation"]`; the negative test passes already.

- [ ] **Step 3: Write the minimal implementation**

In `src/kdive/images/planes/_build_common.py`, add the constants near the top (after `_NAME_RE`) and a helper, then call it in the non-zero branch. Anchor the kernel regex to a `vmlinuz` token on one match:

```python
# ADR-0222 (#694): two libguestfs stderr signatures get an actionable CONFIGURATION_ERROR
# instead of the generic PROVISIONING_FAILURE. The kernel pattern binds "Permission denied"
# to a vmlinuz path on one match so an unrelated permission error is NOT misattributed to the
# host kernel. supermin's own "cannot read .../vmlinuz..." phrasing is covered by the same
# vmlinuz+denial anchor.
_KERNEL_UNREADABLE_RE = re.compile(
    r"(?:/boot/)?vmlinuz[^\n'\"]*['\"]?[^\n]*?(?:Permission denied|cannot (?:open|read))"
    r"|(?:Permission denied|cannot (?:open|read))[^\n]*?vmlinuz",
)
_PASST_FAILURE_RE = re.compile(r"passt exited with status")

_KERNEL_REMEDIATION = (
    "the libguestfs appliance cannot read the host kernel — Debian/Ubuntu ship "
    "/boot/vmlinuz-* as root:0600. Make them readable (run this as the worker user): "
    "`sudo chmod 0644 /boot/vmlinuz-*` (re-apply after a kernel upgrade, or use dpkg-statoverride)"
)
_PASST_REMEDIATION = (
    "the libguestfs appliance network (passt) failed. Unload the passt AppArmor profile "
    "(`sudo apparmor_parser -R /etc/apparmor.d/usr.bin.passt`); if it still fails (a "
    "libguestfs/passt version mismatch on Ubuntu 24.04), build the rootfs on a host with a "
    "working libguestfs appliance or stage a prebuilt bootable qcow2"
)


def _remediation_for_stderr(stderr: str) -> tuple[str, str] | None:
    """Return ``(message, remediation)`` for a known libguestfs host-setup failure, else None."""
    if _KERNEL_UNREADABLE_RE.search(stderr):
        return ("libguestfs cannot read the host kernel /boot/vmlinuz-*", _KERNEL_REMEDIATION)
    if _PASST_FAILURE_RE.search(stderr):
        return ("the libguestfs appliance network (passt) failed", _PASST_REMEDIATION)
    return None
```

Then replace the non-zero-exit branch (currently lines 67-72) with:

```python
    if result.returncode != 0:
        known = _remediation_for_stderr(result.stderr)
        if known is not None:
            message, remediation = known
            raise CategorizedError(
                f"{stage}: {message}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "stage": stage,
                    "tool": argv[0],
                    "remediation": remediation,
                    "stderr": result.stderr[-2000:],
                },
            )
        raise CategorizedError(
            failure_message or f"{stage} failed",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"stage": stage, "tool": argv[0], "stderr": result.stderr[-2000:]},
        )
```

Also extend the module docstring's first line or add an `ADR-0222` reference so the citation is present in `src/`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/images/planes/test_build_common.py -q`
Expected: PASS (all existing tests still green — the generic-failure tests at lines 125-171 exercise the fall-through and must stay green).

- [ ] **Step 5: Guardrails + commit**

Run: `uv run ruff check src/kdive/images/planes/_build_common.py tests/images/planes/test_build_common.py && uv run ruff format --check src/kdive/images/planes/_build_common.py && just type`
Expected: clean.

```bash
git add src/kdive/images/planes/_build_common.py tests/images/planes/test_build_common.py
git commit -m "feat(build): map libguestfs host-setup failures to actionable errors

Detect the unreadable-host-kernel and passt-appliance-network stderr
signatures in run_guestfs_tool and raise CONFIGURATION_ERROR with a
remediation hint instead of an opaque PROVISIONING_FAILURE (ADR-0222, #694).
The kernel signature anchors Permission-denied to a vmlinuz path so unrelated
permission errors stay generic.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Host-kernel readability preflight in `check-local-libvirt.sh`

**Files:**
- Modify: `scripts/check-local-libvirt.sh`
- Test: `tests/scripts/test_check_local_libvirt.py`

**Interfaces:**
- Consumes: the existing `note_fail` function and `fail` variable in the script; the test's `_stub`/`_run` helpers and the all-healthy env in `tests/scripts/test_check_local_libvirt.py`.
- Produces: a new `KDIVE_BOOT_DIR` env override (default `/boot`) and a `_host_kernels_readable` probe. **Hermeticity requirement:** because the probe defaults `BOOT_DIR` to the runner's real `/boot`, **every** test in `tests/scripts/test_check_local_libvirt.py` must set `KDIVE_BOOT_DIR` to a controlled tmp dir, or it will read the host `/boot` and become flaky. `test_all_healthy_exits_zero` (asserts exit 0) is the one that actually breaks without it; the four failure-path tests would otherwise read host `/boot` harmlessly (their exit-1 assertion still holds) but must still be pinned for hermeticity.

**Context:** The script is report-only, `set -euo pipefail`, each probe a small function so tests drive it via stubs/overrides. supermin picks the appliance kernel by version-sort, so we probe **all** `/boot/vmlinuz-*`: if any present one is unreadable, `note_fail`. If none exist, skip (unusual `/boot` layout must not false-fail). The empty-glob case must be handled explicitly — under `set -u`/no-`nullglob`, a non-matching `/boot/vmlinuz-*` glob stays literal, so guard with an existence test, not a bare `for` over the pattern.

- [ ] **Step 1: Write the failing tests**

First, pin **every existing test** in `tests/scripts/test_check_local_libvirt.py` to a controlled boot dir so none read the host `/boot` once the probe exists. Concretely:

- In `test_all_healthy_exits_zero` (currently lines 42-64), before building `env`, add a readable kernel and pass the dir:

```python
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-test").write_text("")  # readable; the new ADR-0222 kernel probe passes
```
and add `"KDIVE_BOOT_DIR": str(boot),` to that test's `env` dict.

- In each of the four failure-path tests (`test_unwritable_install_staging_fails_with_hint`, `test_missing_venv_bindings_fails_with_hint`, `test_missing_kvm_node_fails`, `test_user_not_in_libvirt_group_fails`), add `"KDIVE_BOOT_DIR": str(tmp_path / "boot-empty"),` to their `env` dicts (the dir need not exist — an absent/empty boot dir exercises the probe's skip path, so the kernel probe stays neutral and each test still fails for its intended reason).

Then add the new tests below:

```python
def _healthy_env(tmp_path: Path, bindir: Path, py: Path, boot: Path) -> dict[str, str]:
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    staging = tmp_path / "install-staging"
    staging.mkdir(exist_ok=True)
    return {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "KDIVE_KVM_NODE": str(kvm),
        "KDIVE_PYTHON": str(py),
        "KDIVE_INSTALL_STAGING": str(staging),
        "KDIVE_BOOT_DIR": str(boot),
    }


def _healthy_bin(tmp_path: Path) -> tuple[Path, Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "virsh", 'case "$*" in *net-info*) echo "Active: yes";; esac\nexit 0')
    _stub(bindir, "id", "echo kvm libvirt")
    _stub(bindir, "qemu-system-x86_64", "exit 0")
    _stub(bindir, "qemu-img", "exit 0")
    py = _stub_python(bindir, "venv-python", imports_ok=True)
    return bindir, py


def test_unreadable_host_kernel_fails_with_chmod_hint(tmp_path: Path) -> None:
    bindir, py = _healthy_bin(tmp_path)
    boot = tmp_path / "boot"
    boot.mkdir()
    kernel = boot / "vmlinuz-6.8.0-124-generic"
    kernel.write_text("")
    kernel.chmod(0o600)  # unreadable by a non-owner; but the test runs as owner...
    # Force unreadability deterministically regardless of test UID: strip all read bits.
    kernel.chmod(0o000)

    result = _run(_healthy_env(tmp_path, bindir, py, boot))
    assert result.returncode == 1, result.stdout
    assert "vmlinuz" in result.stderr.lower()
    assert "chmod 0644 /boot/vmlinuz-*" in result.stderr


def test_readable_host_kernel_passes(tmp_path: Path) -> None:
    bindir, py = _healthy_bin(tmp_path)
    boot = tmp_path / "boot"
    boot.mkdir()
    (boot / "vmlinuz-6.8.0-124-generic").write_text("")  # default 0644-ish, readable

    result = _run(_healthy_env(tmp_path, bindir, py, boot))
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stdout.lower()


def test_absent_boot_kernels_skip_probe(tmp_path: Path) -> None:
    bindir, py = _healthy_bin(tmp_path)
    boot = tmp_path / "boot"
    boot.mkdir()  # empty: no vmlinuz-* — probe must skip, not fail on the literal glob

    result = _run(_healthy_env(tmp_path, bindir, py, boot))
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stdout.lower()
```

Note for the implementer: a test running as root would read a `0o000` file, so if the suite ever runs as root this test could false-pass; the project's tests run as a non-root user (CI + dev). If you want a UID-independent unreadable file, place it in a directory with `0o000` mode instead — but the chmod-000 approach matches how the existing `KDIVE_KVM_NODE` tests assume a non-root runner.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run python -m pytest tests/scripts/test_check_local_libvirt.py -q`
Expected: FAIL — `test_unreadable_host_kernel_fails_with_chmod_hint` (script does not yet probe kernels, exits 0), and `test_all_healthy_exits_zero` only if you have NOT yet added `KDIVE_BOOT_DIR` to it. The two skip/readable tests fail too (no probe → but they expect 0, which currently passes; they truly exercise the probe once it exists).

- [ ] **Step 3: Write the minimal implementation**

In `scripts/check-local-libvirt.sh`, add the override near the other `readonly`s:

```bash
# libguestfs builds its supermin appliance from a host kernel under this dir; Debian/Ubuntu
# ship /boot/vmlinuz-* root:0600, unreadable by a non-root worker (ADR-0222, #694). Probe ALL
# present kernels — supermin selects by version-sort, not the running one. Override for tests.
readonly BOOT_DIR="${KDIVE_BOOT_DIR:-/boot}"
```

Add the probe function (near the other `_` probes):

```bash
_host_kernels_readable() {
  local k found=0
  for k in "${BOOT_DIR}"/vmlinuz-*; do
    [[ -e "$k" ]] || continue # no-match glob stays literal under no-nullglob; skip it
    found=1
    [[ -r "$k" ]] || return 1
  done
  ((found)) || return 0 # no kernels present: unusual layout, do not false-fail
  return 0
}
```

Add the call alongside the other build/kdump-specific checks (after the venv-bindings check, before the install-staging check):

```bash
_host_kernels_readable || note_fail \
  "a host kernel under ${BOOT_DIR} (vmlinuz-*) is not readable by this user (libguestfs build-fs appliance, ADR-0222)" \
  "run this preflight as the worker user; if Debian/Ubuntu (root:0600 kernels): sudo chmod 0644 /boot/vmlinuz-* (re-apply after kernel upgrades, or use dpkg-statoverride)"
```

Finally, add `KDIVE_BOOT_DIR` to the all-healthy test's env (Step 1) if not already done.

- [ ] **Step 4: Run the tests + shell lint to verify pass**

Run: `uv run python -m pytest tests/scripts/test_check_local_libvirt.py -q && shellcheck scripts/check-local-libvirt.sh && shfmt -d scripts/check-local-libvirt.sh`
Expected: tests PASS; shellcheck clean; `shfmt -d` prints no diff.

- [ ] **Step 5: Commit**

```bash
git add scripts/check-local-libvirt.sh tests/scripts/test_check_local_libvirt.py
git commit -m "feat(preflight): probe host-kernel readability for build-fs

check-local-libvirt.sh fails with a chmod hint when a /boot/vmlinuz-* kernel
is unreadable by the invoking user, catching the libguestfs appliance failure
before the slow build (ADR-0222, #694). Probes all kernels (supermin selects
by version-sort) and skips cleanly when none are present.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Cross-link the ADR from the walkthrough

**Files:**
- Modify: `docs/operating/providers/local-libvirt-walkthrough.md` (caveat block lines 71-79; preflight note lines 81-92)

**Interfaces:** none (docs only).

**Context:** The walkthrough already documents the chmod fix and the AppArmor/passt note (lines 71-79) and the preflight step (lines 81-92). We add a pointer to ADR-0222 and note that the preflight now flags the kernel case, and that build-fs now reports an actionable error for both. Keep prose plain (no "robust"/"comprehensive"; "Milestone" not "Sprint").

- [ ] **Step 1: Edit the caveat block**

After the existing two bullets in the `> **Debian/Ubuntu libguestfs notes**` block (ending line 79), append a bullet:

```markdown
> - Both failures now report an actionable `configuration_error` from `build-fs` instead of a
>   raw tool dump, and the kernel-readability case is flagged by the preflight (Step 2) when run
>   as the worker user — see [ADR-0222](../../adr/0222-ubuntu-build-fs-libguestfs-diagnostics.md).
```

- [ ] **Step 2: Edit the preflight note**

In the Step 2 preflight paragraph (lines 89-92), after the sentence about the `import guestfs, drgn` check, add:

```markdown
The preflight also flags an unreadable host kernel (`/boot/vmlinuz-*`), which blocks the Step 6
`build-fs` image build on Debian/Ubuntu; fix it with the `chmod` above. Run the preflight as the
worker user, since it checks readability as whoever invokes it.
```

- [ ] **Step 3: Doc guardrails**

Run: `./scripts/check-doc-links.sh && ./scripts/check-doc-paths.sh`
Expected: links resolve; paths valid. Also confirm no style-guard words: `rg -ni 'critical|crucial|essential|significant|comprehensive|robust|elegant|sprint' docs/operating/providers/local-libvirt-walkthrough.md` (expect no NEW hits introduced by your edit).

- [ ] **Step 4: Commit**

```bash
git add docs/operating/providers/local-libvirt-walkthrough.md
git commit -m "docs(local-libvirt): cross-link ADR-0222 build-fs diagnostics

Note that build-fs now reports an actionable configuration_error for the two
Ubuntu 24.04 libguestfs failures and that the preflight flags the unreadable
host kernel (#694).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Full guardrail sweep

**Files:** none (verification only).

- [ ] **Step 1: Run the full CI gate**

Run: `just ci`
Expected: all green (lint, type, lint-shell, lint-ansible, test-ansible, lint-workflows, check-mermaid, docs-links, docs-paths, adr-status-check, docs-check, config-docs-check, config-guard, env-docs-check, resources-docs-check, chart-version-check, test). The `live_vm` suite stays skipped (no KVM).

- [ ] **Step 2: If anything fails**, fix in the task it belongs to and re-run. Do not commit with red guardrails.

## Self-Review

- **Spec coverage:** ADR Decision §1 → Task 1; §2 → Task 2; §3 → Task 3; the no-KVM/test-boundary + anchoring + nullglob constraints → Global Constraints + Task 1 negative test + Task 2 absent-boot test. All covered.
- **Placeholder scan:** every code/step block is concrete (regexes, full functions, exact test bodies, exact commands). No TBD/TODO.
- **Type consistency:** `_remediation_for_stderr` returns `tuple[str, str] | None`, consumed only inside `run_guestfs_tool`; `details["remediation"]` key asserted in Task 1 tests and produced in Task 1 impl; `KDIVE_BOOT_DIR`/`BOOT_DIR` and `_host_kernels_readable` consistent across Task 2 test + impl.
