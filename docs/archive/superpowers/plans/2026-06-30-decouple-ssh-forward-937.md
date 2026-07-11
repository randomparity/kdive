# Plan: always render the local-libvirt SSH forward (#937)

- Issue: #937
- Spec: [decouple-ssh-forward-937](../../specs/2026-06-30-decouple-ssh-forward-937.md)
- ADR: [ADR-0281](../../adr/0281-always-render-ssh-forward.md)
- Execution: direct TDD in one session (the change is tightly coupled — renderer, provisioner, and
  their tests move together — so no subagent fan-out).

Guardrails (run before every commit; CI gates these recipes individually):
`just lint` · `just type` · `just test`. Single test:
`uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -q`.

## Task 1 — Renderer: always append the SSH forward; `ssh_port` becomes required

**Where it fits:** the root of the decouple — `render_domain_xml` is the seam that decides whether
the forward is present.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/xml.py`;
`tests/providers/local_libvirt/test_provisioning.py`.

**Pre-implementation verification (done; results pinned here so the task is self-contained):**

- The **only** production caller of this renderer is the provisioner (`provisioning.py:213`);
  `remote_libvirt` has its own separate `render_domain_xml`. So making `ssh_port` required cannot
  break a `src/` caller — only the test call sites below.
- All direct `render_domain_xml(...)` test call sites that pass **no** `ssh_port` and so must gain one
  (else they raise `CONFIGURATION_ERROR` once the forward is unconditional): the `_render()` helper
  (line 85), and lines 109, 110, 242, 251, 289, 310, 329. Two of these carry assertions that also flip
  (next bullet); the rest just need an `ssh_port` argument added (via the helper or inline).
- Two gdbstub assertions flip because every render now emits a `<qemu:commandline>` with the SSH args:
  `test_render_emits_loopback_gdbstub_when_flag_set` asserts the exact arg list
  `== ["-gdb", "tcp:127.0.0.1:4444"]` (line 298) — must become a subset/`in` check, or assert the
  gdb args are present alongside the SSH args; and `test_render_omits_gdbstub_when_flag_unset` asserts
  `find("qemu:commandline") is None` (line 305) — that proxy is no longer valid (the SSH forward owns a
  commandline element), so assert specifically that `recorded_gdb_port is None` / no `-gdb` arg.

**TDD steps:**

1. Update/replace the render tests first and watch them fail:
   - Replace `test_render_omits_ssh_forward_when_no_credential_ref` and
     `test_render_ignores_ssh_port_when_no_credential_ref` with a single
     `test_render_emits_ssh_forward_without_credential_ref`: render a **default** profile (no
     `ssh_credential_ref`) with `ssh_port=40022`, assert `recorded_ssh_port(xml) == 40022` and the
     exact `-netdev`/`-device` arg list (the same four args the credential-ref test checks).
   - Rename `test_render_rejects_credential_ref_without_an_ssh_port` →
     `test_render_rejects_missing_ssh_port`: render a **default** profile with `ssh_port=None`, assert
     `CONFIGURATION_ERROR` (the rejection no longer depends on the credential ref).
   - Add `ssh_port: int = 40022` to the `_render()` helper and thread it into its `render_domain_xml`
     call. Add `ssh_port=<port>` to the direct call sites enumerated above (109, 110, 242, 251, 289,
     310, 329) that go through `render_domain_xml` directly rather than the helper.
   - Fix the two flipped gdbstub assertions (298, 305) per the verification bullet.
   - Keep `test_render_emits_loopback_ssh_forward_when_credential_ref_set` and
     `test_render_gdbstub_and_ssh_share_one_commandline_element` (credential-ref path still works).
2. Implement in `xml.py`:
   - Replace `if section.ssh_credential_ref is not None: _append_ssh_forward(domain, ssh_port)` with an
     unconditional `_append_ssh_forward(domain, ssh_port)`.
   - Reword `_append_ssh_forward`'s `ssh_port is None` `CONFIGURATION_ERROR` message: a local-libvirt
     domain always renders the SSH forward and so requires an allocated SSH port (drop the
     "drgn-live System (`ssh_credential_ref` set)" framing).
   - Update the `render_domain_xml` docstring: the forward is rendered unconditionally; `ssh_port` is
     required (a `None` is `CONFIGURATION_ERROR`, mirroring `kernel_path`); `ssh_credential_ref`'s
     remaining role is the drgn-live introspection credential.
3. `just lint && just type` and the focused test file green.

**Acceptance:** a default-profile render emits the forward and round-trips the port; a render with
`ssh_port=None` raises `CONFIGURATION_ERROR`; the gdbstub + credential-ref coexistence test still
passes (one `<qemu:commandline>` element).

## Task 2 — Provisioner: always allocate the SSH port

**Where it fits:** the production caller must always pass `ssh_port` now that the renderer requires it.

**Files:** `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`;
`tests/providers/local_libvirt/test_provisioning.py`.

**TDD steps:**

1. Flip `test_provision_non_ssh_does_not_allocate_an_ssh_port` →
   `test_provision_always_allocates_an_ssh_port`: a default-profile provision records an SSH port
   (assert `recorded_ssh_port(conn.recorded_xml[-1])` is the allocated port). Watch it fail.
2. Fix the constant-`free_port` fakes that a gdbstub provision now shares between gdb and ssh:
   `test_provision_gdbstub_allocates_a_fresh_port_when_no_prior_domain` (and any other gdbstub
   provision using `lambda: 5555`) must use a distinct-per-call counter (mirror the existing
   `test_provision_gdbstub_and_ssh_allocate_both_ports` `iter([...])` pattern) so `-gdb tcp:...:<p>`
   and `hostfwd=...:<p>-:22` do not collide on one port. Allocation order is gdb-then-ssh
   (`provisioning.py:211-212`); the counter sequence reflects that.
3. Implement: replace
   `ssh_port = self._ssh_port_for(system_id) if section.ssh_credential_ref is not None else None`
   with the unconditional `ssh_port = self._ssh_port_for(system_id)`.
4. Confirm the reuse-on-retry tests (`test_provision_ssh_reuses_the_recorded_port_on_retry`) and the
   infra-error test still pass unchanged; `just test` for the file green.

**Acceptance:** a default-profile provision allocates and records an SSH port; a gdbstub provision
allocates two distinct ports; reuse-on-retry and infra-error paths unchanged.

## Task 3 — Agent surface: reword the unprovisioned detail

**Where it fits:** the `recorded_ssh_endpoint` → `None` branch is now reachable only for providers
that expose no forward (remote/fault-inject) or a pre-change domain — the message must stop
prescribing a local reprovision.

**Files:** `src/kdive/mcp/tools/lifecycle/systems/ssh_access.py`;
`tests/mcp/lifecycle/test_systems_ssh_access.py`.

**TDD steps:**

1. Update the test that asserts the `None`-endpoint envelope (`_FakeConnector(None)` →
   `CONFIGURATION_ERROR`, `reason=ssh_not_provisioned`) to expect the reworded detail text. Watch it
   fail.
2. Reword `_UNPROVISIONED_DETAIL` in `ssh_access.py`: the System's provider does not expose a loopback
   SSH forward; direct SSH to a System is a local-libvirt capability. Keep the
   `data={"reason": "ssh_not_provisioned"}` discriminator. (`ssh_info` and `authorize_ssh_key` share
   the constant, so one edit covers both.)
3. Focused test file green.

**Acceptance:** a `None`-endpoint System still returns `CONFIGURATION_ERROR` +
`reason=ssh_not_provisioned`, now with the provider-capability wording; success paths unchanged.

## Task 4 — Full guardrails + cross-tree checks

**Files:** none (verification only).

**Steps:**

1. `just lint && just type && just test` (whole tree; the boot/architecture/doc-generation tests live
   outside the edited dirs).
2. Grep for any other test asserting "forward NOT rendered without credential ref" or the old
   `_UNPROVISIONED_DETAIL` wording that the focused runs did not surface
   (`rg -n 'ssh_not_provisioned|not provisioned for SSH|omits_ssh' tests/`).
3. Confirm `tests/providers/local_libvirt/test_connect.py` (resolver, untouched) and
   `tests/mcp/core/test_tool_docs.py` still pass.

**Acceptance:** whole-tree `just lint`/`type`/`test` green; no stale negative assertions remain.

## Rollback / cleanup

Pure code + test change, no migration, no schema, no new tool. Rollback is a `git revert` of the
implementation commits; nothing persists state. No generated snapshots are invalidated (the change
adds no field/model/schema output).
