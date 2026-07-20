# ADR 0392 — Harness boot path for the gdbstub-preserve debug tests

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-20
- **Deciders:** Maintainer (randomparity), Claude Code

## Context

Epic #1289 sub-issue E (#1294) migrated the provisioned-System live tests onto
`require_live_vm_provisioned` and deduped the panic-wait loops onto
`kdive.testing.live_vm.wait_for_panic`, but deliberately left three gdbstub /
preserve-on-crash debug tests only partly migrated:

- `tests/providers/local_libvirt/test_live_preserve_attach.py` (#747 / ADR-0233)
- `tests/mcp/debug/test_debug_live_attach.py`
- `tests/mcp/debug/test_debug_gdbmi_live_smoke.py`

Each renders kdive's **production** `render_domain_xml(..., gdb_port=,
debug={gdbstub, preserve_on_crash})` — pvpanic + `<on_crash>preserve</on_crash>` +
`-gdb` passthrough — which **is their subject under test** (#747, #1255), then
calls `conn.createXML(final_xml, 0)` directly rather than going through
`boot_throwaway_domain`. `boot_throwaway_domain` renders a *generic* throwaway
domain (`throwaway_domain_xml`) with no gdbstub, so a literal migration would
delete exactly what these tests prove.

Two problems follow from the raw-`createXML` shape:

1. **#1323 (bug).** These three tests bypass `prepare_session_runtime`, so under
   `qemu:///session` the per-domain QMP monitor socket is derived under the deep
   pytest `XDG_CONFIG_HOME` and overflows the 108-byte UNIX-socket limit —
   `conn.createXML` dies before any assertion. `boot_throwaway_domain`-based tests
   do not hit this because the harness redirects `XDG_CONFIG_HOME` to a short
   `/tmp` path (`live_vm.py:295`).
2. **Duplicated boot/teardown boilerplate.** Each test re-implements connect +
   `createXML` + panic-wait + `finally: destroy/close`, none of which is the SUT.

#1321 asks: extend the harness (Option a) or confirm the tests stay on production
XML (Option b). The pre-decided answer is **Option (a)** — but the risk the issue
flags is real: naively adding gdbstub/pvpanic/preserve rendering to
`throwaway_domain_xml` would re-implement `render_domain_xml`'s debug logic in a
second XML builder.

## Decision

Extend the harness with a **new transient-domain boot context manager**,
`boot_preserved_gdbstub_domain`, that owns only the *environment boilerplate* and
**delegates all XML rendering to the caller**:

- The test still renders kdive's production `render_domain_xml(...)` (its SUT) and
  applies the test-side direct-kernel `<os>` + serial-log post-processing, then
  hands the finished XML string to the harness. There is **no** second XML builder
  — the harness never imports `render_domain_xml` (the `live_vm` mechanism keeps
  the provider-boundary it documents: `src/` test-support code does not reach into
  provider internals, and reads no `KDIVE_*` env — ADR-0087).
- `boot_preserved_gdbstub_domain(xml, *, uri, console_log, ...)` runs
  `prepare_session_runtime(uri)` (the #1323 fix), opens the connection, boots the
  domain **transiently** with `conn.createXML(xml, 0)`, waits for the console
  panic marker via `wait_for_panic` (raising `LiveVmBootTimeout` on timeout),
  yields a `LiveDomain`, and guarantees teardown (`destroy` + `conn.close` +
  `runtime.restore`) in a `finally`.

Transient (`createXML`) rather than define/`create` mirrors the existing tests'
shape: these domains carry a caller-baked empty scratch disk (to force the early
VFS panic), not a harness-created overlay, and never need `undefineFlags` because a
transient domain vanishes on `destroy`. This is why it is a *separate* context
manager from `boot_throwaway_domain` (which defines a persistent domain, stages a
qcow2 overlay beside the rootfs, and undefines on teardown) rather than a flag on
it — the two share `prepare_session_runtime`, `wait_for_panic`, and `LiveDomain`,
but their boot/teardown lifecycles differ.

**Env contract (the issue's "also decide").** These tests key off
`KDIVE_LIVE_VM_BZIMAGE` + an empty scratch disk, not `KDIVE_LIVE_VM_ROOTFS`, so
they do not fit `require_live_vm_throwaway`'s rootfs contract. Grow the throwaway
family a **bzimage-panic variant**: add `require_live_vm_bzimage` to
`tests/live_vm` with the same skip-vs-fail discipline as the other gates (env
unset → skip; set-but-not-a-file → fail loud, so a mis-provisioned runner cannot
masquerade as "no environment"). The three tests resolve their bzimage + libvirt
URI through it instead of an inline `pytest.skip`. The gdb-MI test's *extra*
inputs (`KDIVE_LIVE_VM_GDBMI_VMLINUX`, optional module fixture) stay test-local —
they are that one test's concern, not the family contract.

## Consequences

- The three debug tests keep proving the production debug XML (unchanged SUT) but
  boot through the harness, so #1323 is fixed as a side effect and the native
  `live-vm` gate is unblocked from that cause.
- One boot/teardown/session-runtime code path instead of three hand-rolled ones;
  the panic-wait was already shared (#1294).
- The bzimage tests now fail loud on a mis-provisioned runner, matching the rest
  of the live_vm family.
- New shipped surface in `kdive.testing.live_vm`
  (`boot_preserved_gdbstub_domain`), covered by fake-connection unit tests
  alongside the existing `boot_throwaway_domain` tests.

## Alternatives considered

- **Option (b): confirm the tests stay on raw production XML.** Rejected: leaves
  #1323 needing its own separate fix and keeps three copies of the boot/teardown
  boilerplate.
- **A `gdb_port`/`debug` flag on `throwaway_domain_xml`.** Rejected: re-implements
  `render_domain_xml`'s debug rendering in a second builder — the exact risk #1321
  names.
- **Import `render_domain_xml` into the harness.** Rejected: breaks the
  `live_vm` mechanism's documented provider-boundary; the caller already holds the
  renderer, so the harness only needs the finished XML.
