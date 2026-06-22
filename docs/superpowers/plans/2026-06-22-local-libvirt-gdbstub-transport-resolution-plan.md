# Plan — Local-libvirt gdbstub transport resolution (M2.8 B1, #675)

Derived from [the spec](../../specs/2026-06-22-local-libvirt-gdbstub-transport-resolution.md).
Execution mode: **direct in-session TDD** (tasks are tightly coupled — a shared parser feeds
both the renderer and the resolver — so subagent fan-out would thrash the same files).

Each task: write the failing test first, confirm it fails for the expected reason, write the
minimal code, run the focused test + `just lint type test`, refactor green. Commit per task with
the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer. Conventional
commits, ≤72-char subject.

Guardrails (from `justfile`): `just lint` (ruff check), `just format` (ruff format --check via
pre-commit), `just type` (ty), `just test` (pytest). Full gate before push: `just ci`. Note the
KNOWN TRAP: local `ty` may diverge from CI because the venv has live debug deps (drgn/guestfs);
if `just type` errors come purely from live-dep import resolution and not from this branch's code,
note it in the PR and rely on CI's `type` job.

## Task ordering rationale

The shared parser (Task 1) must land before the renderer (Task 2) and resolver (Task 4) that both
use it. The renderer (Task 2) must land before provisioning wiring (Task 3) which calls it. The
install-preservation fix (Task 5) is independent of 1–4 but must land before any live drive. The
descriptor + maturity (Task 6) is the surface flip and lands once the seam exists. Tests-only
fixups to cross-agent files (composition test, A2 admission test) ride with Task 6.

---

## Task 1 — Promote `recorded_gdb_port` to the shared libvirt-xml helper

**Where it fits:** spec §6. Both providers need the `-gdb tcp:host:port` parse; extract the pure
helper so the local renderer/resolver and remote share one implementation.

**Files:** `src/kdive/providers/shared/libvirt_xml.py` (add `recorded_gdb_port(xml) -> int | None`),
`src/kdive/providers/remote_libvirt/lifecycle/xml.py` (re-export / import the shared one; keep the
`recorded_gdb_port_strict` wrapper), tests under `tests/providers/shared/` +
`tests/providers/remote_libvirt/test_xml.py` (unchanged expectations must still pass).

**Steps:**
1. Failing test in `tests/providers/shared/test_libvirt_xml.py`: `recorded_gdb_port` returns the
   port from a domain XML carrying `<qemu:commandline><qemu:arg value="-gdb"/><qemu:arg
   value="tcp:127.0.0.1:4444"/></qemu:commandline>`; `None` for absent/malformed/non-`-gdb` args.
2. Move the pure `_recorded_gdb_port(root)` + `recorded_gdb_port(xml)` body from
   `remote_libvirt/lifecycle/xml.py` into `shared/libvirt_xml.py`. Remote imports it; remote's
   `recorded_gdb_port_strict` keeps wrapping `_parse_domain_xml_strict` + the shared
   `_recorded_gdb_port`.
3. Run remote xml tests (must be unchanged-green) + the new shared test.

**Acceptance:** shared `recorded_gdb_port` parses the remote-shaped and loopback-shaped args; the
remote `recorded_gdb_port`/`recorded_gdb_port_strict` public behavior is byte-identical (its tests
pass untouched). No behavior change to remote.

**Rollback:** revert the move; remote keeps its private copy.

---

## Task 2 — Render the gdbstub `<qemu:commandline>` in the local domain XML

**Where it fits:** spec §1. Make the phantom `debug.gdbstub` flag real.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/xml.py`,
`tests/providers/local_libvirt/test_xml.py` (create if absent).

**Steps:**
1. Failing tests:
   - `render_domain_xml(..., gdb_port=4444)` with `debug.gdbstub=True` → XML contains a
     `qemu:`-prefixed `<commandline>` with args `-gdb` and `tcp:127.0.0.1:4444`, and the parsed
     port via the shared `recorded_gdb_port` is `4444`.
   - `debug.gdbstub=False` → no `<qemu:commandline>` element (and `recorded_gdb_port` is `None`).
   - `debug.gdbstub=True` with `gdb_port=None` → raises `CONFIGURATION_ERROR`.
   - `gdb_port` ignored when flag is `False` (passing a port renders nothing).
2. Add keyword-only `gdb_port: int | None = None` to `render_domain_xml`; call
   `register_qemu_namespace()` in `_ensure_kdive_namespace_registered` (rename to
   `_ensure_namespaces_registered`); when `profile.provider.local_libvirt.debug.gdbstub`, append
   the `<qemu:commandline>` element on `127.0.0.1` with `gdb_port`; raise CONFIGURATION_ERROR on
   `gdbstub and gdb_port is None`.
3. Run focused xml tests + `just lint type`.

**Acceptance:** the four cases above pass; existing `render_domain_xml` callers/tests
(non-gdbstub) are unchanged (default `gdb_port=None`, flag default `False`).

**Rollback:** drop the parameter + conditional; the renderer reverts to its current output.

---

## Task 3 — Allocate the loopback port in provisioning (bind-probe + reuse)

**Where it fits:** spec §2. Provision records a stable per-System loopback gdbstub port.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`,
`tests/providers/local_libvirt/test_provisioning.py`.

**Steps:**
1. Failing tests (fakes; no real libvirt/socket):
   - With `debug.gdbstub=True` and a fake free-port source returning `5555` and a fake connect
     whose `lookupByName` raises `VIR_ERR_NO_DOMAIN`: `provision` renders XML recording port
     `5555` (assert via the defined XML captured by the fake `defineXML`).
   - Reuse: a fake existing domain whose `XMLDesc()` records `6666` → `provision` reuses `6666`
     (free-port source NOT called).
   - `debug.gdbstub=False` → no extra connection for port lookup; rendered XML has no gdbstub
     (free-port source NOT called).
   - A non-`NO_DOMAIN` libvirt error during the port lookup → `INFRASTRUCTURE_FAILURE`.
2. Add a `free_port: Callable[[], int] | None` ctor param (default = real bind-probe
   `# pragma: no cover - live_vm`). Add `_gdb_port_for(system_id, *, conn)`:
   `lookupByName(domain_name_for(system_id))` → `recorded_gdb_port(XMLDesc())`; on NO_DOMAIN or
   `None` → `free_port()`; other libvirt error → infra failure. Call it in `provision` (and
   `reprovision` inherits via `provision`) only when the flag is set; thread the port into
   `render_domain_xml(gdb_port=...)`.
3. Decide the connection: reuse the existing `self._connect()` seam; the port lookup may open its
   own short-lived connection (closed in a `finally`) — acceptable per spec. Run focused tests +
   `just lint type`.

**Acceptance:** the four cases pass; non-gdbstub provision path opens no extra connection and is
otherwise unchanged; idempotent retry reuses the recorded port.

**Rollback:** drop `_gdb_port_for` and the ctor param; provision reverts to flag-less render.

---

## Task 4 — Implement `_real_resolve_endpoint` (read the port from the live domain)

**Where it fits:** spec §3. Production resolver feeds the existing loopback-enforcing
`_open_gdbstub`.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/connect.py`,
`tests/providers/local_libvirt/test_connect.py`.

**Steps:**
1. Failing tests (fake libvirt connection injected — refactor `_real_resolve_endpoint` to take an
   injected connect/lookup seam, mirroring provisioning, so the pure branches are testable):
   - Domain XML records `4444` → resolver returns `("127.0.0.1", 4444)`.
   - `lookupByName` raises `VIR_ERR_NO_DOMAIN` → `CONFIGURATION_ERROR` ("no running libvirt
     domain").
   - Domain XML records no gdbstub port → `CONFIGURATION_ERROR` ("not provisioned with a
     gdbstub…").
   - Malformed XML → `INFRASTRUCTURE_FAILURE`.
   - Other libvirt error → `INFRASTRUCTURE_FAILURE`.
   - **Update** `test_from_env_resolver_raises_missing_dependency`: `from_env()` now resolves a
     real endpoint (no longer raises MISSING_DEPENDENCY). Replace it with a test that `from_env`'s
     resolver, given a fake connection, returns the recorded endpoint — or drives the
     no-domain/no-port error. The MISSING_DEPENDENCY assertion is deleted (the defect is fixed).
2. Make `LocalLibvirtConnect.from_env` wire a real `_real_resolve_endpoint` that opens
   `libvirt.open(KDIVE_LIBVIRT_URI)`, looks up `str(system)`, reads `XMLDesc()`, parses via shared
   `recorded_gdb_port`, returns `("127.0.0.1", port)`; the libvirt-open + XMLDesc are the only
   `# pragma: no cover - live_vm` lines, the branch logic is pure and injected for tests.
3. Run focused connect tests + `just lint type`.

**Acceptance:** all six cases pass; `_open_gdbstub`'s loopback check and RSP probe path are
unchanged (the resolver just supplies `("127.0.0.1", port)`); no MISSING_DEPENDENCY for the gdbstub
resolution.

**Rollback:** restore the unconditional-raise stub; `from_env` reverts.

---

## Task 5 — `_real_resolve_ssh_endpoint` honest-unsupported + install namespace preservation

**Where it fits:** spec §4 + §2a. Two small, independent correctness edits.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/connect.py` (ssh stub),
`src/kdive/providers/local_libvirt/lifecycle/install.py` (namespace),
`tests/providers/local_libvirt/test_connect.py`, `tests/providers/local_libvirt/test_install.py`.

**Steps:**
1. Failing test: `_real_resolve_ssh_endpoint` raises `CONFIGURATION_ERROR` (not
   `MISSING_DEPENDENCY`) with a message naming drgn-live as unsupported on local + the follow-up
   issue ref. **Update** `test_from_env_ssh_resolver_raises_missing_dependency` to expect
   CONFIGURATION_ERROR.
2. Rewrite the ssh stub accordingly.
3. Failing test: a provision XML carrying `<qemu:commandline>` (built via `render_domain_xml(...,
   gdb_port=4444)`), passed through `install._render_os_section` (with a fake conn returning that
   XML from `XMLDesc`), re-serializes to XML that still contains a `qemu:`-prefixed
   `<commandline>` whose args include `tcp:127.0.0.1:4444`. (Confirm it FAILS first — drops to
   `ns0:` — then fix.)
4. Add `register_qemu_namespace()` + `register_kdive_namespace()` at the top of
   `_render_os_section` before `ET.tostring`.
5. Run focused tests + `just lint type`.

**Acceptance:** ssh resolver is honestly `CONFIGURATION_ERROR`; the gdbstub element survives the
install os-edit round-trip with its `qemu:` prefix.

**Rollback:** revert both edits independently.

---

## Task 6 — Flip the descriptor + update maturity pointer; fix cross-agent tests

**Where it fits:** spec §5. The surface flip, last — once the seam exists.

**Files:** `src/kdive/providers/local_libvirt/composition.py` (descriptor — cross-agent conflict
zone, single additive line), `src/kdive/mcp/tools/debug/sessions.py` (maturity `providers`
pointer wording, stays `partial`), `tests/providers/local_libvirt/test_composition.py` (expect
`frozenset({"gdbstub"})`), any A2 admission test asserting local admits gdbstub / rejects
drgn-live.

**Steps:**
1. Update `test_composition.py` line ~73 to expect `supported_debug_transports ==
   frozenset({"gdbstub"})` (failing), then set `supported_debug_transports=frozenset({"gdbstub"})`
   in `build_runtime`.
2. Find the admission test for `debug.start_session` capability gating; assert local now admits
   `gdbstub` and still rejects `drgn-live` with `capability_unsupported`. If no such test exists at
   the right boundary, add one driving `_prepare_attach_request`/the start handler with a fake
   runtime whose `supported_debug_transports={"gdbstub"}`.
3. Edit the two `debug.start_session` / `debug.end_session` maturity `providers=` strings from
   "local-libvirt: planned (M2.8 B1)" to "local-libvirt: wired, pending live KVM proof (M2.8 B6
   #680)". Maturity value stays `"partial"`. Leave session-bound ops maturity untouched.
4. Run `just lint type test` (focused on providers + mcp/debug), then the full `just ci`.

**Acceptance:** `supported_debug_transports == frozenset({"gdbstub"})`; admission admits gdbstub /
rejects drgn-live on local; `debug.*` maturity is `partial` with the updated pointer; full `just
ci` green (minus any live-dep `ty` divergence noted for CI).

**Rollback:** revert the descriptor line + test expectation + pointer wording.

---

## Cross-cutting verification before push

- Full `just ci` green (lint, type, test, doc gates, adr-status-check, docs-links/paths).
- `live_vm`-marked tests SKIP in this environment (expected; do not un-gate).
- The follow-up issue (session-networking + drgn-live SSH) is filed; its number is in the spec §4
  placeholder, the ssh-stub message, the PR body, and the final report.
- Grep for any other consumer of the local `render_domain_xml` signature or the
  `MISSING_DEPENDENCY` assertion that the seam change invalidates (the spec's "when a contract
  changes, grep every caller" discipline).
