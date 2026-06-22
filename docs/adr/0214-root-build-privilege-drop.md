# ADR 0214 — Drop privileges for the local build subprocess when the worker runs as root

- **Status:** Accepted
- **Date:** 2026-06-22
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0162](0162-local-git-build-lane.md) (the
  worker-local git-clone lane this fences), [ADR-0101](0101-local-libvirt-remote-build-host.md)
  (the worker-local builder/checkout seam), [ADR-0029](0029-build-plane-local-make.md) (the
  `make` build), [ADR-0204](0204-install-staging-unwritable-config-error.md) (the operator-prereq /
  `CONFIGURATION_ERROR`-at-build-time pattern for a root-owned staging parent),
  [ADR-0019](0019-tool-response-envelope.md) (error taxonomy).
- **Spec:** [`../design/root-build-privilege-drop.md`](../design/root-build-privilege-drop.md)

## Context

Local-libvirt kdump capture requires the **worker to run as root** on the KVM host:
`virtlogd` writes the guest console log `root:0600` (the boot handler must read it), and the
host-side vmcore harvest uses libguestfs + domain force-off + `kexec`. The four-method runbook
§4b already names "run the worker as root" as the simplest arrangement for `qemu:///system`.

A build worker compiles a **from-source kernel**: the worker-local build lane (ADR-0162) clones
an agent-supplied git ref and runs `make` over operator/agent-supplied source. A kernel build is
arbitrary code execution — Kbuild/Kconfig invoke `merge_config.sh`, shell `scripts/`, and host
tools described by the source tree. When the worker runs as root, that build executes **as root**.
The only existing fence is `KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST` (which bounds *where* a git clone
may fetch, not *what privilege* the resulting `make` runs at), and warm-tree builds have no fence
at all. Spawning a root-context build of untrusted source should not be implicit.

## Decision

**Run every local build subprocess as an unprivileged build user; never `make`/clone untrusted
source as root.** The worker keeps root only for the libvirt/libguestfs/`kexec`/console
operations that require it.

1. **New worker setting `KDIVE_BUILD_USER`** (group `build`, processes `{worker}`): the name of an
   unprivileged **passwd account** the local build lane drops to. It must be a real account (a bare
   numeric uid is not accepted — the account is the one source of uid, primary gid, supplementary
   groups via `os.getgrouplist`, and the home directory the demoted child needs). Empty/unset is the
   deny-by-default posture.

2. **Resolve a build sandbox once per build, lazily, fail-closed.** At build time the worker
   reads its effective uid:
   - **euid ≠ 0** (worker already unprivileged): no demotion. The build runs as the current user
     exactly as today; `KDIVE_BUILD_USER` is ignored. This keeps the common developer/CI path
     unchanged.
   - **euid == 0 and `KDIVE_BUILD_USER` set** to a resolvable non-root account: build a
     `BuildSandbox(uid, gid, extra_groups, umask=0o077)` from `pwd`/`os.getgrouplist` and demote
     every build subprocess to it.
   - **euid == 0 and `KDIVE_BUILD_USER` unset, unknown, or resolving to uid 0**: **fail closed**.
     The local build lane refuses to run with a `CONFIGURATION_ERROR` that names the setting; the
     `BUILD` job fails (admitted-then-failed timing, matching ADR-0162/ADR-0204). The worker never
     silently compiles as root.

   Resolution is lazy (at the first build step, inside `build()`), not at `from_env`/composition,
   so a root worker without `KDIVE_BUILD_USER` still starts and registers its handler — only a
   build *attempt* fails, as a categorized job failure rather than a worker-startup crash.

3. **Demote the subprocesses that execute untrusted code, by spawning them with
   `user=/group=/extra_groups=/umask=`** (Python's child-side setuid/setgid; passed only when a
   sandbox is active, i.e. only when root): the git clone (init/fetch/checkout), `make defconfig`,
   `merge_config.sh`, `make olddefconfig`, `make`, `make modules_install`, and `git apply`. Because
   `user=/group=` change the uid/gid but **not** the environment, the demoted child's `env` is
   rebased onto the build user (`HOME`, `USER`, `LOGNAME`, dropped `XDG_*`) so no demoted tool
   resolves `$HOME` to `/root`; a call site's own hardened `env` (the git invocations) is layered on
   top, not discarded.

4. **Hand the workspace to the build user before demoted writes**, per the two source-trust tiers:
   - **git lane (untrusted remote):** the empty per-run workspace dir is created by the worker
     (root) and `chown`ed to the build user *before* the demoted `git init`, so the clone and all
     fetched content land owned by the build user.
   - **warm-tree lane (operator-staged, trusted):** the `rsync -a --delete` runs as root (it must
     read an operator tree whose permissions kdive does not control) but carries `--chown=uid:gid`
     and the dest dir is `chown`ed, so the materialized tree is build-user-owned for the demoted
     `make` that follows.
   - **modules staging:** the `mkdtemp` `INSTALL_MOD_PATH` root is `chown`ed to the build user
     before the demoted `make modules_install`.
   - **the kdump fragment file:** `merge_config` writes it as root (its mode follows the worker
     umask, so a hardened `0o077` worker would leave it `0600 root:root`, unreadable by the demoted
     `merge_config.sh`), then `chown`s it to the build user. The general rule is that any file the
     root worker writes into the build-user-owned workspace that a demoted step must read is
     `chown`ed.

5. **`objcopy` (build-id extraction) stays root.** It is a trusted binutils invocation doing a
   bounded read of the build-user-owned `vmlinux` into a worker-private temp file — not an
   execution of untrusted source. Demoting it would force the temp note file into a build-user
   path for no security gain. Recorded as a deliberate residual, not an oversight.

6. **Remote/SSH build hosts are unaffected.** They already run the build on an isolated host over
   a transport (ADR-0101); the sandbox is wired only into the worker-local `from_env` seams.

## Consequences

- A root worker can capture kdump (its reason for being root) **and** build from source without
  the build ever running as root. Operators opt in by creating an unprivileged account and setting
  `KDIVE_BUILD_USER`; the in-tree default (`""`) fails closed.
- Two new operator prerequisites, documented beside the existing `KDIVE_INSTALL_STAGING` /
  `KDIVE_KERNEL_SRC` ones: the build-workspace parent (`KDIVE_BUILD_WORKSPACE`) must be traversable
  (`o+x`) by the build user, and the warm tree / patch refs must be readable by it. A missing
  prerequisite surfaces as a categorized build failure, not a silent root build.
- A non-root worker (developer laptop, CI) sees no behavior change and needs no new configuration.
- The control is observable: the build logs the demotion target (`user_name`/uid/gid) when it
  demotes and logs the skip when euid ≠ 0, so "ran unprivileged" and "silently bypassed" are
  distinguishable from the worker log; the fail-closed path is already a visible failed `BUILD` job.
- The sandbox is a single value object threaded through the worker-local checkout + run-step
  seams; the demotion kwargs are passed only when root, so a non-root unit run never hits the
  setuid path (the real demotion is exercised under the `live_vm` gate on the KVM host).

## Considered & rejected

- **Authorization gate only (a `KDIVE_ALLOW_ROOT_BUILD` opt-in that still runs `make` as root).**
  Smaller, but it only *records consent* to a root build — the untrusted compile still executes
  with full privilege. The issue asks that untrusted build execution "never silently run as root";
  consent does not satisfy that. Rejected in favor of an actual privilege boundary (this was the
  operator's chosen direction over the gate).
- **Refuse the local build lane entirely when euid == 0** (no build user, no demotion). Simplest
  and fail-closed, but it makes "root worker for kdump" and "from-source build" mutually exclusive
  on the same worker — exactly the #679 arrangement we need to support. Rejected.
- **Demote the whole worker process / re-exec the build under the build user.** The worker must
  retain root for libvirt/console/`kexec`; a process-wide drop or a re-exec'd build child that
  re-acquires a DB pool and object-store client is far more machinery than spawning the existing
  subprocesses demoted. Rejected.
- **`preexec_fn` to call `setuid` in the child.** `subprocess`'s native `user=/group=` does the
  same setuid/setgid but is async-signal-safe and not subject to the `preexec_fn` fork-safety
  caveats. Rejected.
- **Run the warm-tree `rsync` demoted too (uniform demotion of every step).** A demoted `rsync`
  cannot read an operator warm tree that is root-only-readable, coupling correctness to the
  operator's source-tree permissions. Populating as root with `--chown` is permission-agnostic and
  still yields a build-user-owned tree for the compile. Rejected.
- **Resolve the sandbox at `from_env`/composition (eager).** A root worker without
  `KDIVE_BUILD_USER` would then crash at worker startup rather than failing a single build job —
  worse operability and inconsistent with ADR-0162/0204's admitted-then-failed timing. Rejected.
- **A separate build group setting (`KDIVE_BUILD_GROUP`).** YAGNI: the account's primary group +
  supplementary groups from `os.getgrouplist` are sufficient; a group override can be added later
  if a real need appears. Rejected.
