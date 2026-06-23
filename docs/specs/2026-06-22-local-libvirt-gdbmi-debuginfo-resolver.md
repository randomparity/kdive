# Spec — local-libvirt gdb-MI debuginfo resolver (`debug.set_breakpoint` and the symbol ops)

- **Issue:** #702 (M2.8 Epic B, B1 follow-up)
- **ADR:** [ADR-0210](../adr/0210-local-libvirt-live-debug-introspection.md) §1 (anchor;
  do not re-decide — the gdbstub transport resolution and the "session-bound `debug.*` ops run
  against it through the unchanged `GdbMiEngine`" decision). Builds on
  [ADR-0034](../adr/0034-debug-plane-gdbmi-tier.md) (the gdb-MI engine + attach/symbol seam split)
  and [ADR-0083](../adr/0083-remote-connect-debug-plane.md) (the provider owns only its host policy
  + debuginfo resolver over the provider-neutral engine). No new ADR: this is the production
  wiring ADR-0210 §1 already decided, mirroring how B2 (#676) wired its `from_env` seams under the
  same ADR.
- **Design doc:** [m2.8-local-libvirt-service-parity](../design/m2.8-local-libvirt-service-parity.md)
- **Status:** Accepted

## Problem

On local-libvirt, B1 (#675/ADR-0210 §1) made `debug.start_session(gdbstub)` resolve a real
`(host, port)` from the running domain XML and open a live transport — the session reaches `live`.
But the **first gdb-MI op that needs symbols fails** before gdb is ever spawned. Driving live on
real KVM (B6 #680):

```
debug.set_breakpoint(session_id=…, location="schedule")
→ debug_attach_failure: "resolving a Run's debuginfo object runs only under the live_vm gate"
```

The cause is `providers/local_libvirt/debug/gdbmi.py::_resolve_debuginfo_ref`, an unimplemented
stub that unconditionally raises `MISSING_DEPENDENCY` (the handler re-tags it
`DEBUG_ATTACH_FAILURE`, `ops.py::_op_failure`). The production attach path
`default_attach_seam` (wired at `composition.py:124` as `attach_seam=default_attach_seam`) calls
that stub first, so it never reaches `GdbMiEngine.attach`, never spawns gdb, never loads symbols.
Every session-bound symbol op (`set_breakpoint`, `read_registers`, `read_memory`, `continue`,
`interrupt`, `list_breakpoints`) therefore fails on local even though the transport is live.

The build **does** publish the debuginfo object: the Run's `debuginfo_ref` is set to the build
plane's `vmlinux` object key (`build.py` publishes `vmlinux`; `complete_build.py` / `runs_shared.py`
persist `runs.debuginfo_ref`). The object exists. The resolver just has to look the ref up on the
Run row and fetch the bytes to the temp path the seam already computes — "mirroring the Retrieve
plane's lookup", as its own docstring says — instead of raising.

This is the exact shape B2 (#676) already had and fixed: the orchestration (the seam that computes
the temp path, spawns gdb, connects RSP) is real and `live_vm`-gated; only one `from_env`-style
production seam was a placeholder that raised `MISSING_DEPENDENCY`. The fix wires that seam to the
real sync DB + object-store reads that already exist elsewhere in the tree.

## Decision

Per ADR-0210 §1, implement the production debuginfo resolver for local-libvirt's gdb-MI attach
seam. The `AttachSeam` protocol, the `GdbMiEngine`, the descriptor (`supported_debug_transports`
already carries `gdbstub` from B1), and the tool maturity are **unchanged**. Only the
stub-that-raises becomes a real lookup+fetch, split so the orchestration stays unit-tested with
fakes and only the DB/store IO and the gdb spawn are `live_vm`-real.

### 1. Split the resolver into orchestration + injectable IO seams

Introduce a small `DebuginfoResolver` (in `providers/local_libvirt/debug/gdbmi.py`) with two
injected seams, mirroring `LocalLibvirtVmcoreIntrospect`'s `fetch_object`/`open_program` split and
`build_config_fetch_from_env`'s lazy sync pattern:

- `read_debuginfo_ref: Callable[[str], str | None]` — given a Run id, return the Run's
  `debuginfo_ref` (the build plane's `vmlinux` object key) or `None` when the row has none.
- `fetch_object: Callable[[str], bytes]` — given an object key, return its bytes (the same
  object-store read `introspect._real_fetch_object` and `build_config_fetch_from_env` already use).

`resolve(run_id, dest)` orchestrates:

1. `ref = read_debuginfo_ref(run_id)`. A `None` ref is a **legitimate, typed error**, not a silent
   skip: raise `CONFIGURATION_ERROR` with `details={"run_id", "reason": "no_debuginfo"}` and the
   message "the Run has no published debuginfo object; build the kernel before attaching gdb".
   This is the same precondition `_vmcore_targets.NO_DEBUGINFO` names for the vmcore path; here the
   caller is the attach seam, so it surfaces as a configuration error the agent can act on
   (`runs.build`), never a `MISSING_DEPENDENCY` (which would falsely imply a missing host tool) and
   never a `None` the seam would then hand to gdb as a non-existent path.
2. `data = fetch_object(ref)`; write `data` to `dest` (the temp vmlinux path the seam computed).
   Return `dest`. The fetch's own errors (object-store IO / a vanished object) propagate as the
   object store's typed `CategorizedError` exactly as they do for the introspect/crash paths.

The orchestration is pure: a test injects a fake `read_debuginfo_ref` and `fetch_object` and asserts
(a) a present ref is fetched and written to `dest`, (b) a `None` ref raises the `no_debuginfo`
`CONFIGURATION_ERROR` **before** any fetch, (c) a fetch that raises propagates unchanged. No DB, no
object store, no gdb.

### 2. Wire the real sync seams in `default_attach_seam`

`default_attach_seam` builds a `DebuginfoResolver` whose seams are the real lazy sync reads, then
calls `resolve(run_id, vmlinux_path)` before `GdbMiEngine().attach(...)`:

- `read_debuginfo_ref = _real_read_debuginfo_ref` — opens a short-lived **sync** `psycopg`
  connection (`psycopg.connect(config.require(DATABASE_URL))`) and reads `runs.debuginfo_ref` for
  the run id via a new sync query `debuginfo_ref_for_run_sync(conn, run_id)`. Sync because the seam
  runs off the event loop in `asyncio.to_thread` (`ops.py:227` dispatches `_attach_and_run` to a
  thread) and owns no async pool — identical to `build_config_fetch_from_env`. The DB is resolved
  lazily inside the call, so constructing the runtime (`build_runtime`) still opens no connection
  and a host without a reachable DB only fails when an attach is actually attempted.
- `fetch_object = _real_fetch_object` — the same object-store read the introspect port already
  defines (reuse `providers/shared`'s `default_fetch_object` so there is one object-fetch seam, not
  a third copy).

The seam stays `# pragma: no cover - live_vm` (it spawns gdb); the resolver class and its
orchestration are **not** live-gated and are unit-tested. `default_attach_seam` no longer discards
the resolved ref (today's `del debuginfo_ref`): it materializes the vmlinux to `vmlinux_path` so
`GdbMiEngine.attach`'s `resolved_vmlinux.is_file()` check passes against a real symbol file.

### 3. Composition wiring stays minimal

`composition.py:124` keeps `attach_seam=default_attach_seam` — a bare function reference, unchanged.
The lazy DB/store construction lives inside the seam (not in `build_runtime`), so `composition.py`
gains no new dependency and `build_runtime` opens no connection. This keeps the cross-agent surface
on `composition.py` (shared with #703's `xml.py` work) at zero lines.

## Acceptance criteria

- **CI (fakes):**
  - `DebuginfoResolver(read_debuginfo_ref=…, fetch_object=…).resolve(run_id, dest)` with a fake that
    returns a present ref writes the fetched bytes to `dest` and returns `dest`; the fake
    `fetch_object` is called exactly once with that ref.
  - `resolve` with a `read_debuginfo_ref` that returns `None` raises `CONFIGURATION_ERROR` with
    `details["reason"] == "no_debuginfo"` and `details["run_id"]`, and `fetch_object` is **never
    called** (the absent debuginfo is a legitimate error caught before any fetch, not a silent
    `None`).
  - `resolve` propagates a `CategorizedError` raised by `fetch_object` unchanged (object-store IO
    failure path).
  - The pre-existing `test_debuginfo_resolver_default_raises_missing_dependency` is **replaced** (not
    left asserting the stub): the module-level `_resolve_debuginfo_ref` stub is gone, so that test is
    deleted and the three resolver-orchestration tests above stand in its place. No test asserts the
    old "runs only under the live_vm gate" `MISSING_DEPENDENCY` for this seam anymore.
  - The full existing `test_debug_gdbmi.py` engine suite stays green (engine behavior unchanged).
  - `just ci` (lint, type whole-tree, lint-shell, lint-workflows, check-mermaid, test) is green.
- **Live (KVM host) — orchestrator post-merge, NOT this PR:**
  `debug.start_session(gdbstub)` → `debug.set_breakpoint(location="schedule")` →
  `debug.read_registers` succeeds end-to-end against a real booted local System whose Run carries a
  published `debuginfo_ref`. The `debug.*` `partial` → `implemented` promotion is owned by the B6
  re-drive PR (#680), not this one — this PR leaves the maturity metadata untouched.

## Out of scope

- **Maturity promotion.** `debug.*` stays `partial`; the live-proof promotion is a separate
  post-re-drive PR the orchestrator owns. No `mcp/` maturity metadata changes here.
- **The descriptor.** `supported_debug_transports` already carries `gdbstub` (B1, #675); this change
  does not touch the runtime descriptor.
- **The remote provider's identical `_resolve_remote_debuginfo_ref` stub**
  (`remote_libvirt/debug/gdbmi.py:20`). The issue notes it is "likely the same gap"; it is a
  separate provider with its own live-proof and is **flagged, not fixed**, here (different file,
  different live drive). Filed/noted to the orchestrator.
- **Any change to the shared `GdbMiEngine`, the `AttachSeam` protocol, or `ops.py`/`session_registry`
  attach orchestration.** Those are correct; only the local resolver seam was a stub.
- **#703's domain-XML vmcoreinfo work.** Different file (`lifecycle/xml.py`); the only shared file is
  `composition.py`, which this change leaves at zero edits.

## Risks & failure modes

- **Wrong error category for an absent debuginfo.** Returning `None` (silent) would hand gdb a
  non-existent vmlinux path and surface as an opaque `bad_vmlinux_path` config error deep in
  `attach`; raising `MISSING_DEPENDENCY` would falsely claim a missing host tool. Mitigation: the
  resolver raises a precise `CONFIGURATION_ERROR` with `reason=no_debuginfo` and an actionable
  message, asserted by a dedicated test, and `fetch_object` is proven un-called on that path.
- **Event-loop starvation.** The seam runs in `asyncio.to_thread` (`ops.py:227`), so a **sync**
  DB+store read is correct and does not block the loop. Using an async pool here would be wrong (no
  pool in the thread) — the sync `psycopg.connect` mirrors `build_config_fetch_from_env`, the
  established precedent for "this seam runs in a thread and owns no async pool".
- **A second object-fetch implementation drifting from the introspect one.** Mitigation: reuse the
  shared `default_fetch_object` seam rather than adding a third copy; the resolver's `fetch_object`
  is that same provider-neutral seam.
- **Secret leakage.** The resolver moves only an object **key** (a system-produced ref) and raw
  vmlinux **bytes** to a temp file; it returns no guest/console text and persists nothing. All
  textual gdb/MI output is still redacted by the engine downstream (unchanged). No new redaction
  boundary is needed, and the resolver returns a `Path`, never echoed prose.
- **Cross-agent conflict on `composition.py`.** #703 may also touch `composition.py`. Mitigation:
  this change makes **zero** edits to `composition.py` (the seam ref is already wired); the only
  files touched are `debug/gdbmi.py`, a new sync query in the db layer, and the test file.
