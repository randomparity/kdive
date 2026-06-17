# Local warm-tree build admission: reject empty/invalid `KDIVE_KERNEL_SRC` at the worker job boundary (#532)

- **Status:** Draft
- **Date:** 2026-06-17
- **ADR:** [0158](../adr/0158-local-warm-tree-build-admission.md)
- **Issue:** #532
- **Related:** #534 / [ADR-0157](../adr/0157-create-time-build-host-source-check.md)
  (sibling source-kind check), #533 (diagnostics surfacing — out of scope here)

## Problem

The local build lane (`build_host=worker-local`, `kind='local'`) has no usable kernel
source in the demo env, and the misconfiguration is caught too late. A warm-tree
`runs.build` fails only when the BUILD job runs `sync_tree`:

```
configuration_error: This warm-tree build has no kernel source: KDIVE_KERNEL_SRC is
not set on the build worker. <build-lane guidance>
```

Two defects, one issue:

1. **Late validation.** `KDIVE_KERNEL_SRC` defaults to `""`, is worker-scoped
   (`config/core_settings.py:280-287`, `processes=_WORKER`), passes `config.require()`
   (which rejects only `None`), and is baked into the build closure at worker
   composition (`LocalLibvirtBuild.from_env` → `make_checkout`,
   `providers/local_libvirt/build.py:127,133`). The emptiness/usability check lives in
   `sync_tree` (`providers/shared/build_host/workspace.py:183-197`) — after the BUILD
   job has resolved the host, entered `build()`, and begun `build_workspace` (per-run
   `mkdir`, then rsync). The failure is deep in the job's side effects, not at the
   point the job is admitted into execution.

2. **No working demo path.** No `systems.toml`, example, seed, or compose env sets
   `KDIVE_KERNEL_SRC` for the seeded `worker-local` host. A fresh demo deploy has a
   registered local build host that can never build.

## Why not `runs.build` server admission (the issue's suggestion)

`runs.build` runs in the **server** process. `KDIVE_KERNEL_SRC` is read only in the
**worker** at builder composition; the value is not in the BUILD payload
(`jobs/payloads.py` — `BuildPayload` carries `run_id`, `cmdline`, `build_host_id`
only) and the server never reads it (verified: the only `config.require(KERNEL_SRC)`
sites are `providers/local_libvirt/build.py:127` and
`providers/remote_libvirt/build.py:153`, both worker-composition). The canonical demo
(`docker-compose.yml`) runs `server` and `worker` as **separate services with separate
`environment:` blocks**; the server's env does not carry `KDIVE_KERNEL_SRC`, and by the
ADR-0087 process-scoping contract should not. A server-side read is therefore either
unavailable or a false pass. The earliest boundary that holds the authoritative value
is the **worker BUILD job entry**. See ADR-0158 "Considered & rejected" for the full
disposition (also: worker-startup `required_when`, create-time, and dropping the
backstop — all rejected).

## Design

### The reusable admission helper (`services/runs/build_host_selection.py`)

A pure function checks the warm-tree source for a `LOCAL` host and raises on an unusable
value, reusing the predicate and messages `sync_tree` already owns:

```python
def check_warm_tree_source_admission(
    kernel_src: str, *, host_kind: BuildHostKind
) -> None:
    """Reject a LOCAL warm-tree build whose KDIVE_KERNEL_SRC is unset or unusable.

    No-op for non-LOCAL hosts (git lanes do not read KDIVE_KERNEL_SRC). For a LOCAL
    host, applies the same emptiness/usability predicate sync_tree applies and raises
    the identical KERNEL_SRC_UNSET_DETAIL / KERNEL_SRC_INVALID_DETAIL
    (CONFIGURATION_ERROR), so an admission rejection is byte-identical to the
    (now-backstop) build-time one.
    """
```

- Inputs are primitives (`str`, `BuildHostKind`) — no DB, no profile — so both the
  call site and unit tests drive it directly.
- The predicate and the two message constants stay single-sourced in
  `providers/shared/build_host/workspace.py`. To avoid duplicating the
  emptiness/usability logic, factor `sync_tree`'s leading guard into a small reusable
  predicate (e.g. `warm_tree_source_error(kernel_src) -> str | None` returning the
  offending message or `None`) that **both** `sync_tree` and the new helper call. No
  message string is copied; `sync_tree`'s observable behavior is unchanged.

### Call site (worker BUILD handler)

`jobs/handlers/runs.py` `_build_handler_autocommit` already resolves the host and the
profile before building. Insert the admission check immediately before the build runs,
inside `_build_and_record` after `_resolve_build_host` returns the host and before
`builder.build(...)` is invoked, using the worker-composed `KDIVE_KERNEL_SRC`.

The worker reads `KDIVE_KERNEL_SRC` once (it is worker-scoped and already required at
composition); the handler passes that same value (or re-reads via `config.get`) into
the helper together with `host.kind`. Concretely: the value flows from the resolved
runtime/builder or a direct `config.get(KERNEL_SRC)` read in the handler — whichever
keeps the value's single read-point honest (the implementer chooses the lower-coupling
option; both read the same worker env snapshot).

The `over_transport` (remote git on a `LOCAL`-tenant transport) path does not apply:
that path is taken only for git source on a remote host, where `host_kind` is not
`LOCAL`, so the no-op branch covers it.

### Demo path (part 2): documented one-step bootstrap + commented compose stanza

- Extend `docs/operating/build-source-staging.md` with a "Demo / compose bootstrap"
  subsection: bind-mount a buildable kernel tree into the `worker` service and set
  `KDIVE_KERNEL_SRC` to the mount path, with the exact two lines to add.
- Add the same as a **commented, copy-ready** stanza in `docker-compose.yml`'s
  `worker` service (a `# KDIVE_KERNEL_SRC: /srv/linux` env line and a
  `# - /path/to/linux:/srv/linux:ro` volume line), so the bootstrap is discoverable
  where the operator already looks.
- No kernel bytes are committed and no auto-download is added (ADR-0158 rationale).

## Acceptance criteria (falsifiable)

1. A warm-tree profile on a `LOCAL` host (`worker-local`) with empty `KDIVE_KERNEL_SRC`
   is rejected at the worker BUILD handler **before** `build()`/`build_workspace` runs,
   with `CONFIGURATION_ERROR` and the exact `KERNEL_SRC_UNSET_DETAIL` string. (test)
2. A whitespace-only `KDIVE_KERNEL_SRC` is rejected identically (the `.strip()` edge).
   (test)
3. A non-empty-but-unusable `KDIVE_KERNEL_SRC` (relative path, non-existent path, or
   filesystem root) is rejected at admission with `KERNEL_SRC_INVALID_DETAIL` /
   `CONFIGURATION_ERROR`. (test)
4. A usable absolute `KDIVE_KERNEL_SRC` (existing directory) is **admitted** (the helper
   is a no-op / returns) for a `LOCAL` host. (test)
5. The helper is a **no-op for non-`LOCAL`** host kinds (`SSH`, `EPHEMERAL_LIBVIRT`)
   regardless of `KDIVE_KERNEL_SRC` value, including empty. (test)
6. `sync_tree`'s existing checks remain and still raise the same two messages when
   reached directly (the backstop is intact and its tests still pass). (test)
7. The two message constants and the emptiness/usability predicate have exactly one
   definition; no string is duplicated between `workspace.py` and
   `build_host_selection.py`. (test/grep + review)
8. `docs/operating/build-source-staging.md` documents the demo compose bootstrap and
   `docker-compose.yml` carries the commented stanza; doc guardrails
   (`docs-links`, `docs-paths`, doc-style) pass. (guardrail)

## Out of scope

- #533 diagnostics/preflight surfacing of the dead local lane.
- Any change to the server `runs.build` / `runs.create` boundaries (ADR-0157 owns the
  source-kind check there; this spec adds nothing server-side).
- Bundling/auto-downloading a kernel tree.
