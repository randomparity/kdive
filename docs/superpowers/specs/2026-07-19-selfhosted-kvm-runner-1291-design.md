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
  is owned by sub-issue C/D or the operator (see "B does not own" below). The
  resolver fails loud on missing credentials, so a runner that lacks them fails a
  declared provisioned-System run rather than skipping to green.

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

### New role `live_vm_host` — the contract delta

Everything the two reused roles do not cover, RHEL-family (both target arches are
Rocky, so SELinux applies to both):

- **Debug toolchain:** install `drgn`, `crash`, `makedumpfile`, `kexec-tools`,
  `kdump-utils`, `gdb`, and — for each non-native supported arch — the foreign
  `qemu-system-<arch>` (arch-keyed map, mirroring `libvirt_stack_qemu_package_map`).
- **Staged-rootfs directory:** create `live_vm_staging_dir` (default
  `/var/lib/kdive/live-vm`) owned by the runner user, mode `0755`, with **every
  parent component world-traversable** (`o+x` up the tree) — never under `$HOME`,
  whose `0700` mode hides it from the qemu user. Apply a persistent SELinux
  `virt_image_t` fcontext (`sefcontext` + `restorecon`) so system-mode staged
  images are not denied by sVirt. Session-mode tests need no relabel (qemu runs
  as the invoking user), but the label is correct for both modes and harmless in
  session mode.
- **Short `XDG_RUNTIME_DIR`:** `loginctl enable-linger` for the runner user so
  `/run/user/<uid>` exists even with no login session. `enable-linger` alone does
  **not** put `XDG_RUNTIME_DIR` in a system service's process environment, so the
  `github_runner` role also sets `Environment=XDG_RUNTIME_DIR=/run/user/<uid>` on
  the runner service (below); the two together are what deliver the short QMP
  socket base (#1258) to the actual test process. `enable-linger` keeps
  `/run/user/<uid>` alive between jobs.
- **Verification (the host-contract gate).** The role fails if any part fails.
  `scripts/check-local-libvirt.sh` alone is **not** sufficient — it checks
  `/dev/kvm`, the modular daemons, the toolchain imports, the `default` network,
  and its own `KDIVE_INSTALL_STAGING`, but it has **no SELinux-label check, no
  `XDG_RUNTIME_DIR` check, and does not look at `live_vm_staging_dir`**. So the
  gate is two parts:
  1. Run `scripts/check-local-libvirt.sh` as the runner user, asserting exit 0,
     for the KVM / daemon / toolchain / network / install-staging contract it
     does cover. Because `libvirt_stack` adds the runner user to the `kvm` and
     `libvirt` groups **in the same run** and supplementary-group membership is
     not live in an already-open session, this task runs after a
     `meta: reset_connection` (fresh `become` session) so the group probes read
     the new membership rather than false-failing or passing in a stale context.
  2. `live_vm_host`'s **own** assertions for the delta this role uniquely owns,
     which `check-local-libvirt.sh` does not cover:
     - `live_vm_staging_dir` carries the `virt_image_t` type (assert on `ls -Z` /
       `matchpathcon`), is mode `0755`, and is writable by the runner user;
     - every parent of `live_vm_staging_dir` is `o+x` (traversable by the qemu
       user), so a system-mode boot is not blocked by a `0700` ancestor;
     - `XDG_RUNTIME_DIR` resolves to a short (`/run/user/<uid>`-length) path.
  The **definitive** proof that the *runner service* process (not the Ansible
  `become` session) can boot a domain is a smoke run through the registered
  runner — that lives in sub-issue D's job and the runbook; the in-play gate is
  the provisioning-time sanity check, and the spec does not claim the two are the
  same context.

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
  runner would report *changed*. The role therefore keys on the runner's
  `.runner` + `.credentials` marker files: **if the runner is already configured,
  skip the download, the token assert, and `config.sh` entirely** (0 changed);
  only the not-yet-registered branch downloads and registers.
- **Register (first-time branch only):** configure with `--unattended --replace`,
  labels `self-hosted,kvm,<arch-token>`, a dedicated non-root
  `github_runner_user`, the repo/org URL, and a **registration token**
  (`github_runner_registration_token`) that is `no_log` and **fails closed if
  empty** (mirrors the `gdb_addr` fail-closed precedent). The token assert lives
  inside this branch, so an idempotent re-run of an already-registered host needs
  no token.
- **Service:** install the runner's `svc.sh` systemd service running as the
  non-root user, with `Environment=` carrying `XDG_RUNTIME_DIR=/run/user/<uid>`
  (the short QMP-socket base, paired with `live_vm_host`'s `enable-linger`) and
  `KDIVE_SECRETS_ROOT` (the pointer to the file-based S3 credentials — B sets the
  pointer; the credential material is populated by C/D or the operator).
- **Trusted-events posture (documented + asserted where it can be):** the
  runner *binary* cannot enforce which events dispatch to it — that is the
  workflow `if:` guard (sub-issue D) plus the repository setting "Require
  approval for all outside collaborators' workflows." The role/runbook states
  this as a hard requirement and the ADR records why (fork-PR + self-hosted =
  RCE). The `KDIVE_S3_*` secrets ride repo/org secrets, readable by `schedule` /
  `workflow_dispatch` but never by fork PRs.

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
| `/dev/kvm` / daemons / tools not reachable | `check-local-libvirt.sh` gate task (run after a `reset_connection` so group membership is live) exits non-zero → play fails with the script's actionable FAIL lines. |
| Staging dir mis-pointed into `$HOME` / a `0700` ancestor | `live_vm_host`'s own gate asserts every parent of `live_vm_staging_dir` is `o+x`; a non-traversable ancestor fails the play (`check-local-libvirt.sh` does **not** cover this — it checks a different dir and cannot see the traversal problem). |
| SELinux `virt_image_t` not applied | `restorecon` after `sefcontext`, then `live_vm_host`'s own gate asserts the label via `ls -Z`/`matchpathcon` — a missing label fails the play at provisioning, not silently at live-test time. |
| Runner service missing a short `XDG_RUNTIME_DIR` | `live_vm_host`'s gate asserts `XDG_RUNTIME_DIR` resolves to a `/run/user/<uid>`-length path; the service `Environment=` sets it, `enable-linger` keeps the dir alive. |

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
  two-part gate reaches green — `check-local-libvirt.sh` for the KVM / daemon /
  toolchain / network contract it covers, plus the role's own assertions for the
  `virt_image_t` label, parent traversability, and short `XDG_RUNTIME_DIR` that
  the script does not cover.

## Runbook

A `self-hosted-kvm-runner.md` runbook under `docs/operating/runbooks/`:
prerequisites, the three
playbook commands, obtaining a registration token, the trusted-events repo
settings, wiring `KDIVE_S3_*` secrets, the ppc64le override, and the verification
steps (idempotence + off-host `check-local-libvirt`). An `AGENTS.md` /
`deploy/ansible/README.md` pointer so the runner build is discoverable.

## Rollout / rollback

- Rollout is additive: new roles, new playbook, new inventory group, new runbook,
  one ADR + README index row. No existing role or playbook changes behavior.
- Rollback: the `github_runner` role documents `config.sh remove` (deregister)
  and `svc.sh uninstall`; removing the host from the `live_vm_runners` group and
  not running `runner.yml` leaves the remote-libvirt automation untouched.
