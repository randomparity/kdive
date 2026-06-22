# Drop privileges for the local build subprocess when the worker runs as root (#689)

- **Status:** Accepted
- **Date:** 2026-06-22
- **ADR:** [ADR-0214](../adr/0214-root-build-privilege-drop.md)
- **Issue:** [#689](https://github.com/randomparity/kdive/issues/689) — "Require additional
  authorization to spawn a build worker when kdive servers run as root"

## Problem

Local-libvirt kdump capture forces the worker to run as **root** on the KVM host (`virtlogd`
writes the console log `root:0600`; vmcore harvest needs libguestfs + force-off + `kexec`). On
that same root worker, the worker-local build lane (ADR-0162) clones an agent-supplied git ref
and runs `make` over operator/agent-supplied kernel source. A kernel build is arbitrary code
execution (Kbuild/Kconfig run `merge_config.sh`, shell `scripts/`, and source-described host
tools). Today that build executes **as root** with no fence beyond the egress allowlist (which
bounds where a clone fetches, not the privilege the compile runs at). Warm-tree builds have no
fence at all.

## Goal

When the worker runs as root, run every local build subprocess (clone, `make`, config merge,
patch) as an **unprivileged build user**, never as root. The worker keeps root only for the
libvirt/libguestfs/`kexec`/console operations. Deny by default: a root worker with no build user
configured refuses the local build lane (fail-closed `CONFIGURATION_ERROR`) rather than building
as root.

Non-goals: dropping privileges of the worker process itself; sandboxing remote/SSH build hosts
(already isolated, ADR-0101); changing the egress allowlist; a build *group* override setting.

## Design

### Effective-uid is the trigger; `KDIVE_BUILD_USER` is the opt-in

A new worker setting:

```
KDIVE_BUILD_USER     # string: a passwd account NAME; group "build"; processes={worker}
```

`KDIVE_BUILD_USER` must name a **real passwd account** (resolved via `pwd.getpwnam`), not a bare
numeric uid. The account is the single source of the uid, the primary gid, the supplementary
groups (`os.getgrouplist`), and the home directory (for the demoted child's `HOME`); a bare uid
with no passwd entry has none of those, and admitting it would both contradict the
"unknown account → fail closed" rule below and leave `getgrouplist`/`HOME` undefined. Requiring an
account is the simplest unambiguous contract (an operator running builds as an "unnamed uid" is
not a case we support; create an account).

The local build resolves a **build sandbox** once per build, lazily (at the first build step,
inside `LocalLibvirtBuild.build()` — not at `from_env`, so a root worker without the setting still
starts and only a build *attempt* fails):

| `os.geteuid()` | `KDIVE_BUILD_USER` | Result |
|---|---|---|
| ≠ 0 | (ignored) | **No demotion.** Build runs as the current user, unchanged from today. A `debug` log records that demotion is skipped because the worker is not root. |
| 0 | unset / empty | **Fail closed.** `CONFIGURATION_ERROR` naming the setting; the `BUILD` job fails. |
| 0 | unknown account (no `pwd.getpwnam`) / resolves to uid 0 | **Fail closed.** `CONFIGURATION_ERROR`. |
| 0 | resolvable non-root account | **Demote.** `BuildSandbox(uid, gid, extra_groups, home, umask=0o077)`. An `info` log records the demotion target (`user_name`, uid, gid) at build start. |

`uid`, `gid` (primary group), and `home` come from the passwd entry; `extra_groups` from
`os.getgrouplist(name, gid)`.

**Observability.** A privilege-drop control is invisible from the outside unless it is logged:
"demotion applied to uid N" and "demotion skipped (worker not root)" are otherwise
indistinguishable from a silent bypass. The build therefore logs the demotion target when it
demotes and logs the skip when euid ≠ 0, so an operator can confirm from the worker log that
builds run unprivileged (the fail-closed path is already observable as a failed `BUILD` job).

### `BuildSandbox` — a single value object, demotion only when root

```python
@dataclass(frozen=True, slots=True)
class BuildSandbox:
    uid: int
    gid: int
    extra_groups: tuple[int, ...]
    user_name: str
    home: str
    umask: int = 0o077

    def run(self, argv, *, env=None, **kwargs) -> CompletedProcess:
        return subprocess.run(
            argv, user=self.uid, group=self.gid,
            extra_groups=list(self.extra_groups), umask=self.umask,
            env=self._child_env(env), **kwargs,
        )

    def _child_env(self, env):
        # subprocess user=/group= change the uid/gid but NOT the environment. Without this the
        # demoted child inherits the root worker's HOME=/root, USER=root, etc., which breaks any
        # build tool that writes under $HOME and is an incomplete sandbox. Rebase the identity
        # env onto the build user. Callers that already pass a custom env (the hardened git env)
        # are layered on top of this base, not discarded.
        base = dict(env if env is not None else os.environ)
        base.update(HOME=self.home, USER=self.user_name, LOGNAME=self.user_name)
        base.pop("XDG_RUNTIME_DIR", None)
        base.pop("XDG_CACHE_HOME", None)
        return base
```

A module-level `sandbox_run(sandbox: BuildSandbox | None, argv, **kwargs)` is the single chokepoint
every demotable call site uses: `subprocess.run(argv, **kwargs)` when `sandbox is None`, else
`sandbox.run(...)`. The `user=/group=` kwargs are passed **only** when a sandbox exists — and a
sandbox only exists when euid == 0 — so a non-root process never asks the kernel to setuid (which
would raise). Call sites that build their own `env` (the hardened git invocations in `_run_git` /
`apply_patch`) pass it through `sandbox_run`, which layers the build-user identity onto it. The
real demotion is therefore exercised only on the root KVM host (`live_vm`); unit tests assert the
resolution table, that the kwargs are assembled, and that the child `env` carries the build user's
`HOME`/`USER` (not `/root`/`root`), via a fake runner.

A `SandboxProvider` memoizes the resolution (and re-raises the cached fail-closed error) so the
several seams that call `.get()` during one build resolve identically and fail-closed exactly once.

### Which subprocesses demote, and how the workspace changes hands

The local build spawns these subprocesses (today all as the worker's euid):

| Step (file) | Demote? | Workspace handoff |
|---|---|---|
| `git init/fetch/checkout` — git lane (`workspace.py:clone_tree`) | **yes** | empty per-run dir `chown`ed to build user *before* `git init` |
| `rsync -a --delete` — warm-tree lane (`workspace.py:sync_tree`) | no (root) | `--chown=uid:gid` + dest dir `chown` → tree owned by build user |
| `make defconfig` + `merge_config.sh` (`workspace.py:merge_config`) | **yes** | runs in the build-user-owned workspace |
| `git apply` (`workspace.py:apply_patch`) | **yes** | patch ref must be build-user-readable (documented prereq) |
| `make olddefconfig` (`execution.py:real_run_olddefconfig`) | **yes** | — |
| `make` (`execution.py:real_run_make`) | **yes** | — |
| `make modules_install` (`execution.py:real_run_modules_install`) | **yes** | `mkdtemp` mod root `chown`ed to build user first |
| `objcopy` build-id (`execution.py:real_read_build_id`) | **no (root)** | trusted bounded ELF-note read of a build-user file; residual |

The two source-trust tiers (ADR-0214 §4) drive the handoff:
- **git remote = untrusted** → demote the clone itself; pre-`chown` the empty dir so fetched
  content is build-user-owned.
- **warm tree = operator-staged/trusted** → populate as root (kdive does not control the operator
  tree's read permissions) with `rsync --chown`, handing the materialized tree to the build user
  for the demoted `make`.

`merge_config` writes the kdump fragment file as root into the build-user-owned workspace, then the
demoted `merge_config.sh` reads it. **Ownership is not irrelevant here:** `Path.write_bytes` honors
the worker's umask, so on a hardened root worker (umask `0o077`) the fragment lands `0600
root:root` and the demoted `merge_config.sh` cannot read it — the build would break on exactly the
deployment this feature targets. So after writing the fragment, `merge_config` **`chown`s it to the
build user** (when a sandbox is active), the same handoff the workspace tree gets. The general rule:
any file the root worker writes into the build-user-owned workspace that a demoted step must read is
`chown`ed (or written demoted); the fragment is the one such file today, and a test asserts it is
build-user-readable.

### Code organization

- New `providers/shared/build_host/sandbox.py`: `BuildSandbox`, `sandbox_run`, `SandboxProvider`,
  and `resolve_build_sandbox_provider()` (reads euid + `KDIVE_BUILD_USER`).
- `execution.py`: `real_run_make`, `run_make_target`, `real_run_olddefconfig`,
  `real_run_modules_install` gain an optional `sandbox: BuildSandbox | None = None` and route their
  `subprocess.run` through `sandbox_run`. `real_read_build_id` (objcopy) is unchanged.
- `workspace.py`: `real_checkout`, `make_checkout`, `clone_tree`, `sync_tree`, `merge_config`,
  `apply_patch`, `_run_git` gain the optional `sandbox`; `clone_tree`/`sync_tree`/modules-staging
  do the `chown` handoff, and `merge_config` `chown`s the written fragment file to the build user.
- `local_libvirt/build.py`: `from_env` builds the `SandboxProvider` and threads it into
  `make_checkout` and the run-step seam closures; `_maybe_publish_modules` `chown`s the staging
  root when sandboxed. `over_transport` passes no sandbox (remote host).
- `config/core_settings.py`: declare and register `KDIVE_BUILD_USER`.

## Error contract

| Condition | `ErrorCategory` |
|---|---|
| euid == 0 and `KDIVE_BUILD_USER` unset/empty | `CONFIGURATION_ERROR` (lane refused: would build as root) |
| euid == 0 and `KDIVE_BUILD_USER` is an unknown account or resolves to uid 0 | `CONFIGURATION_ERROR` |
| build-workspace parent not traversable / warm tree unreadable by the build user | the step's existing category (e.g. `INFRASTRUCTURE_FAILURE` mkdir, `CONFIGURATION_ERROR` rsync) |

The fail-closed message names `KDIVE_BUILD_USER` and points at the build-source-staging doc; it
never echoes a uid that came from the environment beyond the resolved account name.

## Testing

TDD; the real setuid demotion stays `live_vm` (needs root on the KVM host). Unit tests drive the
resolution + kwarg-assembly boundary with `os.geteuid`/`pwd` patched and a fake subprocess runner:

- **Resolution table:** euid ≠ 0 → `None`; euid == 0 + unset → `CONFIGURATION_ERROR` naming the
  setting; euid == 0 + unknown account (`getpwnam` `KeyError`) → `CONFIGURATION_ERROR`;
  euid == 0 + uid-0 account → `CONFIGURATION_ERROR`; euid == 0 + valid account → a `BuildSandbox`
  with the resolved uid/gid/groups/home.
- **`SandboxProvider` memoization:** one resolution across repeated `.get()`; the fail-closed error
  re-raises on every call (so a later seam fails closed too, not silently None).
- **`sandbox_run` kwarg + env assembly:** with a sandbox, the captured `subprocess.run` call
  carries `user=/group=/extra_groups=/umask=` and a child `env` whose `HOME`/`USER`/`LOGNAME` are
  the build user's (not `/root`/`root`), even when the caller passes its own hardened `env`; with
  `None`, none of those kwargs are present and `env` is untouched (a non-root run must not request a
  setuid).
- **Fragment readability:** under a sandbox, `merge_config` leaves the kdump fragment file
  owned by / readable by the resolved build uid (regression test for the umask-0o077 break).
- **Demotion wiring:** each run-step / checkout seam routes through `sandbox_run` with the resolved
  sandbox (fake runner captures the sandbox it was handed); the warm-tree `rsync` argv gains
  `--chown=uid:gid` under a sandbox and omits it without; `clone_tree` `chown`s the empty dir before
  the first git call.
- **Fail-closed fails the BUILD job:** a `build()` with euid patched to 0 and no build user raises
  `CONFIGURATION_ERROR` before any subprocess runs.
- **No-op when unprivileged:** with euid ≠ 0 every existing build/checkout test stays green (default
  `sandbox=None`); no `chown`, no demotion kwargs.

## Docs

- ADR-0214 (decision + rejected alternatives).
- `mcp/resources/_content/build-source-staging.md` (and the config reference): the new
  `KDIVE_BUILD_USER` setting, the root-worker fail-closed behavior, and the two operator
  prerequisites (workspace-parent traversable by the build user; warm tree / patch refs readable
  by it).

## Considered & rejected

See [ADR-0214](../adr/0214-root-build-privilege-drop.md) "Considered & rejected": the
authorization-gate-only option (records consent but still builds as root), refusing the lane
entirely under root (splits kdump and from-source build across workers), a process-wide privilege
drop / re-exec'd build child, `preexec_fn`, uniform demotion of `rsync` (couples to operator
source-tree permissions), eager sandbox resolution at `from_env` (crashes worker startup), and a
separate build-group setting (YAGNI).
