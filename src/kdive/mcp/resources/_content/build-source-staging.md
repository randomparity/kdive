# Staging kernel source for `runs.build`

The server-build lane (`runs.build` on a Run whose `build_profile` has
`source="server"`) needs a kernel source tree to build from. The lane a Run takes is decided
by the **provenance form** of its `kernel_source_ref` (a bare string vs a structured git
object) and the build host it runs on. Picking the wrong combination is the most common
reason a first build fails, so this page covers each lane.

This is an **operator** prerequisite: a caller cannot stage a warm tree, allowlist a local
git remote, or register a remote build host over the MCP surface alone. The error messages
from `runs.create`/`runs.build` name the step that is missing and point here.

## The build lanes

| Lane | `kernel_source_ref` form | Build host | Operator prerequisite |
|---|---|---|---|
| Warm-tree (local) | a bare string label/path, e.g. `linux-6.9` or `/srv/linux` | the seeded `worker-local` host | stage `KDIVE_KERNEL_SRC` on the worker |
| Git-clone (local) | the structured object `{"git": {"remote": "…", "ref": "…"}}` | the seeded `worker-local` host | allowlist the remote in `KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST` |
| Git-clone (remote) | the structured object `{"git": {"remote": "…", "ref": "…"}}` | a registered **remote** build host | register the host with `build_hosts.register_ssh` (or `…_ephemeral_libvirt`) |

A **bare string is always warm-tree provenance metadata**, never git-clone provenance —
even one that looks like a git URI (`git:…`, `git+ssh://…`, `https://…`). Those URI-looking
bare strings are rejected at `runs.create` with a message pointing at the structured form,
because they would otherwise be silently routed to the local warm-tree lane and fail later.
For a git build you must pass the structured `{"git": {...}}` object.

## Warm-tree lane: stage `KDIVE_KERNEL_SRC`

The local `worker-local` build host materializes each build's workspace by mirroring a
pre-staged kernel source tree into scratch (`rsync -a --delete`). The tree's path is the
worker-process setting `KDIVE_KERNEL_SRC` (see the [config reference](../guide/reference/config.md));
its default is empty, so a fresh deploy has no warm tree and a warm-tree build fails with:

> a local (`worker-local`) build requires the operator to pre-stage a warm kernel source
> tree (`KDIVE_KERNEL_SRC`)

To stage it:

1. Place a kernel source tree on the **worker** host (a git checkout or an unpacked tarball;
   the build runs `make` against it, so it must be a buildable tree, not a bare repo).
2. Set `KDIVE_KERNEL_SRC` to its **absolute** path in the worker process's environment
   (the same place you set the other `KDIVE_*` worker settings — systemd unit, compose
   `environment:`, or Helm `config.*`).
3. Restart the worker so it reads the new value.

`KDIVE_KERNEL_SRC` must be an absolute path to an existing directory. A relative path, a
non-existent path, or a filesystem root is rejected at build time with a distinct
"not a usable absolute path to an existing tree" error.

A bare `kernel_source_ref` in the Run's profile is provenance metadata only — it labels the
build, it does **not** override `KDIVE_KERNEL_SRC`. The worker always builds from the staged
tree.

For the full local provider prerequisites (toolchain, disk space, fixtures) see
[Local libvirt](providers/local-libvirt.md).

## Git-clone lane (local): structured ref + an allowlisted remote

The `worker-local` host can clone an agent-supplied git remote directly, so a developer can
build a fork's branch on the default worker without standing up a remote build host. Because
the worker runs in the control plane, the remotes it may clone are **deny-by-default** and
gated by an operator allowlist.

1. Submit the structured provenance object in the Run's build profile (same form as the
   remote lane), leaving `build_host` unset (or naming `worker-local`):

   ```json
   "kernel_source_ref": {"git": {"remote": "https://github.com/myorg/linux", "ref": "v6.9"}}
   ```

2. Set `KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST` on the worker to the remotes you trust it to
   clone. It is a comma-separated list; each entry is a **host** (`github.com`) or a
   **host/path-prefix** (`github.com/myorg`). A host entry admits any path on that host; a
   path-prefix entry matches only at a `/` boundary (`github.com/myorg` does not admit
   `github.com/myorg-evil`). Only `https`, `ssh`, and `git` remotes are eligible; `file://`
   and `http://` are rejected. Restart the worker after changing it.

   ```
   KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST=github.com/myorg,git.example.com
   ```

3. `ref` must be a server-advertised tag or branch. A bare commit SHA is not guaranteed
   fetchable by the shallow clone and surfaces as a `git fetch` failure.

If the allowlist is empty or unset, the local git lane is **off**: a git build on the local
host fails at build time with a `configuration_error` that says local git builds are disabled.
A remote whose host/path is not on a non-empty allowlist fails with a distinct
`configuration_error` that the remote is not allowlisted. Neither message echoes the submitted
remote URL.

## Git-clone lane (remote): structured ref + a remote build host

For a build that clones a git ref on a dedicated build host instead of the worker:

1. Submit the structured provenance object in the Run's build profile:

   ```json
   "kernel_source_ref": {"git": {"remote": "https://github.com/torvalds/linux", "ref": "v6.9"}}
   ```

2. Register a **remote** build host so the clone-and-build runs off the worker. Register an
   SSH or ephemeral-libvirt host with the operator tools:

   - `build_hosts.register_ssh` — an SSH-reachable build host.
   - `build_hosts.register_ephemeral_libvirt` — a per-build throwaway libvirt VM.

   See [Remote libvirt host setup](runbooks/remote-libvirt-host-setup.md) for preparing the
   host, and `build_hosts.list` to confirm it is registered and reachable.

3. Name the host in the build profile's `build_host` field (or leave it to the default
   selection once a compatible remote host is registered).

## Related

- [Config reference](../guide/reference/config.md) — `KDIVE_KERNEL_SRC` and the other
  worker settings.
- [Local libvirt](providers/local-libvirt.md) — the `worker-local` provider prerequisites.
- [Remote libvirt host setup](runbooks/remote-libvirt-host-setup.md) — preparing a remote
  build/boot host.
