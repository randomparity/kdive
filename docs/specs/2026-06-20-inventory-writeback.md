# Spec: inventory writeback to the live source (M2.7 sub-issue D)

- **Issue:** #641 · **Epic:** #429 · **ADR:** [ADR-0199](../adr/0199-seed-once-runtime-authoritative-inventory.md)
- **Depends on:** #640 (C, the serializer — merged)
- **Status:** Draft

## Problem

Sub-issue C (#640) added `ops.export_systems_toml`, which serializes the live DB inventory
(honoring the override ledger) into a deterministic `systems.toml` document and returns it as
**text** in `data["toml"]`. The text is inert: an operator must hand-copy it into the
version-controlled `systems.toml` and re-apply the `kdive-systems` ConfigMap for the reconciler to
re-read it on a fresh start. This is the same "edit the file, re-apply the ConfigMap" manual loop
ADR-0199 set out to remove for the *runtime-mutation* path; it remains for the *persist* path.

Sub-issue D closes that loop: turn the export text into a write against the live source the app
reads (`KDIVE_SYSTEMS_TOML`), behind an explicit operator opt-in, so an operator-invoked writeback
updates the source the reconciler re-reads and a pod restart reproduces the live inventory from it.

## Scope

In scope:

1. A **writeback adapter seam** (`src/kdive/inventory/writeback.py`) — a port (`WritebackTarget`)
   with two real implementations and a fake:
   - **ConfigMap**: patch the `kdive-systems` ConfigMap's `systems.toml` key via the Kubernetes
     API, using the in-cluster service-account token + CA. Needs an RBAC Role granting `patch`
     (and `get`) on that **one** ConfigMap by name.
   - **Mounted file**: write the PVC-backed file at `KDIVE_SYSTEMS_TOML` directly (atomic
     temp-file + `os.replace`).
   - **Fake**: records the last written text in memory for unit tests.
2. **Wire the export tool**: `ops.export_systems_toml` gains an opt-in `persist: bool = False`
   parameter. When `persist=False` (default) it behaves exactly as today (returns text, writes
   nothing). When `persist=True` it additionally writes the serialized document through the
   configured adapter, and reports the outcome in the response. **A persist of a document that
   still contains a `REPLACE_ME_*` placeholder is refused** (see "Skeleton guard" below): persisting
   an unedited `remote_libvirt` skeleton would write a `systems.toml` that does not parse, silently
   stalling the reconciler's inventory pass.
3. **Operator runbook + RBAC manifest**: a runbook step for the real ConfigMap path, including the
   `Role`/`RoleBinding`/`ServiceAccount` manifest granting `patch`+`get` on `kdive-systems`, and
   how to set the opt-in.

Out of scope (unchanged from C / the milestone):

- Auto-writeback on every mutation (ADR-0199 rejected it; export stays an explicit operator action,
  and writeback is opt-in on top of it).
- Any change to the serializer or the ledger-honoring read (#640 owns those).
- Standing up a real Kubernetes cluster in CI. The real ConfigMap path is a runbook step, verified
  on a real cluster; local tests cover the serializer + the seam with the fake adapter.

## Design

### The port

```python
class WritebackTarget(Protocol):
    target_kind: str  # "configmap" | "file" — for the response/audit, never the secret
    async def write(self, toml_text: str) -> None: ...
```

`write` is the whole contract: persist `toml_text` to the live source, or raise a
`CategorizedError` (`CONFIGURATION_ERROR` for a misconfiguration the operator can fix, e.g. missing
token/path; `PROVIDER_ERROR` for a transport/API failure). It is **not** idempotent-by-content
beyond what the underlying store gives (a ConfigMap patch is last-writer-wins; a file replace is
atomic).

### Selection — a new config setting

A new `KDIVE_INVENTORY_WRITEBACK` setting (group `inventory`, consumed by `server`, where the
`ops.*` tools run) selects the adapter:

- unset / `off` → **no adapter**; `persist=True` returns a `CONFIGURATION_ERROR` telling the
  operator writeback is disabled and how to enable it (no silent no-op — a persist that quietly does
  nothing is the phantom-feature failure mode).
- `configmap` → `ConfigMapWriteback`.
- `file` → `MountedFileWriteback`.

The factory `resolve_writeback_target(config) -> WritebackTarget | None` reads the setting and
constructs the adapter (or `None` when off). Constructing the ConfigMap adapter reads the
namespace/token/CA from the standard in-cluster locations
(`/var/run/secrets/kubernetes.io/serviceaccount/{token,ca.crt,namespace}`) and the API host from
`KUBERNETES_SERVICE_HOST`/`KUBERNETES_SERVICE_PORT`; a missing in-cluster mount is a
`CONFIGURATION_ERROR` at write time (the operator opted in outside a pod).

Additional settings for the two paths:

- `KDIVE_INVENTORY_WRITEBACK_CONFIGMAP` — the ConfigMap name (default `kdive-systems`) and key
  (the file name; reuse the mounted `fileName`, default `systems.toml`).
- The file path is `KDIVE_SYSTEMS_TOML` itself (already exists) — the mounted-file adapter writes
  exactly the file the reconciler reads.

### The ConfigMap patch

A strategic-merge `PATCH` to
`/api/v1/namespaces/{ns}/configmaps/{name}` with body `{"data": {"<key>": "<toml>"}}`,
`Content-Type: application/strategic-merge-patch+json`, `Authorization: Bearer <token>`, TLS pinned
to the service-account CA. `httpx` (already a dependency) is the transport — **no new dependency**.
A non-2xx response raises `PROVIDER_ERROR` with the status code (body redacted: it can echo cluster
detail). 403 specifically maps to `CONFIGURATION_ERROR` ("the RBAC Role is missing or does not grant
`patch` on `kdive-systems`") so the operator gets the actionable fix.

The ConfigMap mount is **read-only** in the pod, so the pod cannot write the mounted file for the
ConfigMap shape — the patch goes through the API server, and kubelet propagates the updated
ConfigMap to the mount (eventually-consistent; the reconciler picks it up on its next pass / a pod
restart re-reads it). This is why the ConfigMap path needs the API write, not a file write.

### The mounted-file path

For a writable `KDIVE_SYSTEMS_TOML` (a writable volume, **not** a ConfigMap mount, which is
read-only), write atomically: serialize to a temp file in the same directory, `os.replace` onto the
target so a reader never sees a half-written file. Offloaded to a thread (`asyncio.to_thread`) so
the event loop is not blocked on disk I/O.

**Cross-pod reach is the operator's responsibility, and the runbook says so plainly.** The
`ops.*` tools run in the **server** pod; the inventory pass that re-reads `systems.toml` runs in
the **reconciler** pod (and the worker resolves connections from it). The default chart mounts
`systems.toml` from a **read-only ConfigMap** on every pod, so the `file` adapter cannot make a
written file visible to the reconciler under the default deployment — the ConfigMap path is the
supported k8s shape. The `file` adapter is for deployments where `KDIVE_SYSTEMS_TOML` already
points at a volume the writer and the reader share (e.g. a single host running all processes, or an
operator-provisioned `ReadWriteMany` PVC mounted on both the server and the reconciler). The chart
does not provision such a PVC; the runbook documents the constraint and does not claim the default
chart supports the `file` path. The `file` adapter writes only the file it is told to; it does not
attempt to reach another pod.

### Tool wiring

`export_systems_toml(pool, ctx, *, persist=False)`:

- `persist=False`: unchanged — serialize, audit the read, return text.
- `persist=True`: resolve the adapter; if `None`, return `CONFIGURATION_ERROR`. Otherwise serialize,
  `await target.write(toml)`, audit the **write** (a distinct audit event/scope from the read so the
  audit trail distinguishes "exported" from "persisted"), and return the text plus
  `data["persisted"] = true` and `data["target"] = target.target_kind`.

`persist=True` is the mutating shape, but the role gate is unchanged (`PLATFORM_OPERATOR`): an
operator who can export can persist. No new RBAC role inside kdive; the *Kubernetes* RBAC is the
new external dependency, documented in the runbook.

Adding a parameter to the existing tool is **not** a new tool — the three-registration set
(registrar / `test_tool_docs` / `exposure.py`) stays as-is; only the tool's signature and generated
doc change.

### Skeleton guard

A `remote_libvirt` block is exported as a skeleton with `REPLACE_ME_*` placeholders for the
file-only connection/debug fields (ADR-0199; the serializer's `_REMOTE_PLACEHOLDERS`). Those are
required fields, so an unedited skeleton does **not** parse. Persisting it to the live source and
restarting would feed the reconciler a malformed `systems.toml`, which per `KDIVE_SYSTEMS_TOML`'s
contract fails the inventory pass quietly (a malformed file is logged and skipped, not fatal) —
i.e. the running inventory silently stops being reconciled, the exact failure the operator was
trying to avoid.

So `persist=True` refuses when the serialized document contains the placeholder marker
(`REPLACE_ME_`): it returns a `CONFIGURATION_ERROR` whose detail tells the operator the export
contains skeleton placeholders that must be completed before the document can be a usable source,
and that they can still take the returned `data["toml"]` text and complete it by hand. The marker
is a single shared constant so the serializer and the guard cannot drift. This guard does not fire
for an inventory with no `remote_libvirt` hosts (or one whose images are all built, not `defined`),
so the common images/build_hosts/cost_classes round-trip still persists cleanly.

## Failure modes & edges

- **Writeback off, `persist=True`** → `CONFIGURATION_ERROR`, names `KDIVE_INVENTORY_WRITEBACK` and
  its accepted values. Not a silent success.
- **Not in a pod, `configmap` selected** → `CONFIGURATION_ERROR` at write (missing token/CA mount).
- **403 from the API** → `CONFIGURATION_ERROR` naming the missing RBAC grant.
- **5xx / network error from the API** → `PROVIDER_ERROR`, status/exception class only (body
  redacted).
- **File path not writable / parent missing (`file` selected)** → `CONFIGURATION_ERROR` naming the
  path.
- **Serializer raises** (e.g. a resource missing a required sizing capability — `_require_int`) →
  the existing serializer `ValueError` surfaces; the write never runs (serialize-then-write order),
  so a bad snapshot never half-persists.
- **Persist of an unedited skeleton** (`REPLACE_ME_*` present) → `CONFIGURATION_ERROR`, refused
  before any write (the skeleton guard); the returned text is still available to complete by hand.
- **Concurrent writeback** → last-writer-wins at the store; acceptable (the operator drives this
  explicitly and serially).
- **ConfigMap mount propagation lag** → after a successful patch, a *running* reconciler in another
  pod re-reads the updated `systems.toml` only after kubelet syncs the ConfigMap to the mount (up to
  its sync period) or on a pod restart. The acceptance signal is "a pod restart reproduces the live
  inventory", not "the next reconcile pass takes immediate effect"; the runbook states this.

## Testing

CI-covered (no cluster, no real file-system dependency beyond a tmp dir):

- **Serializer→fake seam**: `persist=True` with a `FakeWriteback` captures exactly
  `serialize_inventory(snapshot)`; the response reports `persisted=true` and the target kind.
- **Off path**: `persist=True` with writeback unset returns `CONFIGURATION_ERROR` and writes
  nothing.
- **Skeleton guard**: `persist=True` on a snapshot containing a `remote_libvirt` host (whose export
  carries `REPLACE_ME_*`) returns `CONFIGURATION_ERROR` and writes nothing; a snapshot with no
  placeholders (images/build_hosts/cost_classes only) persists. The guard constant is the same one
  the serializer emits (a drift test asserts they match).
- **Factory**: `resolve_writeback_target` returns the right adapter per setting value, `None` when
  off, and a `CONFIGURATION_ERROR` on an unknown value.
- **ConfigMap adapter, transport mocked**: a 200 issues the expected `PATCH` (URL, headers,
  strategic-merge body, key); a 403 → `CONFIGURATION_ERROR`; a 500 → `PROVIDER_ERROR` with the body
  redacted; a missing in-cluster token → `CONFIGURATION_ERROR`. The HTTP boundary is mocked (it is
  the external service); the adapter logic is exercised directly.
- **Mounted-file adapter**: writes to a tmp path, asserts the file content equals the toml and the
  write is atomic (temp file gone, target replaced); a non-writable dir → `CONFIGURATION_ERROR`.
- **Auth gate**: a non-operator caller gets `authorization_denied` even with `persist=True` (the
  denial path runs before any writeback).
- **Doc/tool**: the generated tool reference reflects the new `persist` parameter
  (`just docs`), and the config reference reflects the new settings (`just config-docs`,
  `just env-docs-check`).

Runbook-only (not CI):

- The real ConfigMap patch against a live cluster: apply the RBAC manifest, set
  `KDIVE_INVENTORY_WRITEBACK=configmap`, invoke `ops.export_systems_toml(persist=true)`, confirm the
  `kdive-systems` ConfigMap's `systems.toml` key updated, restart a pod, confirm the live inventory
  reproduces.

## Rollout / rollback

- Default `KDIVE_INVENTORY_WRITEBACK` unset → no behavior change; `persist` defaults to `False`;
  existing `ops.export_systems_toml` callers are unaffected.
- Rollback is config-only: unset the setting (writeback off) — no schema, no migration, no data
  change. The serializer and ledger are untouched.
