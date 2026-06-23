# Local-libvirt drgn-live SSH transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire local-libvirt's `drgn-live` debug transport realized over drgn-over-SSH into an in-guest sshd reached on a loopback-forwarded guest SSH port (#697), feeding the existing loopback-enforcing `_open_ssh` probe.

**Architecture:** Mirror B1's gdbstub-port mechanism end-to-end: `render_domain_xml` renders a loopback QEMU user-net `hostfwd` (gated on the profile's `ssh_credential_ref`), provision bind-probes + records the forwarded port (idempotent reuse), and `_real_resolve_ssh_endpoint` reads it back. The credential plumbing and `_open_ssh` orchestration already exist; the descriptor flips to advertise `drgn-live` while `debug.*` maturity stays `partial` (B6 owns the live proof).

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`, `xml.etree.ElementTree`, `defusedxml`, libvirt-python (fake-injected in tests).

## Global Constraints

- ADR: **0218** (assigned). Spec: `docs/specs/2026-06-23-local-libvirt-session-ssh-transport.md`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict (whole-tree). Absolute imports only.
- Guardrails before EVERY commit: `just lint` + `just type` + the touched-area tests; full `just ci` before first push.
- `live_vm`-gated seams stay gated (`# pragma: no cover - live_vm`). Never un-gate.
- Pick the most specific `ErrorCategory`; never invent strings.
- `127.0.0.1` literal hard-coded (loopback is the security boundary). No secret in XML/logs/responses.
- Conventional commits, imperative ≤72-char subject, trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Shared `recorded_ssh_port` reader

**Files:**
- Modify: `src/kdive/providers/shared/libvirt_xml.py`
- Test: `tests/providers/shared/test_libvirt_xml.py` (create if absent; else append)

**Interfaces:**
- Produces: `recorded_ssh_port_from_root(root: ET.Element) -> int | None`, `recorded_ssh_port(domain_xml: str) -> int | None`. Parses the value of the `<qemu:arg>` following a `-gdb`-style scan, matching `hostfwd=tcp:127.0.0.1:(\d+)-:22` (regex anchored on `127.0.0.1` host + guest port `22`). First match wins; non-integer/absent → `None`; malformed XML wrapper → `None`.

- [ ] **Step 1: Write failing tests.** Cover: reads the forwarded port from a rendered `-netdev ...hostfwd=tcp:127.0.0.1:<port>-:22` arg; `None` when no `-netdev`; `None` when a `-netdev` value has a different host/guest-port; `None` for non-integer; `None` for malformed XML via `recorded_ssh_port("<domain")`; coexists with a `-gdb` arg in the same commandline (both readers read their own).
- [ ] **Step 2: Run — expect FAIL (function not defined).** `uv run pytest tests/providers/shared/test_libvirt_xml.py -q`
- [ ] **Step 3: Implement** `recorded_ssh_port_from_root` + `recorded_ssh_port` mirroring `recorded_gdb_port_from_root`/`recorded_gdb_port`, using `re.search(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-:22", value)` on each `-netdev`-following arg value.
- [ ] **Step 4: Run — expect PASS.** Plus `just lint && just type`.
- [ ] **Step 5: Commit** `feat(local-libvirt): add shared recorded_ssh_port domain-XML reader`.

---

### Task 2: Render the SSH forward in `render_domain_xml`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/xml.py`
- Test: `tests/providers/local_libvirt/test_xml.py` (or wherever `render_domain_xml` is tested — locate first)

**Interfaces:**
- Consumes: `recorded_ssh_port` (Task 1) in tests to assert the rendered port.
- Produces: `render_domain_xml(system_id, profile, *, disk_path, gdb_port=None, ssh_port=None)`. Renders `-netdev user,id=kdivessh,hostfwd=tcp:127.0.0.1:<ssh_port>-:22` + `-device virtio-net-pci,netdev=kdivessh` into the single `<qemu:commandline>` element iff `section.ssh_credential_ref is not None`. `ssh_port=None` + ref-set → `CONFIGURATION_ERROR`. Helper `_qemu_commandline(domain) -> ET.Element` returns/creates the lone commandline element so gdbstub + ssh args share it.

- [ ] **Step 1: Write failing tests.** (a) ref set + `ssh_port` → `recorded_ssh_port(xml) == ssh_port` and the host is `127.0.0.1`; (b) ref `None` → no `-netdev` in xml (`recorded_ssh_port(xml) is None`); (c) ref set + `ssh_port=None` → `CategorizedError` CONFIGURATION_ERROR; (d) gdbstub + ref both set → `recorded_gdb_port(xml)` and `recorded_ssh_port(xml)` both return their ports AND there is exactly **one** `<qemu:commandline>` element (assert via parsed root `findall`).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement.** Add `ssh_port: int | None = None` kwarg; add `_append_ssh_forward(domain, ssh_port)` (raises CONFIGURATION_ERROR on `None`); refactor `_append_gdbstub` + the new fn to call `_qemu_commandline(domain)` (find existing `{QEMU_NS}commandline` or create). Gate on `section.ssh_credential_ref is not None`.
- [ ] **Step 4: Run — expect PASS.** Plus `just lint && just type`.
- [ ] **Step 5: Commit** `feat(local-libvirt): render loopback SSH hostfwd in domain XML`.

---

### Task 3: Allocate + record the SSH port in provisioning

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`
- Test: `tests/providers/local_libvirt/test_provisioning.py` (locate the gdbstub-port allocation tests; mirror them)

**Interfaces:**
- Consumes: `render_domain_xml(..., ssh_port=...)` (Task 2), `recorded_ssh_port` (Task 1), the existing injected `self._free_port` seam.
- Produces: `_ssh_port_for(system_id) -> int` (reuse-or-bind-probe) and `_recorded_ssh_port(system_id) -> int | None`, mirroring `_gdb_port_for`/`_recorded_gdb_port`. `provision()` passes `ssh_port=self._ssh_port_for(...)` iff `section.ssh_credential_ref is not None`.

- [ ] **Step 1: Write failing tests.** Mirror the gdbstub tests with a fake connection: (a) no prior domain (`VIR_ERR_NO_DOMAIN`) → bind-probes via injected `free_port`; (b) prior domain records an SSH port → reuses it (no `free_port` call); (c) other libvirt error → `INFRASTRUCTURE_FAILURE`; (d) provision with ref set passes a non-None `ssh_port` into `render_domain_xml` (assert via a render spy or the rendered xml carrying the port); (e) provision without ref renders no SSH forward and opens no extra connection for it.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** `_ssh_port_for` + `_recorded_ssh_port` (copy the `_gdb_port_for`/`_recorded_gdb_port` shape, swapping `recorded_gdb_port`→`recorded_ssh_port` and the error-message verbs), and wire `provision()` to compute `ssh_port` gated on `section.ssh_credential_ref is not None`.
- [ ] **Step 4: Run — expect PASS.** Plus `just lint && just type`.
- [ ] **Step 5: Commit** `feat(local-libvirt): allocate + record loopback SSH port on provision`.

---

### Task 4: Install preserves the SSH forward (regression test only)

**Files:**
- Test: `tests/providers/local_libvirt/test_install.py` (locate the gdbstub-preservation test; mirror it)

**Interfaces:**
- Consumes: `render_domain_xml(..., ssh_port=...)`, `install._render_os_section` (or the public install path the gdbstub test drives), `recorded_ssh_port`.

No production change — `install._render_os_section` already calls `register_qemu_namespace()` before `tostring` (the B1 fix). This task pins a regression guard for the new element.

- [ ] **Step 1: Write the test.** Render a provision XML with `ssh_credential_ref` set + `ssh_port`, run it through the install os-edit (mirror the existing gdbstub-preservation test exactly), assert the re-serialized XML still contains a `qemu:`-prefixed `-netdev ...hostfwd=tcp:` arg AND `recorded_ssh_port(result) == ssh_port`.
- [ ] **Step 2: Run — expect PASS immediately** (the production fix already exists). If it FAILS, the namespace fix regressed — stop and report.
- [ ] **Step 3: Commit** `test(local-libvirt): pin SSH forward survives install os-edit`.

---

### Task 5: Implement `_real_resolve_ssh_endpoint`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/connect.py`
- Test: `tests/providers/local_libvirt/test_connect.py` (extend the SSH section)

**Interfaces:**
- Consumes: `recorded_ssh_port_from_root` (Task 1), the existing `_Connect`/`_Conn`/`_Domain` protocols + `_default_connect`/`_close`.
- Produces: `_resolve_ssh_endpoint_via(connect: _Connect) -> _ResolveEndpoint` and a real `_real_resolve_ssh_endpoint = _resolve_ssh_endpoint_via(_default_connect)`. Returns `("127.0.0.1", port)`; not-found → CONFIGURATION_ERROR, no recorded port → CONFIGURATION_ERROR, malformed XML / other libvirt error → INFRASTRUCTURE_FAILURE. `from_env()` wires `resolve_ssh_endpoint=_real_resolve_ssh_endpoint` (already does).

- [ ] **Step 1: Write failing tests.** Reuse the `_FakeGdbConn`/`_FakeGdbDomain` shape with an `-netdev hostfwd` XML helper. Cover: reads the recorded port → `("127.0.0.1", port)` + connection closed; absent domain (`VIR_ERR_NO_DOMAIN`) → CONFIGURATION_ERROR; no recorded port → CONFIGURATION_ERROR; malformed XML → INFRASTRUCTURE_FAILURE; other libvirt error → INFRASTRUCTURE_FAILURE. Update `test_from_env_ssh_resolver_is_unsupported_configuration_error` (the stub no longer raises unconditionally) — replace with a test that `from_env().open_transport(_SYSTEM, "drgn-live")` now hits the resolver (which fails on no real libvirt, but with the *resolver's* CONFIGURATION_ERROR / INFRASTRUCTURE_FAILURE, not the old `#697` stub message).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** `_resolve_ssh_endpoint_via` (copy `_resolve_endpoint_via`, swap `_resolved_port`→a `_resolved_ssh_port` using `recorded_ssh_port_from_root`, and the not-found/no-port message verbs to "drgn-live SSH transport" / "reprovision with ssh_credential_ref set"). Replace `_real_resolve_ssh_endpoint`'s stub body with `_resolve_ssh_endpoint_via(_default_connect)` (module-level, like `resolve_endpoint`). Keep `_real_ssh_connect` `live_vm`-gated.
- [ ] **Step 4: Run — expect PASS.** Plus `just lint && just type`.
- [ ] **Step 5: Commit** `feat(local-libvirt): resolve recorded SSH endpoint for drgn-live`.

---

### Task 6: Flip the descriptor (live-gated)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/composition.py:121` (`supported_debug_transports`)
- Test: `tests/providers/local_libvirt/test_composition.py` (or wherever `build_runtime`'s descriptor is asserted — locate)

**Interfaces:**
- Produces: `build_runtime(...).supported_debug_transports == frozenset({"gdbstub", "drgn-live"})`; `supported_introspection` unchanged.

- [ ] **Step 1: Write/adjust failing test** asserting `supported_debug_transports == frozenset({"gdbstub", "drgn-live"})` and `supported_introspection == frozenset({"offline-vmcore"})` (unchanged).
- [ ] **Step 2: Run — expect FAIL** (descriptor still gdbstub-only).
- [ ] **Step 3: Implement** — add `"drgn-live"` to the frozenset; update the explanatory comment to say drgn-live is wired (loopback SSH forward, #697/ADR-0218) with maturity held at partial for B6.
- [ ] **Step 4: Run — expect PASS.** Plus `just lint && just type`.
- [ ] **Step 5: Commit** `feat(local-libvirt): advertise drgn-live debug transport`.

---

### Task 7: Admission integration test (capability now admits drgn-live)

**Files:**
- Test: locate the existing admission/capability test that asserts `drgn-live` is rejected on local (grep `capability_unsupported` + `drgn-live` under `tests/`); update it.

**Interfaces:**
- Consumes: the flipped descriptor (Task 6).

- [ ] **Step 1: Find** any test asserting local rejects `debug.start_session(..., "drgn-live")` with `capability_unsupported`. If one exists, update it to assert admission now passes the capability gate (the request proceeds past `_capability_unsupported`; it may still fail later on no live libvirt — assert the failure is no longer `capability_unsupported`). If none exists, add a `_prepare_attach_request`-level test that `drgn-live in supported_debug_transports` so admission does not short-circuit.
- [ ] **Step 2: Run — expect FAIL or PASS** depending on whether the stale assertion exists; make it green.
- [ ] **Step 3: Run** `just ci` (full suite — catches cross-cutting drift: docs, architecture, snapshot tests).
- [ ] **Step 4: Commit** `test(local-libvirt): admit drgn-live debug session after descriptor flip`.

---

## Self-Review

- **Spec coverage:** §1 profile signal → Task 2/3 gate; §2 render → Task 2; §3 allocation → Task 3; §4 install preservation → Task 4; §5 resolve → Task 5; §6 reader → Task 1; §7 descriptor/maturity → Task 6 (maturity untouched = stays partial, no task needed); §8 user/identity contract → documented, live_vm-only, no CI task. Acceptance bullets each map to a Task 1–7 test. ✓
- **Placeholder scan:** none. ✓
- **Type consistency:** `recorded_ssh_port`/`recorded_ssh_port_from_root`, `_ssh_port_for`/`_recorded_ssh_port`, `_resolve_ssh_endpoint_via`, `ssh_port` kwarg used consistently across tasks. ✓
