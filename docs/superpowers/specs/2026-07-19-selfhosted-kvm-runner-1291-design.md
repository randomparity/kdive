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
- **Object-store env for the provisioned-System family:** `KDIVE_S3_*` reachable
  and file-based S3 credentials under `KDIVE_SECRETS_ROOT`.

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
- No product code, no database migration. Ops/infra only.

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
  `/var/lib/kdive/live-vm`) owned by the runner user, mode `0755` under a
  world-traversable path — never `$HOME`. Apply a persistent SELinux
  `virt_image_t` fcontext (`sefcontext` + `restorecon`) so system-mode staged
  images are not denied by sVirt. Session-mode tests need no relabel (qemu runs
  as the invoking user), but the label is correct for both modes and harmless in
  session mode.
- **Short `XDG_RUNTIME_DIR`:** `loginctl enable-linger` for the runner user so
  `/run/user/<uid>` exists even when the runner runs as a systemd service with no
  login session — that path is the short base session-mode libvirt uses for the
  QMP socket.
- **Verification:** run `scripts/check-local-libvirt.sh` as the runner user and
  assert exit 0 (the acceptance's "`check-local-libvirt` … passes"). This is the
  single host-contract gate; the role fails if it fails.

### New role `github_runner` — register the Actions runner

- **Arch resolution:** map `ansible_architecture` → GitHub runner asset + label:
  `x86_64` → asset `linux-x64`, label token `x64`; `ppc64le` → label token
  `ppc64le`, **no upstream asset** (see the arch seam). A `github_runner_arch_map`
  var holds this; an arch absent from the map with no override URL fails loud.
- **Download + verify:** fetch the pinned `github_runner_version` asset over
  HTTPS and verify its published SHA-256 before extracting (never extract an
  unverified tarball on a host that will execute CI).
- **Register:** configure with `--unattended --replace`, labels
  `self-hosted,kvm,<arch-token>`, a dedicated non-root `github_runner_user`, the
  repo/org URL, and a **registration token** (`github_runner_registration_token`)
  that is `no_log` and **fails closed if empty** (mirrors the `gdb_addr`
  fail-closed precedent).
- **Service:** install the runner's `svc.sh` systemd service running as the
  non-root user, with `KDIVE_SECRETS_ROOT` set so the provisioned-System family's
  file-based S3 credentials resolve.
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
| Registration token absent | Fail closed at play start (`assert`, `no_log`), like `gdb_addr`. |
| Host arch has no runner asset + no override URL | Fail loud naming the ppc64le gap and the `github_runner_tarball_url` knob. |
| Downloaded runner tarball checksum mismatch | `get_url` with `checksum:` fails the task; nothing is extracted. |
| `/dev/kvm` / daemons / tools not reachable | `check-local-libvirt.sh` verification task exits non-zero → play fails with the script's actionable FAIL lines. |
| Staging dir under `$HOME` or unlabeled | Role creates it at a world-traversable default and relabels; a misconfigured override that points into `$HOME` is caught by the `check-local-libvirt.sh` install-staging writability check. |
| SELinux `virt_image_t` not applied | `restorecon` after `sefcontext`; system-mode boot denial otherwise surfaces in the live tests, not silently. |

## Testing

- **`ansible-lint` + `yamllint`** (`just lint-ansible`) gate every new role/play.
- **`just test-ansible`** gains a `github_runner` regression case, mirroring the
  `gdbstub_acl` harness (drive the **real** task in isolation via `--tags` + a
  fake `PATH`), asserting the two security-sensitive, pure-logic behaviors:
  1. **Token fail-closed:** with `github_runner_registration_token` empty, the
     play fails at the assert and no download/`config.sh` runs.
  2. **Arch fail-loud:** an `ansible_architecture` absent from the asset map with
     no `github_runner_tarball_url` fails with the ppc64le-gap message, rather
     than downloading or skipping.
  The fake stands in for the network fetch + `config.sh`, so the test needs no
  GitHub token and no live runner.
- **`shellcheck`** (`just lint-shell`) on any shell the runbook or role ships.
- **Idempotence:** `runner.yml` documented to report `0 changed` on a second run
  (the repo's existing Ansible verification bar), noted in the runbook.
- **Local host-contract validation:** on this dev KVM host, `check-local-libvirt`
  reaches green with the daemons, `virt_image_t` label, and toolchain in place —
  the codified verification the role runs.

## Runbook

`docs/operating/runbooks/self-hosted-kvm-runner.md`: prerequisites, the three
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
