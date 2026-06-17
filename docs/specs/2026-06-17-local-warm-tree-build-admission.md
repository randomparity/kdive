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

### Call site (the dispatch LOCAL branch)

The check goes in `run_build_on_host` (`providers/shared/build_host/dispatch.py:45`),
**not** in `jobs/handlers/runs.py`. That function is the single seam that already
discriminates LOCAL from transport:

```python
async def run_build_on_host(builder, host, run_id, parsed, *, secret_registry, ...):
    if host.kind is BuildHostKind.LOCAL:
        return await asyncio.to_thread(builder.build, run_id, parsed)
    ...  # transport (git/remote) path — KDIVE_KERNEL_SRC is never read here
```

Insert the admission check at the top of the `LOCAL` branch, before
`asyncio.to_thread(builder.build, ...)`. This is the earliest point that (a) knows the
host is LOCAL and (b) is about to run the warm-tree build, so it fails before any
workspace side effect (`build()` → `build_workspace` → per-run `mkdir` → rsync). The
`_build_and_record`/`_run_build` handler frames are **not** the call site: they hold
only the `builder` object, never `kernel_src` (the value is sealed in the builder's
checkout closure at composition, `make_checkout(kernel_src,...)`), and they do not
branch on `host.kind`. Putting the check upstream would force a redundant `host.kind`
re-derivation away from the one branch that already owns it.

**Single read-point for `KDIVE_KERNEL_SRC`.** The admission check reads the value once,
via `config.get(KERNEL_SRC)` at the dispatch LOCAL branch. We do **not** thread the
closure-captured value out of the builder (that would widen the `Builder` port across
all providers for one provider's concern) and we do **not** add a second read elsewhere.
In a live worker `config.load()` snapshots the env once at startup
(`__main__.py`) and is not reset during operation (reset is the test-only autouse
fixture), so this admission read and the composition read resolve against the **same
worker snapshot** — the rejection is byte-identical to the build-time backstop by
construction, not by coincidence. The check is a no-op for any non-`LOCAL` host kind
because it lives inside the `LOCAL` branch; the `over_transport`/git path never reaches
it and never reads `KDIVE_KERNEL_SRC`.

Because the check raises `CONFIGURATION_ERROR` from inside the `builder.build` dispatch,
it propagates up through `_run_build` → `_build_and_record`'s `except CategorizedError`
exactly as a build-time `sync_tree` rejection does today: same `_fail_build` path, same
terminal-after-retries behavior. A LOCAL host holds **no** build-host lease
(`_release_build_lease` is a no-op DELETE for local; leases are inserted only for
non-LOCAL hosts in `resolve_and_admit`), so the retain-on-failure semantics strand
nothing. The admission rejection therefore changes *when* the same failure happens
(before workspace materialization instead of inside it), not the lease/retry contract.

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
   is rejected at the dispatch `LOCAL` branch **before** `builder.build`/`build_workspace`
   runs (asserted via a `builder.build` that records whether it was called), with
   `CONFIGURATION_ERROR` and the exact `KERNEL_SRC_UNSET_DETAIL` string. (test)
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
   definition in `providers/shared/build_host/workspace.py`; the admission helper and
   `sync_tree` both call the shared predicate, and no message string is duplicated in
   `build_host_selection.py` or `dispatch.py`. (test/grep + review)
8. A `LOCAL` warm-tree build rejected at admission for empty/invalid `KDIVE_KERNEL_SRC`
   strands no build-host lease and follows the same terminal-after-`max_attempts`
   contract as a build-time `sync_tree` rejection (LOCAL holds no lease; the rejection
   flows through `_build_and_record`'s existing `except CategorizedError`). (test)
9. `docs/operating/build-source-staging.md` documents the demo compose bootstrap and
   `docker-compose.yml` carries the commented stanza; doc guardrails
   (`docs-links`, `docs-paths`, doc-style) pass. (guardrail)

## Out of scope

- #533 diagnostics/preflight surfacing of the dead local lane.
- Any change to the server `runs.build` / `runs.create` boundaries (ADR-0157 owns the
  source-kind check there; this spec adds nothing server-side).
- Bundling/auto-downloading a kernel tree.
