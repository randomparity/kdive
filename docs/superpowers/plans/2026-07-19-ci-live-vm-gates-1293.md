# CI live-VM gates (epic #1289 sub-issue D, #1293) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the inert `workflow_dispatch`-only `live-vm` CI job into two real gates — a hosted TCG spine gate and a self-hosted native `live_vm` gate — backed by a family-keyed fail-loud env preflight, with no fork-PR exposure.

**Architecture:** A new `.github/workflows/live.yml` (separate from `ci.yml` so a `schedule` trigger does not fan out to every job, and so it can carry no `pull_request` trigger at all) holds two jobs: `tcg` (hosted `ubuntu-latest`, three core `live_vm_tcg` proofs over the compose stack) and `native` (`[self-hosted, kvm, x64]`, both native families). A `scripts/live-vm/preflight-env.sh` fails the job loud when a declared family's env is absent. The self-hosted job reuses `/opt/kdive`'s libguestfs venv as the interpreter (`KDIVE_PYTHON`) with a `PYTHONPATH` source overlay — which requires teaching the reused live-stack scripts to honor `KDIVE_PYTHON`.

**Tech Stack:** GitHub Actions (YAML), Bash (the `scripts/live-vm/lib.sh` `die`/`require_*` idiom), Python 3.14 / pytest (behavioral tests, subprocess-source pattern), Ansible (`live_vm_host` role), `uv`, `just`.

## Global Constraints

- **Authoritative design:** [`docs/superpowers/specs/2026-07-19-ci-live-vm-gates-1293-design.md`](../specs/2026-07-19-ci-live-vm-gates-1293-design.md) and [ADR-0389](../../adr/0389-ci-live-vm-gates.md). Do not re-decide anything settled there.
- **This is CI/test infra only** — no product code, no database migration.
- **Replace, don't deprecate:** the inert `live-vm` job is *removed* from `ci.yml`; the real jobs live in `live.yml`.
- **Fork-PR safety is three layers:** (1) `live.yml` has **no `pull_request` trigger**; (2) the `native` job's `if:` is a **positive** `github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'` allowlist (NOT `!= 'pull_request'`, which admits `push`); (3) the repo "require approval for outside collaborators" setting (operator/runbook).
- **Concurrency:** two **distinct per-job** groups, both `cancel-in-progress: false` (never kill a self-hosted boot mid-flight).
- **Interpreter contract:** the self-hosted job never `uv sync`s in `$GITHUB_WORKSPACE` and never mutates `/opt/kdive`; it uses `KDIVE_PYTHON=/opt/kdive/.venv/bin/python` + `PYTHONPATH=$GITHUB_WORKSPACE/src`.
- **Env continuity:** each job's stage → map → preflight → run steps are **one `run:` block** (Actions runs each `run:` in a fresh shell).
- **Doc-style guard:** no "Sprint"; plain, factual prose (no "critical/robust/comprehensive/elegant") in code comments, commit messages, YAML comments.
- **Guardrails (run before every commit that touches the relevant surface):** `just lint-shell` (shellcheck+shfmt), `just lint-workflows` (actionlint+zizmor), `just lint-ansible`, `just test`, and the full `just ci` before push. Line length 100; ruff lint set `E,F,I,UP,B,SIM`; `ty` strict whole-tree.
- **Pinned actions only** (zizmor enforces SHA-pinning); reuse the exact action SHAs already in `ci.yml` (`actions/checkout@9c091bb…`, `astral-sh/setup-uv@11f9893b…`, `extractions/setup-just@53165ef7…`).

---

### Task 1: Teach the live-stack scripts to honor `KDIVE_PYTHON`

The self-hosted job's whole interpreter model depends on the reused stack bring-up running the worker under `/opt/kdive`'s libguestfs venv. Today `scripts/live-stack/lib.sh` hardcodes the workspace `.venv` and `onboard.sh` uses `uv run python`, both ignoring `KDIVE_PYTHON`. This is the foundational, independently-testable change; do it first.

**Files:**
- Modify: `scripts/live-stack/lib.sh:8`
- Modify: `scripts/live-stack/onboard.sh:47,51,60,73`
- Test: `tests/scripts/test_live_stack_interpreter.py` (create)

**Interfaces:**
- Produces: the invariant that `scripts/live-stack/lib.sh`'s `$py` equals `$KDIVE_PYTHON` when set, else `${repo_root}/.venv/bin/python` (default unchanged). Task 3 (`mint-system.sh`) and Task 5 (`live.yml` native job) rely on this.

- [ ] **Step 1: Write the failing behavioral test**

Create `tests/scripts/test_live_stack_interpreter.py` (subprocess-source `lib.sh`, the pattern C's `tests/scripts/test_live_vm_stores.py` uses):

```python
"""lib.sh resolves its interpreter from KDIVE_PYTHON when set (#1293, ADR-0389).

The self-hosted live_vm job runs the stack's worker under /opt/kdive's libguestfs venv via
KDIVE_PYTHON; lib.sh must honor it, else the worker's guestfs import fails. The default (KDIVE_PYTHON
unset) must stay the workspace .venv so operator use is unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "live-stack" / "lib.sh"


def _resolved_py(env_kdive_python: str | None) -> str:
    prelude = f'export KDIVE_PYTHON="{env_kdive_python}"\n' if env_kdive_python is not None else ""
    script = f'{prelude}source "{_LIB}"\nprintf "%s" "$py"\n'
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    return out.stdout


def test_py_honors_kdive_python_when_set() -> None:
    assert _resolved_py("/opt/kdive/.venv/bin/python") == "/opt/kdive/.venv/bin/python"


def test_py_defaults_to_workspace_venv_when_unset() -> None:
    assert _resolved_py(None).endswith("/.venv/bin/python")
    assert "/opt/kdive/" not in _resolved_py(None)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_live_stack_interpreter.py -q`
Expected: `test_py_honors_kdive_python_when_set` FAILS (current `$py` ignores `KDIVE_PYTHON`).

- [ ] **Step 3: Edit `lib.sh` to honor `KDIVE_PYTHON`**

In `scripts/live-stack/lib.sh`, change line 8 from:

```bash
py="${repo_root}/.venv/bin/python"
```

to (mirroring `scripts/live-vm/lib.sh:169`'s `py="${KDIVE_PYTHON:-python3}"` precedent):

```bash
# KDIVE_PYTHON overrides the interpreter (the #1293 self-hosted job points it at /opt/kdive's
# libguestfs venv); unset, it stays the workspace .venv so operator use is unchanged.
py="${KDIVE_PYTHON:-${repo_root}/.venv/bin/python}"
```

- [ ] **Step 4: Edit `onboard.sh`'s four `uv run python` sites**

In `scripts/live-stack/onboard.sh`, add after line 29 (`cd "$repo_root"`):

```bash
# KDIVE_PYTHON overrides the interpreter (the #1293 self-hosted job points it at /opt/kdive's
# libguestfs venv); unset, fall back to `uv run python` (the operator dev-loop default).
py=("${KDIVE_PYTHON:+"$KDIVE_PYTHON"}")
[[ ${#py[@]} -eq 0 ]] && py=(uv run python)
```

Then replace each `uv run python` invocation (lines ~47, 51, 60, 73) with `"${py[@]}"`. For example line 47 `uv run python -m kdive migrate` becomes `"${py[@]}" -m kdive migrate`, and the heredoc `uv run python - "$PROJECT" ...` becomes `"${py[@]}" - "$PROJECT" ...`.

- [ ] **Step 5: Run the test + shellcheck to verify green**

Run: `uv run python -m pytest tests/scripts/test_live_stack_interpreter.py -q && just lint-shell`
Expected: both tests PASS; shellcheck/shfmt clean.

- [ ] **Step 6: Commit**

```bash
git add scripts/live-stack/lib.sh scripts/live-stack/onboard.sh tests/scripts/test_live_stack_interpreter.py
git commit -m "feat(1293): honor KDIVE_PYTHON in the live-stack scripts"
```

---

### Task 2: The fail-loud family env preflight

`scripts/live-vm/preflight-env.sh <family…>` asserts each declared family's required env is present and **fails the job** (never `pytest.skip`) when it is not.

**Files:**
- Create: `scripts/live-vm/preflight-env.sh`
- Test: `tests/scripts/test_live_vm_preflight.py` (create)

**Interfaces:**
- Consumes: `scripts/live-vm/lib.sh`'s `die` + `require_tools` (source it).
- Produces: CLI `preflight-env.sh throwaway|provisioned|tcg [more…]`; exit 0 iff every requested family's env resolves, else non-zero with a message naming the first missing var. Task 5's `live.yml` calls it.

- [ ] **Step 1: Write the failing behavioral test**

Create `tests/scripts/test_live_vm_preflight.py`:

```python
"""preflight-env.sh fails loud on a declared family's missing env (#1293, ADR-0389).

A declared family with absent env must FAIL the job (non-zero, names the missing var) — never a
green skip. Mirrors the subprocess-invocation pattern of tests/scripts/test_live_vm_stores.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live-vm" / "preflight-env.sh"


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full = {"PATH": os.environ["PATH"], **env}
    return subprocess.run(
        ["bash", str(_SCRIPT), *args], capture_output=True, text=True, env=full
    )


def test_throwaway_ok_when_rootfs_exists(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"x")
    r = _run(["throwaway"], {"KDIVE_LIVE_VM_ROOTFS": str(rootfs), "KDIVE_LIBVIRT_URI": "qemu:///session"})
    assert r.returncode == 0, r.stderr


def test_throwaway_fails_when_rootfs_missing() -> None:
    r = _run(["throwaway"], {"KDIVE_LIBVIRT_URI": "qemu:///session"})
    assert r.returncode != 0
    assert "KDIVE_LIVE_VM_ROOTFS" in r.stderr


def test_provisioned_fails_without_system_id() -> None:
    r = _run(["provisioned"], {"KDIVE_S3_ENDPOINT_URL": "http://x", "KDIVE_S3_BUCKET": "b"})
    assert r.returncode != 0
    assert "KDIVE_LIVE_VM_SYSTEM_ID" in r.stderr


def test_tcg_fails_without_ppc64le_emulator(tmp_path: Path) -> None:
    img = tmp_path / "img.qcow2"
    img.write_bytes(b"x")
    tree = tmp_path / "linux"
    tree.mkdir()
    env = {
        "KDIVE_STACK_BASE_URL": "http://x", "KDIVE_OIDC_ISSUER": "http://x",
        "KDIVE_DATABASE_URL": "postgresql://x", "KDIVE_S3_ENDPOINT_URL": "http://x",
        "KDIVE_S3_BUCKET": "b", "AWS_ACCESS_KEY_ID": "k", "AWS_SECRET_ACCESS_KEY": "s",
        "KDIVE_GUEST_IMAGE_PPC64LE": str(img), "KDIVE_KERNEL_SRC": str(tree),
        "PATH": "/nonexistent",  # no qemu-system-ppc64
    }
    r = _run(["tcg"], env)
    assert r.returncode != 0
    assert "qemu-system-ppc64" in r.stderr


def test_unknown_family_fails_loud() -> None:
    r = _run(["bogus"], {})
    assert r.returncode != 0
    assert "bogus" in r.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_live_vm_preflight.py -q`
Expected: FAIL — `preflight-env.sh` does not exist yet.

- [ ] **Step 3: Write `preflight-env.sh`**

Create `scripts/live-vm/preflight-env.sh` (mode 0755):

```bash
#!/usr/bin/env bash
# Fail-loud env preflight for the live_vm CI gates (#1293, ADR-0389). Given one or more DECLARED
# families, assert each family's required env is present and FAIL the job (never a green skip) when
# it is not. Reuses the scripts/live-vm/lib.sh die/require_* idiom.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-vm/lib.sh
source "${here}/lib.sh"

[ "$#" -ge 1 ] || die "usage: preflight-env.sh <throwaway|provisioned|tcg> [more...]"

require_set() { # NAME: die unless the named env var is non-empty.
  local name="$1"
  [ -n "${!name:-}" ] || die "required env ${name} is unset for the declared family"
}
require_file() { # NAME: die unless the named env var points at an existing file.
  local name="$1"
  require_set "$name"
  [ -e "${!name}" ] || die "${name}=${!name} does not exist"
}

check_throwaway() {
  require_file KDIVE_LIVE_VM_ROOTFS
  require_set KDIVE_LIBVIRT_URI
}
check_provisioned() {
  # Teeth over A's resolver: A returns AVAILABLE on endpoint+bucket alone, so a declared family with
  # no minted System skips green. The AWS_* creds are the on-box MinIO minioadmin default (env.sh),
  # so a credential-absence check is vacuous here — the System id is the real assertion.
  require_set KDIVE_LIVE_VM_SYSTEM_ID
  require_set KDIVE_S3_ENDPOINT_URL
  require_set KDIVE_S3_BUCKET
}
check_tcg() {
  require_set KDIVE_STACK_BASE_URL
  require_set KDIVE_OIDC_ISSUER
  require_set KDIVE_DATABASE_URL
  require_set KDIVE_S3_ENDPOINT_URL
  require_set KDIVE_S3_BUCKET
  require_set AWS_ACCESS_KEY_ID   # teeth here: the tcg job exports these explicitly (no env.sh)
  require_set AWS_SECRET_ACCESS_KEY
  require_file KDIVE_GUEST_IMAGE_PPC64LE
  require_file KDIVE_KERNEL_SRC
  command -v qemu-system-ppc64 >/dev/null 2>&1 ||
    die "qemu-system-ppc64 not on PATH: the ppc64le TCG guest cannot boot"
}

for family in "$@"; do
  case "$family" in
  throwaway) check_throwaway ;;
  provisioned) check_provisioned ;;
  tcg) check_tcg ;;
  *) die "unknown family '${family}' (expected throwaway|provisioned|tcg)" ;;
  esac
done
echo "live_vm preflight: all declared families ($*) have their required env" >&2
```

- [ ] **Step 4: Run the test + shellcheck to verify green**

Run: `uv run python -m pytest tests/scripts/test_live_vm_preflight.py -q && just lint-shell`
Expected: all PASS; shellcheck/shfmt clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/live-vm/preflight-env.sh tests/scripts/test_live_vm_preflight.py
git commit -m "feat(1293): add the fail-loud live_vm family env preflight"
```

---

### Task 3: `mint-system.sh` — stand up the provisioned System

Mints the System the provisioned-System family needs, so `KDIVE_LIVE_VM_SYSTEM_ID` can be exported before pytest. Reuses `onboard.sh` for funding + token, then drives allocate → provision → poll-ready over the MCP HTTP transport.

**Files:**
- Create: `scripts/live-vm/mint-system.sh`
- Test: `tests/scripts/test_mint_system.py` (create — argument/precondition validation; the live mint is proven by the operator nightly, not CI)

**Interfaces:**
- Consumes: `scripts/live-stack/onboard.sh` (funding + `KDIVE_TOKEN`), `KDIVE_LIVE_VM_ROOTFS` (the warm rootfs), `KDIVE_STACK_BASE_URL`. Reference the lifecycle pattern in `scripts/live-debug.py:_provision_boot_run` and the generic `kdivectl tool call` passthrough (`src/kdive/cli/__main__.py`).
- Produces: on success, prints the ready System id as the sole stdout line (captured into `KDIVE_LIVE_VM_SYSTEM_ID`); fails loud otherwise.

- [ ] **Step 1: Write the failing precondition test**

Create `tests/scripts/test_mint_system.py`:

```python
"""mint-system.sh validates its preconditions before any stack call (#1293, ADR-0389).

The live mint (allocate -> provision -> ready) needs a running stack and is proven by the operator
nightly, not CI. This test pins the fail-loud preconditions: an absent warm rootfs or stack URL dies
before any HTTP call, so a misconfigured job fails at the boundary, not deep in provisioning.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live-vm" / "mint-system.sh"


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT)], capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], **env},
    )


def test_dies_without_rootfs() -> None:
    r = _run({"KDIVE_STACK_BASE_URL": "http://127.0.0.1:8000"})
    assert r.returncode != 0
    assert "KDIVE_LIVE_VM_ROOTFS" in r.stderr


def test_dies_without_stack_url(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"x")
    r = _run({"KDIVE_LIVE_VM_ROOTFS": str(rootfs)})
    assert r.returncode != 0
    assert "KDIVE_STACK_BASE_URL" in r.stderr
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_mint_system.py -q`
Expected: FAIL — script does not exist.

- [ ] **Step 3: Write `mint-system.sh` (preconditions + mint sequence)**

Create `scripts/live-vm/mint-system.sh` (mode 0755). The preconditions block must satisfy the test; the mint sequence follows `scripts/live-debug.py:_provision_boot_run` (invoke it, or `kdivectl tool call`, with the funded token). Skeleton:

```bash
#!/usr/bin/env bash
# Mint one provisioned System for the self-hosted live_vm provisioned family (#1293, ADR-0389).
# Order: fund/onboard a project (onboard.sh) -> allocate -> provision from the warm rootfs -> poll
# ready -> print the System id (the sole stdout line, captured into KDIVE_LIVE_VM_SYSTEM_ID).
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-vm/lib.sh
source "${here}/lib.sh"

[ -n "${KDIVE_LIVE_VM_ROOTFS:-}" ] || die "KDIVE_LIVE_VM_ROOTFS unset (the warm rootfs to provision)"
[ -e "${KDIVE_LIVE_VM_ROOTFS}" ] || die "KDIVE_LIVE_VM_ROOTFS=${KDIVE_LIVE_VM_ROOTFS} does not exist"
[ -n "${KDIVE_STACK_BASE_URL:-}" ] || die "KDIVE_STACK_BASE_URL unset (bring up the stack first)"

# 1. Fund the project + mint a token (reuse onboard.sh; it prints `export KDIVE_TOKEN=...`).
eval "$("${here}/../live-stack/onboard.sh")"
[ -n "${KDIVE_TOKEN:-}" ] || die "onboard.sh did not mint a token"

# 2. allocate -> provision (arch=native) from KDIVE_LIVE_VM_ROOTFS -> poll systems.get until ready,
#    then print the System id. Drive the lifecycle over MCP HTTP; the reference is
#    scripts/live-debug.py:_provision_boot_run (investigation -> allocation -> provision), reused with
#    KDIVE_TOKEN and KDIVE_STACK_BASE_URL. Implement with `${py[@]:-uv run python}` (honoring
#    KDIVE_PYTHON) invoking a small in-repo helper or `kdivectl tool call`, and echo ONLY the id.
system_id="$( ... )"   # implementer fills the lifecycle per the reference above
[ -n "$system_id" ] || die "provisioning did not yield a ready System id"
printf '%s\n' "$system_id"
```

Keep stdout to the id alone (progress → stderr), so `KDIVE_LIVE_VM_SYSTEM_ID="$(mint-system.sh)"` is clean. Honor `KDIVE_PYTHON` for any Python invocation (Task 1's pattern).

- [ ] **Step 4: Run the test + shellcheck to verify green**

Run: `uv run python -m pytest tests/scripts/test_mint_system.py -q && just lint-shell`
Expected: PASS; shellcheck/shfmt clean. (The live mint path is exercised in Task 7's local proof / the operator nightly, not here.)

- [ ] **Step 5: Commit**

```bash
git add scripts/live-vm/mint-system.sh tests/scripts/test_mint_system.py
git commit -m "feat(1293): add mint-system.sh for the provisioned-System family"
```

---

### Task 4: Declare `docker` + compose as a `live_vm_host` dependency

The self-hosted job's on-box stack bring-up (`up.sh`) needs `docker` + the compose plugin, which B's roles never provision. Parity convention: declare it in the owning role in the same change.

**Files:**
- Modify: `deploy/ansible/roles/live_vm_host/tasks/main.yml` (add a docker install task)
- Modify: `deploy/ansible/roles/live_vm_host/defaults/main.yml` if a package-list var fits the role's existing style

**Interfaces:**
- Produces: a reprovisioned `live_vm_runners` host has `docker` + `docker compose` (the plugin) and the runner user in the `docker` group.

- [ ] **Step 1: Read the role's existing package-install idiom**

Run: `sed -n '1,80p' deploy/ansible/roles/live_vm_host/tasks/main.yml`
Note how it installs the debug toolchain (apt module, OS-branch, package-list var) so the docker task matches that style exactly — do not invent a new pattern.

- [ ] **Step 2: Add the docker install task**

Append a task to `deploy/ansible/roles/live_vm_host/tasks/main.yml` matching the role's style, e.g.:

```yaml
- name: Install Docker Engine + compose plugin (the #1293 self-hosted job's on-box stack)
  ansible.builtin.apt:
    name:
      - docker.io
      - docker-compose-v2
    state: present
    update_cache: true

- name: Add the runner service account to the docker group
  ansible.builtin.user:
    name: "{{ github_runner_user }}"
    groups: docker
    append: true
```

(Confirm the compose-plugin package name for the runner's Ubuntu 26.04 base; `docker-compose-v2` provides `docker compose`. Match the role's existing arch/OS branching if it has any.)

- [ ] **Step 3: Lint the role**

Run: `just lint-ansible`
Expected: clean (ansible-lint + yamllint). Note: `just test-ansible` does **not** exercise `live_vm_host`, so this task's live verification is the operator's idempotent `runner.yml` re-run, not CI (state this in the commit body).

- [ ] **Step 4: Commit**

```bash
git add deploy/ansible/roles/live_vm_host/
git commit -m "feat(1293): provision docker+compose on the live_vm runner host"
```

---

### Task 5: `live.yml` workflow + remove the inert `ci.yml` job + shape guard

The two real gates, plus the workflow-shape guard test that pins the security + cleanup posture at the source.

**Files:**
- Create: `.github/workflows/live.yml`
- Modify: `.github/workflows/ci.yml` (remove the `live-vm` job, lines 218-245)
- Test: `tests/scripts/test_live_workflow_shape.py` (create)

**Interfaces:**
- Consumes: `scripts/live-vm/{preflight-env.sh,mint-system.sh,warm-store.sh,stage-tcg-images.sh}`, `scripts/live-stack/up.sh`, `scripts/fetch-kernel-tree.sh`, Task 1's `KDIVE_PYTHON` override.
- Produces: nothing downstream in this plan; the shape test guards it.

- [ ] **Step 1: Write the failing shape guard test**

Create `tests/scripts/test_live_workflow_shape.py`:

```python
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
    # PyYAML parses the bare `on:` key as boolean True; read it back under that key.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_live_yml_has_no_pull_request_trigger() -> None:
    triggers = _load(_LIVE)[True]  # `on:` -> True under PyYAML
    assert "pull_request" not in triggers
    assert "pull_request_target" not in triggers


def test_native_job_uses_positive_event_allowlist() -> None:
    native = _load(_LIVE)["jobs"]["native"]
    cond = native["if"]
    assert "schedule" in cond and "workflow_dispatch" in cond
    assert "!=" not in cond  # a != 'pull_request' guard would admit push — forbidden


def test_both_jobs_disable_cancel_in_progress() -> None:
    jobs = _load(_LIVE)["jobs"]
    for name in ("tcg", "native"):
        assert jobs[name]["concurrency"]["cancel-in-progress"] is False


def test_ci_yml_no_longer_defines_a_live_vm_job() -> None:
    assert "live-vm" not in _load(_CI)["jobs"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_live_workflow_shape.py -q`
Expected: FAIL — `live.yml` absent; `ci.yml` still has `live-vm`.

- [ ] **Step 3: Remove the inert `live-vm` job from `ci.yml`**

Delete lines 218-245 of `.github/workflows/ci.yml` (the entire `live-vm:` job, from `  live-vm:` through the `run: uv run python -m pytest -m live_vm -q` step). Leave the surrounding jobs untouched.

- [ ] **Step 4: Write `.github/workflows/live.yml`**

Create `.github/workflows/live.yml` per the spec. Skeleton (fill the step bodies from the spec's Job 1/Job 2 step lists; keep each job's stage→map→preflight→run as **one `run:` block**; pin action SHAs from `ci.yml`):

```yaml
name: live-vm gates

on:
  schedule:
    - cron: "0 7 * * *"   # nightly ~07:00 UTC
  workflow_dispatch:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  tcg:
    name: live_vm_tcg (hosted)
    if: github.event_name != 'pull_request'
    runs-on: ubuntu-latest
    timeout-minutes: 30   # Task 7 sets this from the measured wall-time (ceil(x1.5), floor 30)
    concurrency:
      group: live-tcg-${{ github.ref }}
      cancel-in-progress: false
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
        with:
          persist-credentials: false
      # host deps: qemu-system-ppc, libguestfs-tools, e2fsprogs, elfutils, debuginfod
      # uv sync --locked --group live
      # ONE run: block — stack up -> stage-tcg-images.sh (eval) -> map to KDIVE_GUEST_IMAGE_PPC64LE
      #   + KDIVE_KERNEL_SRC + stack/S3/AWS_* env -> preflight-env.sh tcg -> just test-live-tcg

  native:
    name: live_vm (self-hosted KVM)
    if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'
    runs-on: [self-hosted, kvm, x64]
    timeout-minutes: 90
    concurrency:
      group: live-native-${{ github.ref }}
      cancel-in-progress: false
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
        with:
          persist-credentials: false
      # ONE run: block with KDIVE_PYTHON=/opt/kdive/.venv/bin/python, PYTHONPATH=$GITHUB_WORKSPACE/src:
      #   reaper (virsh destroy kdive-* ; docker compose down -v) -> eval warm-store.sh ->
      #   KDIVE_WORKER_AS_ROOT=0 up.sh --skip-obs -> KDIVE_LIVE_VM_SYSTEM_ID="$(mint-system.sh)" ->
      #   preflight-env.sh throwaway provisioned -> just test-live
```

Reference `ci.yml`'s existing `live-vm` job (before removal) for the uv/checkout step shapes and the libvirt-dev note.

- [ ] **Step 5: Run the shape test + workflow linters**

Run: `uv run python -m pytest tests/scripts/test_live_workflow_shape.py -q && just lint-workflows`
Expected: shape test PASS; actionlint + zizmor clean (SHA-pinned actions, least-privilege `permissions`, no `pull_request_target`).

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/live.yml .github/workflows/ci.yml tests/scripts/test_live_workflow_shape.py
git commit -m "feat(1293): add live.yml gates; remove the inert ci.yml live-vm job"
```

---

### Task 6: Env docs + full guardrail sweep

Close any env-docs gap and confirm the whole PR gate is green.

**Files:**
- Modify: `src/kdive/config/external_env.py` (only if a new `KDIVE_*` token appears in the tree)
- Modify: operator CI notes (a short section in the live-stack runbook or `AGENTS.md` pointer, per the spec's "operator CI notes")

- [ ] **Step 1: Find any undocumented `KDIVE_*` the change introduced**

Run: `just env-docs-check`
Expected: PASS, or a message naming an undocumented token. The vars the scripts *read* (`KDIVE_LIVE_VM_*`, `KDIVE_S3_*`, `KDIVE_STACK_BASE_URL`, `KDIVE_GUEST_IMAGE_PPC64LE`, `KDIVE_KERNEL_SRC`, `KDIVE_TCG_*`, `KDIVE_WORKER_AS_ROOT`, `KDIVE_PYTHON`) are already documented; if the guard flags one, add an `ExternalEnvVar(...)` entry in `external_env.py` matching the surrounding style (name, `"script"`/`"test"` scope, default, one-line description citing #1293).

- [ ] **Step 2: Add the operator CI note**

Add a short subsection to `docs/operating/runbooks/live-stack.md` (or the self-hosted-kvm-runner runbook) stating: enable `github_runner_service_enabled: true` only after `live.yml` is merged **and** the "require approval for outside collaborators" repo setting is applied; the two live gates run nightly + `workflow_dispatch`. Keep it factual (doc-style guard).

- [ ] **Step 3: Run the full PR gate**

Run: `just ci`
Expected: every recipe green (lint, type, lint-shell, lint-ansible, test-ansible, lint-workflows, docs-check, env-docs-check, adr-status-check, …).

- [ ] **Step 4: Commit**

```bash
git add -- src/kdive/config/external_env.py docs/operating/runbooks/ AGENTS.md
git commit -m "docs(1293): document live-gate env + operator enabling order"
```

---

### Task 7: Local TCG live proof — measure the timeout, flip the ADR to Accepted

Grounds Decision 1's `timeout-minutes` in a real number and moves ADR-0389 from Proposed to Accepted. On this x86_64 host, ppc64le is foreign → TCG, so the hosted-gate boot path is reproducible locally.

**Files:**
- Modify: `.github/workflows/live.yml` (the `tcg` job's `timeout-minutes`)
- Modify: `docs/adr/0389-ci-live-vm-gates.md` (record the measured wall-time; Status Proposed → Accepted)
- Modify: `docs/adr/README.md` (Status column → Accepted)
- Modify: `docs/superpowers/specs/2026-07-19-ci-live-vm-gates-1293-design.md` (record the number)

- [ ] **Step 1: Stand up the stack + emulator + TCG image set locally**

Bring up the compose stack (`just stack-up`), ensure `qemu-system-ppc64` is installed, set `KDIVE_TCG_IMAGE` + `DEBUGINFOD_URLS`, and stage the set (`eval "$(scripts/live-vm/stage-tcg-images.sh)"`). Map `KDIVE_GUEST_IMAGE_PPC64LE`, `KDIVE_KERNEL_SRC` (`scripts/fetch-kernel-tree.sh`), and the stack/S3 env, matching the `live.yml` `tcg` block.

- [ ] **Step 2: Time the run**

Run: `time just test-live-tcg`
Record the wall-clock of the three core proofs (skip the bundle proof — `KDIVE_PPC64LE_BUNDLE` unset). If the run cannot complete locally (emulator/host limits), record that and set a conservative default (state it explicitly), per the CI-cannot-always-prove-it-live posture A/B/C shipped.

- [ ] **Step 3: Set the timeout from the measurement**

Set the `tcg` job's `timeout-minutes` = `ceil(measured_minutes × 1.5)`, floored at 30. Update `live.yml`.

- [ ] **Step 4: Record the number + flip the ADR to Accepted**

In `docs/adr/0389-ci-live-vm-gates.md`, replace the "Measured wall-time" placeholder with the measured value + the resulting `timeout-minutes`, and change Status `Proposed` → `Accepted`. Update the same row's Status in `docs/adr/README.md`. Add the number to the spec's Testing section.

- [ ] **Step 5: Verify the doc + workflow guards**

Run: `just adr-status-check && just lint-workflows`
Expected: ADR status guard reports the index in sync (Accepted matches the row); actionlint/zizmor clean.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/live.yml docs/adr/0389-ci-live-vm-gates.md docs/adr/README.md docs/superpowers/specs/2026-07-19-ci-live-vm-gates-1293-design.md
git commit -m "test(1293): record the measured TCG wall-time; accept ADR-0389"
```

---

## Notes for the implementer

- **Ordering:** Task 1 → 2 → 3 → 4 → 5 → 6 → 7. Tasks 1–4 are independent-ish and each land green on ordinary CI; Task 5 depends on 2+3 (it calls their scripts); Task 7 depends on 5 (it edits `live.yml`) and needs a live host.
- **The live behavior is not provable in ordinary hosted PR CI** — that is by design. Ordinary CI proves the shell/workflow/preflight *shape* (Tasks 1–6); the boot behavior is Task 7's local proof + the operator nightly on the enabled runner.
- **Do not enable the runner service** as part of this PR. Enabling is the operator's step after merge + the repo approval setting (Task 6's note).
- **Rollback:** deleting `live.yml` disables both gates; the scripts/tests/role-edit are inert without it. No migration, no data.
