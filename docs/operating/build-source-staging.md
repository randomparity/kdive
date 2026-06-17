# Staging kernel source for `runs.build`

The server-build lane (`runs.build` on a Run whose `build_profile` has
`source="server"`) needs a kernel source tree to build from. There are two lanes, and which
one a Run takes is decided by the **provenance form** of its `kernel_source_ref`. Picking
the wrong form is the most common reason a first build fails, so this page covers both.

This is an **operator** prerequisite: a caller cannot stage a warm tree or register a remote
build host over the MCP surface alone. The error messages from `runs.create`/`runs.build`
name the step that is missing and point here.

## The two lanes

| Lane | `kernel_source_ref` form | Build host | Operator prerequisite |
|---|---|---|---|
| Warm-tree (local) | a bare string label/path, e.g. `linux-6.9` or `/srv/linux` | the seeded `worker-local` host | stage `KDIVE_KERNEL_SRC` on the worker |
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

`KDIVE_KERNEL_SRC` must be an absolute path to an existing directory. An unset/empty value, a
relative path, a non-existent path, or a filesystem root is rejected when the worker admits the
build job — before it materializes a workspace — with the
`KDIVE_KERNEL_SRC is not set on the build worker` / `not an absolute path to an existing kernel
source tree` configuration error (the worker also re-checks at sync as a backstop; ADR-0160).

A bare `kernel_source_ref` in the Run's profile is provenance metadata only — it labels the
build, it does **not** override `KDIVE_KERNEL_SRC`. The worker always builds from the staged
tree.

For the full local provider prerequisites (toolchain, disk space, fixtures) see
[Local libvirt](providers/local-libvirt.md).

### Demo / compose bootstrap (one step)

The bundled `docker-compose.yml` does not stage a kernel tree (none is shipped — a buildable
tree is hundreds of MB and version/licence-coupled). To make `worker-local` buildable in the
compose demo, bind-mount a buildable tree into the `worker` service and point
`KDIVE_KERNEL_SRC` at the mount:

1. Have a buildable kernel tree on the host, e.g. `~/src/linux` (a git checkout or an unpacked
   tarball — not a bare repo).
2. In `docker-compose.yml`'s `worker` service, uncomment the two lines the file marks for this
   (a `KDIVE_KERNEL_SRC` env entry and a read-only bind-mount), and set the host path to your
   tree:

   ```yaml
   worker:
     environment:
       KDIVE_KERNEL_SRC: /srv/linux
     volumes:
       - ~/src/linux:/srv/linux:ro
   ```
3. `docker compose up -d worker` (or restart it) so it reads the value.

Until then, a warm-tree `runs.build` against `worker-local` is rejected when the worker admits
the build job (before any workspace is materialized), rather than failing deep in the build.

## Git-clone lane: structured ref + a remote build host

For a build that clones a git ref instead of mirroring a warm tree:

1. Submit the structured provenance object in the Run's build profile:

   ```json
   "kernel_source_ref": {"git": {"remote": "https://github.com/torvalds/linux", "ref": "v6.9"}}
   ```

2. Register a **remote** build host so the clone-and-build has somewhere to run. A git ref
   cannot build on the local `worker-local` host (it requires a warm tree); host selection
   rejects that combination. Register an SSH or ephemeral-libvirt host with the operator
   tools:

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
