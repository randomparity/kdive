# M2 — Remote libvirt (Integration Contract)

## Purpose

M2 adds the **first real second provider**: an independent `remote_libvirt` package that
drives the full spine — allocate → provision → build → install → boot → attach → force-crash →
capture vmcore → release — against a **genuinely remote** libvirt/QEMU host the MCP server and
worker tier do **not** share a filesystem with. It is the first provider that resolves a
**real secret** (an x509 client cert reaching `qemu+tls://` libvirtd) and the first that moves
artifacts across a network boundary, so it converts the secret-by-reference→redaction contract
and the object-store distribution model from synthetic (M1.5) to production.

M2 has **two co-equal goals**, and a change to core counts as a design smell to refactor away,
not a cost to accept:

1. **Working remote capability** — an operator drives the whole spine on a real remote TLS host.
2. **The portability hypothesis, made checkable** — the M2 merge touches **zero net lines** in
   core (`domain` / `db` / `jobs` / `reconciler` / `services` / `store` / `security` and the
   `mcp` server skeleton) and the MCP tool surface (`mcp/tools/*`), modulo a small explicit
   allowlist (the `ResourceKind` enum value, `providers/composition.py` registration, the one
   migration, regenerated docs). This is the top-level design's falsifiable hypothesis
   (`top-level-design.md` §Roadmap), measured against a real second provider for the first
   time.

`local_libvirt` is a bootstrap headed for removal once `remote_libvirt` is enabled in
production (the MCP server runs separately from the libvirt-enabled development hosts). **M2
keeps `local_libvirt`** as the default and the test/baseline backbone — its removal is an
explicit follow-up milestone, not M2 — so the falsifiability diff has a stable "before"
baseline and the M1.2 / M1.5 scaffolding standing on local-libvirt is undisturbed.

- **Decisions:** [ADR-0076](../adr/0076-remote-libvirt-provider-package.md) (the independent
  `remote_libvirt` package + `ResourceKind.REMOTE_LIBVIRT` + composition registration + the
  portability diff gate), [ADR-0077](../adr/0077-qemu-tls-control-transport.md) (`qemu+tls://`
  control transport + x509 client-cert secret-by-reference),
  [ADR-0078](../adr/0078-object-store-in-target-install-seam.md) (object-store +
  presigned-URL **in-target install/retrieve seam**; direct-kernel boot retired as a
  carry-forward), [ADR-0079](../adr/0079-remote-live-debug-transport.md) (remote live-debug
  reachability — direct-TCP gdbstub, in-guest drgn, worker-side vmcore postmortem). All build
  on [ADR-0071](../adr/0071-per-kind-provider-runtime-registry.md) (the per-kind
  `ProviderRuntime` registry this plugs into), [ADR-0063](../adr/0063-typed-provider-runtime.md)
  (the typed port seam the package satisfies), [ADR-0012](../adr/0012-secret-backend.md) /
  [ADR-0027](../adr/0027-safety-modules-secret-backend-impl.md) (the register-before-return
  `SecretBackend` the TLS cert flows through), [ADR-0013](../adr/0013-object-store-layout-retention.md)
  / [ADR-0017](../adr/0017-object-store-client-interface.md) (the object store the artifact
  channel uses), and [ADR-0042](../adr/0042-live-stack-e2e-mcp-http.md) (the operator-run
  live-stack test M2's e2e mirrors against a remote host).
- **Parent:** [`top-level-design.md`](top-level-design.md) §Roadmap (M2).

## What M2 adds

- **An independent `remote_libvirt` provider package** — `src/kdive/providers/remote_libvirt/`
  with its own discovery, lifecycle (provisioning / install / connect / control), build,
  retrieve, and debug (gdb-MI + introspect) modules, composing the same typed `ProviderRuntime`
  ports (ADR-0063). It does **not** share a `libvirt_common` layer with `local_libvirt`:
  local-libvirt is headed for removal, so coupling the production provider to a doomed module
  would create exactly the migration-shim the "replace, don't deprecate" standard forbids
  (ADR-0076). A new `ResourceKind.REMOTE_LIBVIRT = "remote-libvirt"` and migration `0020`
  (CHECK widen) register the third kind behind the per-kind `ProviderResolver` (ADR-0071).
- **A `qemu+tls://` control transport** — discovery, provisioning (define/start), control
  (power/reset/force-crash), and capability enumeration call libvirt over `qemu+tls://`. The
  x509 client cert (and key) is a **secret-by-reference** resolved at the worker boundary
  through the runtime's `SecretBackend`, which registers the resolved value before returning it
  so any transcript capturing it is exact-value masked (ADR-0077). This is the first provider
  resolving a real secret — the half of the secret contract local-libvirt (which resolves none)
  never exercised, and which M1.5's fault-inject only exercised against a synthetic sentinel.
- **The object store as the canonical artifact channel + a presigned-URL in-target seam** —
  the worker publishes the built kernel to the object store and mints a time-boxed **presigned
  GET** URL; the **target** (not the host, not the worker) pulls and installs it. The vmcore
  flows the other way: kdump in the guest uploads to a **presigned PUT** URL, and the worker
  references the resulting object. No standing object-store credentials live in any guest, no
  host-side agent is deployed, and `virStorageVolUpload` is not used for kernel artifacts
  (ADR-0078). The in-target execution that triggers the pull/install/reboot is realized for M2
  by **qemu-guest-agent over the same TLS connection** — no separate channel, no second secret.
  **Direct-kernel boot is retired as a carry-forward**: `remote_libvirt` boots a disk-image base
  OS and iterates kernels by in-guest install + reboot/kexec, debugged via the QEMU gdbstub.
  `local_libvirt` keeps direct-kernel boot. This is the model M3 (cloud-init), M4 (SSH/SoL
  after PXE/NIM), and M5 (in-LPAR) re-realize behind the **same** Installer/Retriever port
  contract — the one mechanism that survives to M5 rather than being replaced one milestone
  later.
- **Remote live-debug reachability** — three paths, scoped by what crosses the network
  (ADR-0079): the **gdb-MI tier** (breakpoints, single-step, live `read_memory` / `read_registers`,
  continue/interrupt) connects **directly over TCP** from the worker to the host's QEMU gdbstub
  port (`qemu+tls://` does not tunnel it), under an explicit, documented operator network
  assumption (routable worker→host TCP on a configured gdbstub port range); **drgn live
  introspection** runs **inside the guest** via the qemu-guest-agent seam (constrained
  allowlist, results streamed back); **vmcore postmortem** (drgn-from-vmcore, crash) runs **on
  the worker** after fetching the vmcore object from S3 — no live reachability needed. Bare
  metal later swaps the gdbstub for KGDB-over-SoL behind the same Connect port.
- **Build stays on the worker** — `RemoteLibvirtBuild` runs `make` on the worker exactly as
  `local_libvirt` does, then publishes vmlinuz+modules to the object store via the artifact
  channel. "Remote build host / GitHub Actions" (`top-level-design.md` line 232) is a later
  optimization, **not** M2.

## Non-goals (scoped out)

- **No new agent-facing MCP tools.** Like M1.5, a remote-libvirt resource is discovered and
  registered service-side and driven through the **existing** surface (`resources.*` /
  `allocations.*` / `systems.*` / `runs.*` / `debug.*` / `control.*` / `artifacts.*` /
  `vmcore.*`). The transport, host URI, TLS cert ref, and gdbstub port range are `capabilities`
  config on the resource, not a tool. Keeping the tool surface untouched is **half the
  falsifiability gate**.
- **No new `ErrorCategory`.** Every remote failure maps to an existing value
  (`transport_failure`, `infrastructure_failure`, `provisioning_failure`, `install_failure`,
  `boot_timeout`, `control_failure`, `stale_handle`, `lease_expired`). Pick the most specific
  existing value; invent no strings.
- **No `libvirt_common` extraction / no shared layer with local-libvirt** (ADR-0076).
- **No removal of `local_libvirt`** — deferred to a follow-up milestone; M2 keeps it as the
  default and falsifiability baseline.
- **No remote build host / GitHub Actions build** — build stays on the worker.
- **No host-side `kdive` daemon** — the in-target seam is guest-agent + presigned URLs; a host
  agent would be a libvirt-only investment thrown away at M3 (no controllable host in cloud, a
  BMC on bare metal), which is why it is rejected (ADR-0078).
- **No cross-host scheduling beyond M1.4** and **no multi-arch** — remote stays x86_64
  libvirt/QEMU; ppc64le is M5.

## Postgres schema (M2 delta)

- **One migration, `0020_resources_kind_remote_libvirt.sql`** — widen `resources_kind_check`
  from `IN ('local-libvirt', 'fault-inject')` to add `'remote-libvirt'`, mirroring how `0018`
  widened it for fault-inject. Forward-only (ADR-0015). **No other DDL.**
- **No new columns or tables.** A remote-libvirt resource's `connect_uri` (`qemu+tls://…`),
  TLS-cert `secret_ref`, gdbstub port range, object-store config, and per-host
  `concurrent_allocation_cap` are **keys in the existing `resources.capabilities` jsonb**, set
  by discovery exactly like local-libvirt's `vcpus` / `memory_mb` keys.

## Provider model (M2 delta)

`providers/composition.py` stays the **only** production provider-assembly point and gains a
third map entry behind the `ProviderResolver` (ADR-0071):

```
ResourceKind ──▶ ProviderRuntime
  local-libvirt ─▶ build_local_runtime()         # default; unchanged; removal deferred
  fault-inject  ─▶ build_faultinject_runtime()   # opt-in (M1.5)
  remote-libvirt▶ build_remote_runtime(...)       # opt-in: operator supplies host URI + cert ref
```

The `remote-libvirt` entry **and its discovery registrar** are composed only under operator
config (the host's `qemu+tls://` URI and the TLS-cert `secret_ref`); a deployment with no
remote host configured registers no remote runtime and has no bookable remote resource.
Resolution is the **post-System** path ADR-0071 fixed (`job → system → allocation →
resource.kind`); the pre-grant allocation plane and discovery are untouched, so M2 threads **no
new resolver call sites** — it registers a runtime into a seam that already exists. The port
**interfaces are unchanged** — `remote_libvirt` satisfies the same `ProviderRuntime` dataclass —
which is what keeps the falsifiability claim honest.

## The in-target install/retrieve seam (the load-bearing mechanism)

The seam every later provider reuses. M2 fixes its **contract** (ADR-0078); the guest-agent
realization is M2's implementation of it:

1. **Publish** — the worker writes the built artifact to the object store under the
   run/system-scoped key layout (ADR-0013) and mints a **presigned GET** URL bounded to the
   op's lifetime.
2. **Deliver the URL, not the bytes** — the worker hands the presigned URL to the target
   through the in-target execution seam (M2: a qemu-guest-agent `exec` over the TLS connection;
   M3: cloud-init / agent; M4/M5: SSH/SoL after netboot). No object-store credential ever
   enters a guest.
3. **Install in-target** — the target pulls the kernel, installs it (boot entry + the
   method-conditional crashkernel cmdline, reusing the ADR-0051/ADR-0061 composition into the
   guest's grub), and reboots/kexecs into it.
4. **Capture out** — kdump in the guest writes the vmcore and uploads it to a **presigned PUT**
   URL; the worker references the resulting object and runs postmortem locally.

Two contract points are load-bearing and tested: **(a)** the presigned URL's lifetime is
bounded and the seam never plants a standing credential in a guest; **(b)** any transcript the
guest-agent `exec` captures (which can echo the presigned URL and, for the TLS path, the
resolved cert material) flows the **normal redaction path** — the resolved cert `secret_ref` is
registered before use (ADR-0077) and the scope is released **only after** redact-and-persist
(ADR-0075's release-after-persist rule), so no resolved secret reaches the object store or a
response snippet unmasked.

## MCP tool surface (M2 delta)

**None.** M2 adds no agent-facing tools. A remote-libvirt resource is discovered and registered
service-side; `resources.list` / `.describe` show it; the existing tool surface allocates,
provisions, builds, installs, boots, attaches, crashes, captures, and releases it. Holding
`mcp/tools/*` at zero net lines is half the falsifiability gate.

## Auth / RBAC delta

**None.** The remote-libvirt resource registers under the **service identity** at discovery,
the same path local-libvirt uses; no new role, claim, or gate. A caller allocates and drives it
through the **existing** per-project `operator`/`viewer` checks — it is a Resource like any
other. The destructive-op gate (control.power/force_crash/teardown) applies unchanged.

## Error taxonomy (M2 delta)

**None.** Remote failures map to existing categories: TLS-connect / gdbstub-unreachable /
guest-agent-unreachable → `transport_failure`; presigned-URL / object-store / host-infra
failures → `infrastructure_failure`; define/start failures → `provisioning_failure`; in-guest
install failures → `install_failure` / `boot_timeout`; power/reset failures → `control_failure`;
a reference invalidated by a reprovision/reboot → `stale_handle`. The rule is unchanged: most
specific existing value, no new strings.

## Decomposition into single-PR issues

Each issue is one PR, dependency-ordered. Issue 1 is the serial foundation; the planes fan out
once it lands; issue 8 is the proving gate.

| # | Issue | Depends on | Area |
|---|-------|-----------|------|
| 1 | **Package foundation + control transport + discovery**: `ResourceKind.REMOTE_LIBVIRT` + migration `0020` (CHECK widen) + `remote_libvirt/` skeleton + injected `qemu+tls://` connection factory (unit-tested, no real host) + **TLS client-cert secret-by-reference** resolution (register-before-return) + composition registration (opt-in by operator config) + discovery over TLS (capabilities → `capabilities` jsonb) | — | providers + security |
| 2 | **Provisioning**: remote disk-image base-OS provisioning profile (qemu-guest-agent + virtio-serial channel + gdbstub in the domain XML) + `RemoteLibvirtProvision` define/start over TLS + per-System overlay | 1 | provisioning |
| 3 | **Artifact channel + presigned-URL + in-target (guest-agent) exec seam**: publish-to-object-store, mint presigned GET/PUT, run a constrained in-guest command via guest-agent over TLS, with secret-registration→redaction on the captured transcript | 1 | providers + security |
| 4 | **Build**: `RemoteLibvirtBuild` — worker `make`, publish vmlinuz+modules to the object store via the issue-3 channel | 3 | build-install |
| 5 | **Install**: `RemoteLibvirtInstall` — in-guest presigned-GET pull + install + method-conditional crashkernel cmdline into the guest grub + reboot/kexec via the seam | 3, 4 | build-install |
| 6 | **Connect + Debug**: direct-TCP gdbstub gdb-MI attach + in-guest drgn-live (guest-agent) + worker-side vmcore drgn/crash postmortem | 2, 3 | debug |
| 7 | **Control + Retrieve**: `RemoteLibvirtControl` (power/reset/force-crash over TLS) + `RemoteLibvirtRetrieve` (in-guest kdump → presigned-PUT → worker reference) | 2, 3 | control-retrieve |
| 8 | **Operator-run live-stack e2e** against a real remote TLS host (mirrors M1.2) + **falsifiability diff gate** (assert zero net core / `mcp/tools/*` lines vs the pre-M2 baseline, allowlisted touch-points) | all | core-platform |

**Merge wave:** `1` → `{2, 3}` → `{4, 6, 7}` → `5` → `8`. Issue 4 needs only the publish path
(issue 3); issue 5 needs the built artifact (4) and the seam (3); issues 6/7 need a running
System (2) and the seam (3).

### Sequencing & shared seams (no separate plan)

M2 follows the M1.3/M1.4/M1.5 model: detailed design lives here and in the four ADRs; each
issue is planned and implemented end-to-end by `work-issue`. There is **no
`docs/plans/m2-implementation.md`**. The cross-issue concerns no single issue owns are pinned
here:

- **One migration, claimed up front.** Issue **1** owns
  `0020_resources_kind_remote_libvirt.sql` (the only DDL this milestone) — the CHECK widen
  lands with the runtime registration it admits, so the ADR-0071 CHECK↔registry parity test
  never sees a CHECK-allowed kind without a runtime. Single schema-touching issue → no
  renumber-on-rebase expected.
- **Land the foundation before its consumers.** Issue **1** is strictly first (the package,
  the transport, the kind, discovery). The artifact/guest-agent seam (issue **3**) and
  provisioning (issue **2**) parallel after it; build/install (4→5) and debug/control-retrieve
  (6/7) consume those.
- **Expected rebase zones** (recurring in M1.x parallel work): `providers/composition.py`
  (issue 1 adds the registration; issues 2–7 wire ports into the remote runtime),
  `tests/db/test_migrate.py` (issue 1's migration), `domain/models.py`'s `ResourceKind` (issue
  1, one enum value — an allowlisted falsifiability touch-point), and the generated
  `docs/guide/reference/*` only if a tool docstring shifts (regenerated by `docs-check` —
  rebase, don't hand-edit). M2 adds **no** tools, so tool-doc churn is largely absent.
- **Each issue runs in its own worktree** (parallel `work-issue` requires it, per the global
  standard) — never share the working copy, and place worktrees **outside** the repo tree.

## Carried invariants

1. **The provider seam is unchanged** (ADR-0063) — `remote_libvirt` satisfies the same
   `ProviderRuntime` ports; M2 adds a registration in the composition map (the expected
   change-surface), not a port-interface change, so the falsifiability claim holds and is now
   tested against a real second provider (ADR-0076).
2. **A resolved secret is registered before it can be emitted, and released only after
   redact-and-persist** (ADR-0027, ADR-0073, ADR-0075) — the TLS client cert resolves through
   the `SecretBackend` (register-before-return), the guest-agent transcript is redacted by
   exact value on persist, and the per-op scope releases **after** every artifact/snippet that
   could contain it is persisted, so no resolved secret reaches the object store or a snippet
   unmasked (ADR-0077).
3. **The object store is the only bulk artifact channel; the target pulls/pushes via bounded
   presigned URLs** (ADR-0078) — no standing object-store credential in any guest, no host-side
   agent, no `virStorageVolUpload` for kernel artifacts. The in-target install/retrieve seam's
   contract is the M2→M5 carry-forward; direct-kernel boot is local-libvirt-only.
4. **`remote_libvirt` is opt-in; the default deployment composes only `local-libvirt`**
   (ADR-0071, ADR-0076) — the remote entry and its discovery registrar are composed only when
   an operator configures a remote host URI + TLS cert ref, so a deployment without one has no
   bookable remote resource.
5. **Portability is measured, not asserted** (ADR-0076) — the issue-8 diff gate checks the M2
   range against the pre-M2 baseline tag and fails on any net change under core or `mcp/tools/*`
   outside the explicit allowlist (the `ResourceKind` value, `composition.py` registration, the
   migration, regenerated docs). A core change is a smell to refactor away, per the milestone's
   co-equal goals.
