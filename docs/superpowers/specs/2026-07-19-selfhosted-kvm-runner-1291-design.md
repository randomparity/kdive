# Self-hosted Rocky 10 KVM runner, codified (epic #1289, sub-issue B)

- **Date:** 2026-07-19
- **Status:** Draft
- **Issue:** [#1291](https://github.com/randomparity/kdive/issues/1291)
- **Epic:** [#1289](https://github.com/randomparity/kdive/issues/1289) · epic spec
  [`docs/design/2026-07-18-live-test-framework.md`](../../design/2026-07-18-live-test-framework.md)
- **ADR:** [0387 — self-hosted KVM runner host codification](../../adr/0387-selfhosted-kvm-runner-host-codification.md)
  (this sub-issue implements it)
- **Depends on:** sub-issue A ([#1290](https://github.com/randomparity/kdive/issues/1290),
  PR #1297, merged) — the `live_vm` environment contract this host is built to satisfy.

## Problem

The `live_vm` native-KVM tier needs a real host to run on: Rocky Linux 10 with
libvirt, qemu, the kernel-debug toolchain, and a registered GitHub Actions
runner. Today that host is tribal knowledge — no reproducible build, so a fresh
runner (or the north-star ppc64le POWER runner) is a from-memory bring-up.

Sub-issue A fixed the *test-side* environment contract in code
(`tests/live_vm/__init__.py`: the `KDIVE_LIVE_VM_*` / `KDIVE_S3_*` resolvers and
`require_live_vm_*` gates). This sub-issue builds the *host side* of that
contract as reproducible automation, so a runner host is codified, not
remembered.

The contract the host must satisfy, restated from the epic spec's "environment
contract" section and enforced by `tests/live_vm/__init__.py`:

- **KVM + native qemu:** `/dev/kvm` readable/writable by the runner user; the
  host's native `qemu-system-<arch>` present.
- **Modular libvirt daemons:** `virtqemud` / `virtnetworkd` (RHEL-family model).
- **`default` storage pool + network:** active and autostarted.
- **Kernel-debug toolchain:** `drgn`, `crash`, `makedumpfile`, `kexec-tools`,
  `kdump-utils`, `gdb`, plus the foreign qemu emulator per non-native arch (TCG).
- **Staged rootfs location:** a writable, **world-traversable** directory (never
  `$HOME`, whose `0700` mode hides it from the qemu user) that carries the
  SELinux `virt_image_t` label so system-mode staged images boot.
- **Short `XDG_RUNTIME_DIR`:** `/run/user/<uid>` present for the runner user, so
  session-mode QEMU's QMP socket path stays under the length limit (#1258).
- **Object-store env pointer for the provisioned-System family:** the runner
  service carries `KDIVE_SECRETS_ROOT` (and the `KDIVE_S3_*` values ride repo/org
  secrets into the workflow). B provisions only the *pointer* and the secrets
  directory; **placing the actual credential files** under `KDIVE_SECRETS_ROOT`
  is owned by sub-issue C/D or the operator (see "B does not own" below).
  **Important correction to a tempting assumption:** sub-issue A's resolver
  (`require_live_vm_provisioned`, `tests/live_vm/__init__.py`) validates only the
  `KDIVE_S3_ENDPOINT_URL` + `KDIVE_S3_BUCKET` *env* — it explicitly does **not**
  check the credential *material* under `KDIVE_SECRETS_ROOT` (its docstring: "S3
  credentials are NOT env vars … out of this resolver's env scope"). So a runner
  with the S3 env set but an empty secrets dir resolves `AVAILABLE`, not
  misconfigured. Catching missing credential material is therefore **not** A's
  job; it belongs to D's declared-family fail-loud preflight (or the ADR-0089
  worker-boundary secrets loader, which errors when it resolves a referenced
  secret that is absent). D must not be built on the false premise that A's
  resolver fails loud on missing credentials — this spec makes that D dependency
  explicit rather than assuming a guarantee A does not provide.

And the runner-registration requirements from the issue:

- A GitHub Actions runner registered with label `[self-hosted, kvm, <arch>]`.
- Runs only on **trusted events** (`schedule` + `workflow_dispatch`), never fork
  pull requests — self-hosted + fork PR is arbitrary code execution on the host.
- `KDIVE_S3_*` repository/organization secrets available to scheduled runs.

## Goals

1. A reproducible, **arch-parameterized** host build (Rocky 10 x86_64 now; a
   `[self-hosted, kvm, ppc64le]` POWER runner a documented drop-in) that reaches
   the contract above.
2. Codified as Ansible roles under the existing `deploy/ansible` tree, reusing
   what already fits, so it inherits the repo's `ansible-lint` / `test-ansible`
   guardrails.
3. GitHub Actions runner registration codified: arch-selected asset + label,
   installed as a non-root systemd service, fail-closed on a missing token.
4. A runbook and the trusted-events / secrets security posture, so an operator
   can stand up a runner from the automation rather than from memory.

## Non-goals

- **The `live-vm` CI job itself** — matrix, `schedule` trigger wiring, the
  fail-loud env preflight, the test invocation — is sub-issue D. This sub-issue
  delivers the host + runner + runbook + secrets model that D wires a job onto.
- **Standing up the ppc64le runner.** Out of scope for this phase (epic non-goal).
  The obligation here is that nothing blocks it; see the arch seam below.
- **Guest-image / debuginfo provisioning** (the warm store) — sub-issue C. This
  host build creates the *staging directory and its SELinux label*; C stages the
  images into it.
- **Populating the S3 credential files** under `KDIVE_SECRETS_ROOT` (the
  file-based secrets the provisioned-System family reads). B provisions the
  `KDIVE_SECRETS_ROOT` pointer and directory and wires the `KDIVE_S3_*` secrets
  model; the credential *material* is placed by sub-issue C/D or the operator per
  the runbook. B's gate does not assert credential presence — that is the
  declared-family fail-loud preflight's job in D.
- No product code, no database migration. Ops/infra only.

### What B does not own (ownership boundary)

To keep the deferral explicit: B owns the host contract, the runner
registration, the secrets *pointer/model*, and the runbook. B does **not** own
the `live-vm` CI job (D), the guest-image warm store (C), or the S3 credential
*material* (C/D/operator). Each deferred item is a fail-loud input to D's
preflight, not a silent gap.

## Architecture

### Reuse boundary

The `deploy/ansible` tree already automates a *remote-libvirt provider* host
(TLS listener, PKI, gdbstub ACL). Two of its roles are contract-neutral and
reused as-is:

| Reused role | What it already delivers toward the contract |
| --- | --- |
| `libvirt_stack` | qemu / qemu-img / libvirt / libguestfs / virtinst install (arch-keyed qemu package map); RHEL→modular-daemon switch (`virtqemud`/`virtnetworkd`); runner user in `kvm`+`libvirt` groups; `virt-host-validate` KVM assertion. |
| `libvirt_pool_net` | `default` dir storage pool + active/autostarted `default` network (which `check-local-libvirt.sh` asserts). |

The remote-only roles (`libvirt_tls`, `gdbstub_acl`, `remote_libvirt_facts`) are
**not** used — the CI runner is a local-libvirt host with no TLS listener and no
raw-TCP gdbstub egress to firewall.

### The single service account (`github_runner_user`)

The whole contract is delivered to **one** Linux account — the account the
runner *service* runs as — and the spec names it `github_runner_user` (a
required var, default `github-runner`). This is deliberately **not**
`ansible_user_id`: operators typically run the play over SSH as `root`/`deploy`,
while the runner service runs as a dedicated non-root account. The reused
`libvirt_stack` role adds `ansible_user_id` to the `kvm`/`libvirt` groups for its
own remote-provider purpose; the CI runner needs the *service* account in those
groups, so **`live_vm_host` explicitly adds `github_runner_user` to `kvm` and
`libvirt`** rather than relying on `libvirt_stack`'s connection-user add. Every
contract step below — group membership, staging ownership, `enable-linger`,
`XDG_RUNTIME_DIR`, and the readiness gate's `become_user` — targets
`github_runner_user`. If the service account were not in `kvm`/`libvirt` (or got
the wrong `/run/user/<uid>`), every live-VM boot would fail at CI time while every
provisioning gate passed as the become user — the "green but not ready" trap one
layer down. The gate closes it by asserting `github_runner_user`'s group
membership and running as that user.

### New role `live_vm_host` — the contract delta

Everything the two reused roles do not cover, RHEL-family (both target arches are
Rocky, so SELinux applies to both), all targeting `github_runner_user`:

- **Service-account groups:** add `github_runner_user` to `kvm` and `libvirt`.
- **Debug toolchain:** install `drgn`, `crash`, `makedumpfile`, `kexec-tools`,
  `kdump-utils`, `gdb`, and — for each non-native supported arch — the foreign
  `qemu-system-<arch>` (arch-keyed map, mirroring `libvirt_stack_qemu_package_map`).
- **Project venv for the `guestfs`/`drgn` import contract, at a pinned persistent
  path.** `check-local-libvirt.sh` probes that the *worker's* interpreter can
  `import guestfs, drgn`, and that interpreter is the project `.venv`, not system
  `python3` — system-package `drgn` is not importable from the venv, and
  `libguestfs` has no PyPI wheel (the known symlink dance,
  `docs/operating/runbooks/four-method-live-run.md` §4b). The role provisions the
  venv the way the worker uses it: a **persistent repo checkout + venv at a
  stable, host-owned path** `live_vm_venv` (default `/opt/kdive`), `uv sync
  --group live` for drgn, install `python3-libguestfs`, and symlink its
  `guestfs.py` + `libguestfsmod*.so` into the venv `site-packages`. Two mechanized
  details, not left to assumption:
  - **ABI match:** `uv sync --python /usr/bin/python3` pins the venv to the
    *system* interpreter `python3-libguestfs` is built for, so the symlinked
    native module ABI-matches — uv's default managed CPython could otherwise be a
    different minor version and fail the import at runtime. The gate asserts the
    venv Python minor equals the system one.
  - **The contract D consumes:** `KDIVE_PYTHON` points at
    `<live_vm_venv>/.venv/bin/python`, a **persistent** path — the gate uses it,
    and **D's per-job workflow must reuse this venv, not build a throwaway one**
    in the ephemeral `$GITHUB_WORKSPACE` (a fresh `uv sync` there gets drgn but
    not the libguestfs symlinks, so `guestfs` would fail to import at live-test
    time). This spec states that path as a host-contract output B owns, so the
    provisioning-time green and the live-test-time import are the same venv.
  So "reaches green" is reproducible on a fresh runner and across the B/D seam,
  not only on a host whose venv is already hand-wired.
- **Staging directories.** Create the `/var/lib/kdive` tree owned by
  `github_runner_user`, with **both** staging areas the live tests need, because
  `check-local-libvirt.sh` requires its install-staging path writable and the
  provisioned-System family (kdump/install) genuinely uses it:
  - `live_vm_staging_dir` (default `/var/lib/kdive/live-vm`) — the throwaway-rootfs
    overlay area (`KDIVE_LIVE_VM_ROOTFS`'s parent);
  - the install-staging path `check-local-libvirt.sh` checks (default
    `/var/lib/kdive/install`, `KDIVE_INSTALL_STAGING`) — the provisioned-System
    kernel/initrd stage.
  Both are mode `0755` with **every parent component world-traversable** (`o+x`) —
  never under `$HOME`, whose `0700` hides it from the qemu user — and both carry a
  persistent SELinux `virt_image_t` fcontext (`sefcontext` + `restorecon`) so
  system-mode staged images are not denied by sVirt. (Session mode needs no
  relabel — qemu runs as the invoking user — but the label is correct for both
  modes.)
- **`/boot` kernel readability for the service account (RHEL-family).**
  `check-local-libvirt.sh`'s `_host_kernels_readable` is a **FAIL** (not a warn)
  when any `/boot/vmlinuz-*` / `/boot/vmlinux-*` is unreadable by the invoking
  user — libguestfs builds its supermin appliance from a host kernel (ADR-0222,
  #694/#1156). A stock Rocky 10 host ships `/boot/vmlinuz-*` mode `0600 root:root`
  (RHEL hardening), so the gate run as the non-root `github_runner_user` would
  fail. The role therefore makes the host kernels group-readable for the service
  account (`chmod 0644 /boot/vmlinu?-*`, matching both arches' names), and the
  runbook notes this **must be re-applied after a kernel upgrade** (a
  `dpkg-statoverride`-equivalent is Debian-only; on RHEL the chmod is re-run — the
  runner is Rocky, so a one-shot chmod plus the upgrade note is the mechanism).
- **Short `XDG_RUNTIME_DIR`:** `loginctl enable-linger github_runner_user` so
  `/run/user/<uid>` exists with no login session. `enable-linger` alone does
  **not** put `XDG_RUNTIME_DIR` in a system service's process environment, so the
  `github_runner` role also sets `Environment=XDG_RUNTIME_DIR=/run/user/<uid>` on
  the runner service unit (below). The two together deliver the short QMP-socket
  base (#1258) to the actual test process; `enable-linger` keeps the dir alive
  between jobs.
- **Verification (the host-contract gate).** The role fails if any part fails.
  `scripts/check-local-libvirt.sh` alone is **not** sufficient — it checks
  `/dev/kvm`, the modular daemons, the venv `guestfs`/`drgn` import, the `default`
  network, its own `KDIVE_INSTALL_STAGING` **writability**, and `/boot`
  kernel readability, but it has **no SELinux-label check, no `XDG_RUNTIME_DIR`
  check, and does not look at `live_vm_staging_dir`** (and checks the
  install-staging dir's *writability* only, not its label). So the gate is two
  parts, both run as `github_runner_user`:
  1. Run `scripts/check-local-libvirt.sh` with `become_user: github_runner_user`,
     `KDIVE_PYTHON` set to the provisioned venv, asserting exit 0 — for the KVM /
     daemon / venv-import / network / install-staging-writability / `/boot`-kernel
     contract it covers (the `/boot` FAIL is why the role makes the kernels
     readable above). Because `live_vm_host` adds `github_runner_user` to
     `kvm`/`libvirt` **in the same run** and supplementary-group membership is not
     live in an already-open session, this runs after a `meta: reset_connection`
     so the group probes read the new membership.
  2. `live_vm_host`'s **own** assertions for the delta the script does not cover:
     - **both** `live_vm_staging_dir` **and** the install-staging dir carry the
       `virt_image_t` type (assert on `ls -Z` / `matchpathcon`) — the script
       checks the install-staging dir's writability but not its label, and a
       restorecon that silently failed for either dir would otherwise sVirt-deny a
       system-mode boot at live-test time; both are asserted mode `0755` and
       writable by `github_runner_user`;
     - every parent of both staging dirs is `o+x`, so a system-mode boot is not
       blocked by a `0700` ancestor;
     - `github_runner_user` is a member of `kvm` and `libvirt`;
     - `/run/user/<github_runner_uid>` exists (post-`enable-linger`) **and** the
       installed runner service unit's `Environment=` carries
       `XDG_RUNTIME_DIR=/run/user/<uid>` (assert on the unit file /
       `systemctl show`). This checks what is verifiable at provisioning time; it
       does **not** claim to observe the service process's live environment.
  The **definitive** proof that the *runner service* process (not the Ansible
  `become` session) boots a domain with a short QMP base is a smoke run through
  the registered runner — sub-issue D's job and the runbook. The in-play gate is
  the provisioning-time sanity check, and the spec does not conflate the two
  contexts.

### New role `github_runner` — register the Actions runner

- **Arch resolution:** map `ansible_architecture` → GitHub runner asset + label:
  `x86_64` → asset `linux-x64`, label token `x64`; `ppc64le` → label token
  `ppc64le`, **no upstream asset** (see the arch seam). A `github_runner_arch_map`
  var holds this; an arch absent from the map with no override URL fails loud.
- **Download + verify:** fetch the `github_runner_version` asset over HTTPS and
  verify it against an **operator-pinned** `github_runner_sha256` var maintained
  beside `github_runner_version` (bumped together per version). The digest is
  *not* fetched from the same release page as the tarball — a same-origin
  checksum a compromised origin can serve alongside a spoofed asset is near
  vacuous; only an out-of-band pin gives real assurance. `get_url` with
  `checksum:` fails the task on mismatch, so nothing is extracted on a host that
  will execute CI.
- **Registration idempotence guard (reconciles fail-closed with `0 changed`).**
  A GitHub registration token is single-use and expires (~1h), so re-running the
  play with the same token cannot work and re-registering an already-correct
  runner would report *changed*. The role keys on the runner's `.runner` +
  `.credentials` marker files: **if the runner is already configured, skip the
  download, the token assert, and `config.sh`** (0 changed); only the
  not-yet-registered branch downloads and registers. **Divergence caveat (local
  markers can lie).** GitHub auto-removes a self-hosted runner that stays offline
  past its window (~14 days), and B installs the service *stopped* — so a runner
  registered by B and left stopped while D/trust-posture work drags on can be
  removed server-side while the local markers persist, after which a naive
  marker-only guard would skip re-registration and the operator would enable a
  runner GitHub no longer knows. So the guard is "markers present **and** the
  runner is not stale": when enabling the service (`github_runner_service_enabled`)
  the role runs a liveness check (`config.sh --check` / the service's connection
  status) and, if the local markers exist but the runner is unknown to GitHub,
  **re-registers** (needs a fresh token then) rather than starting a dead runner.
  The stale-registration failure mode and its `config.sh remove` recovery are in
  the failure table and runbook.
- **Register (first-time branch only):** configure with `--unattended --replace`,
  labels `self-hosted,kvm,<arch-token>`, a dedicated non-root
  `github_runner_user`, the repo/org URL, and a **registration token**
  (`github_runner_registration_token`) that is `no_log` and **fails closed if
  empty** (mirrors the `gdb_addr` fail-closed precedent). The token assert lives
  inside this branch, so an idempotent re-run of an already-registered host needs
  no token.
- **Service, installed stopped (closes the B-before-D RCE window).** Install the
  runner's `svc.sh` systemd service as `github_runner_user`, with `Environment=`
  carrying `XDG_RUNTIME_DIR=/run/user/<uid>` (the short QMP-socket base, paired
  with `live_vm_host`'s `enable-linger`) and `KDIVE_SECRETS_ROOT` (the pointer to
  the file-based S3 credentials — B sets the pointer; C/D or the operator populate
  the material). The service is installed **not started and not enabled** by
  default: a bare `runner.yml` run registers the runner but leaves **no listener**
  picking up jobs. Only when `github_runner_service_enabled: true` is set — after
  the operator has applied the trusted-events posture — does the role start/enable
  it. This is because B is sequenced before D: until D's `if:` guard and the repo
  approval setting exist, a listening self-hosted runner on
  `[self-hosted, kvm, <arch>]` is an arbitrary-code-execution target for a fork PR.
  Installing stopped makes the automation itself fail-safe, not just the runbook.
- **Trusted-events posture (ordered, not merely documented):** the runner
  *binary* cannot enforce which events dispatch to it — that is the workflow `if:`
  guard (sub-issue D) plus the repository setting "Require approval for all
  outside collaborators' workflows." The runbook **orders** that repo setting (and
  D's guard) **before** enabling the service, and the ADR records why (fork-PR +
  self-hosted = RCE). The `KDIVE_S3_*` secrets ride repo/org secrets, readable by
  `schedule` / `workflow_dispatch` but never by fork PRs.

### The arch seam (the one non-additive step, made explicit)

Every host-build step resolves by arch: native qemu (`libvirt_stack`), foreign
emulator, label token, rootfs, tools. The **single** step that is not a free
drop-in for ppc64le is the GitHub runner *binary*: the `actions/runner` project
ships release assets for `linux-x64` / `linux-arm64` / `linux-arm` only — there
is no `ppc64le` asset. The design turns this from a hidden blocker into a
parameterized seam:

- `github_runner_tarball_url` overrides the derived asset URL, so an operator can
  point at a self-built ppc64le runner.
- With no upstream asset and no override, the role **fails loud** with a message
  naming the gap, rather than downloading a wrong-arch binary or silently
  skipping registration.

So adding the POWER runner is: build/point-at a ppc64le `actions/runner` and set
the override URL — one documented step, and the rest of the host build is
unchanged. This satisfies the epic's "nothing in the design blocks ppc64le."

### Playbook + inventory

- `playbooks/runner.yml`: applies `libvirt_stack` → `libvirt_pool_net` →
  `live_vm_host` → `github_runner` to a new `live_vm_runners` inventory group.
- Inventory: a `live_vm_runners` group in `hosts.yml` and a host_vars file for
  the x86_64 runner (the existing `rock10-big` is a remote-libvirt host; the
  runner is a distinct role for a host, so it gets its own group membership).

## Failure modes and how they are handled

| Failure | Handling |
| --- | --- |
| Registration token absent (first-time branch) | Fail closed in the register branch (`assert`, `no_log`), like `gdb_addr`. On an already-registered host the branch is skipped, so no token is needed. |
| Host arch has no runner asset + no override URL | Fail loud naming the ppc64le gap and the `github_runner_tarball_url` knob. |
| Downloaded runner tarball checksum mismatch | `get_url` with `checksum:` (an operator-pinned `github_runner_sha256`, not a same-origin fetch) fails the task; nothing is extracted. |
| `/dev/kvm` / daemons / venv-import not reachable | `check-local-libvirt.sh` gate (run as `github_runner_user`, `KDIVE_PYTHON`=venv, after `reset_connection` so group membership is live) exits non-zero → play fails with the script's actionable FAIL lines. |
| Service account not in `kvm`/`libvirt` (runs as a different user than the play connects as) | `live_vm_host` adds `github_runner_user` (not `ansible_user_id`) to `kvm`/`libvirt`; the gate asserts that membership, so a mismatched account fails at provisioning instead of at CI-boot time. |
| Venv cannot `import guestfs, drgn` on a fresh host | `live_vm_host` provisions the venv (`uv sync --group live` + the `libguestfs` symlink) and the gate runs against it via `KDIVE_PYTHON` — reproducible, not dev-host-only. |
| Staging dir mis-pointed into `$HOME` / a `0700` ancestor | `live_vm_host`'s own gate asserts every parent of `live_vm_staging_dir` is `o+x`; a non-traversable ancestor fails the play (`check-local-libvirt.sh` cannot see the traversal problem). |
| SELinux `virt_image_t` not applied (either staging dir) | `restorecon` after `sefcontext`, then `live_vm_host`'s own gate asserts the label on **both** the live-VM and install-staging dirs via `ls -Z`/`matchpathcon` — a missing label on either fails the play at provisioning, not silently at live-test time. |
| Stock Rocky 10 ships `/boot/vmlinuz-*` `0600 root:root` → gate fails as non-root | `live_vm_host` makes the host kernels group-readable (`chmod 0644 /boot/vmlinu?-*`) so `check-local-libvirt.sh`'s `_host_kernels_readable` passes for the service account; runbook flags re-applying after a kernel upgrade. |
| Runner service missing a short `XDG_RUNTIME_DIR` | `live_vm_host`'s gate asserts `/run/user/<uid>` exists **and** the service unit's `Environment=` carries `XDG_RUNTIME_DIR` — the process-env proof is deferred to D's smoke run. |
| Runner registered but starts listening before the trust posture is set | The service installs **stopped/disabled**; the role starts it only on explicit `github_runner_service_enabled: true`, so a bare `runner.yml` leaves no fork-PR-exploitable listener. |
| Locally registered but removed server-side (offline past GitHub's ~14-day window, or admin-removed) | Enabling the service runs a liveness check; if the local `.runner` markers exist but GitHub no longer knows the runner, the role re-registers (fresh token) instead of starting a dead runner. Recovery (`config.sh remove` + re-run) and the offline-window warning are in the runbook. |

## Testing

- **`ansible-lint` + `yamllint`** (`just lint-ansible`) gate every new role/play.
- **`just test-ansible`** gains a `github_runner` regression case, mirroring the
  `gdbstub_acl` harness (drive the **real** task in isolation via `--tags` + a
  fake `PATH`), asserting the two security-sensitive, pure-logic behaviors:
  1. **Token fail-closed:** in the not-yet-registered branch with
     `github_runner_registration_token` empty, the play fails at the assert and no
     download/`config.sh` runs.
  2. **Arch fail-loud:** an `ansible_architecture` absent from the asset map with
     no `github_runner_tarball_url` fails with the ppc64le-gap message, rather
     than downloading or skipping.
  3. **Already-registered skip:** with the `.runner`/`.credentials` markers
     present, the play skips the download, the token assert, and `config.sh` (0
     changed) — proving the idempotence guard reconciles fail-closed with a
     no-token re-run.
  The fake stands in for the network fetch + `config.sh`, so the test needs no
  GitHub token and no live runner.
- **`shellcheck`** (`just lint-shell`) on any shell the runbook or role ships.
- **Idempotence:** measured on an **already-registered** host. The host-setup
  roles (`libvirt_stack`, `libvirt_pool_net`, `live_vm_host`) are naturally
  idempotent and report `0 changed`; `github_runner` reports `0 changed` because
  the `.runner`/`.credentials` marker guard skips re-registration. The first-time
  registration is inherently a change (it configures a new runner) and is not
  part of the `0 changed` bar — the bar asserts a second run of a converged host
  is a no-op. Noted in the runbook.
- **Local host-contract validation:** on this dev KVM host, `live_vm_host`'s
  two-part gate reaches green — `check-local-libvirt.sh` (run as
  `github_runner_user` against the provisioned venv) for the KVM / daemon /
  venv-import / network contract it covers, plus the role's own assertions for the
  service-account group membership, `virt_image_t` label, parent traversability,
  and the `/run/user/<uid>` + service-unit `XDG_RUNTIME_DIR` that the script does
  not cover. The runner-service *process*-context proof (a job actually booting a
  domain) is deferred to D's smoke run.

## Runbook

A `self-hosted-kvm-runner.md` runbook under `docs/operating/runbooks/`:
prerequisites; the playbook commands (`libvirt_stack` → `libvirt_pool_net` →
`live_vm_host` → `github_runner` via `runner.yml`); provisioning the persistent
venv at `live_vm_venv` and the `KDIVE_PYTHON` contract D consumes (reuse this
venv, do not build a per-job one); obtaining a registration token; **the ordered
security steps — apply the repo "require approval for all outside collaborators"
setting (and D's `if:` guard) BEFORE setting `github_runner_service_enabled:
true`**, so the runner never listens without the trust posture; **the offline-
removal warning — leaving the service stopped past GitHub's ~14-day window
invalidates the registration; recovery is `config.sh remove` then re-run with a
fresh token**; wiring `KDIVE_S3_*` secrets and where the credential material lands
(C/D/operator, since A's resolver does not check it); the ppc64le
`github_runner_tarball_url` override; deregistration (`config.sh remove` /
`svc.sh uninstall`); and the verification steps (idempotence `0 changed` on an
already-registered host + off-host `check-local-libvirt`). An `AGENTS.md` /
`deploy/ansible/README.md` pointer so the runner build is discoverable.

## Rollout / rollback

- Rollout is additive: new roles, new playbook, new inventory group, new runbook,
  one ADR + README index row. No existing role or playbook changes behavior.
- Rollback: the `github_runner` role documents `config.sh remove` (deregister)
  and `svc.sh uninstall`; removing the host from the `live_vm_runners` group and
  not running `runner.yml` leaves the remote-libvirt automation untouched.
