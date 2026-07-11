# Operator-gated local-libvirt egress (#1031) Implementation Plan

> **For agentic workers:** Use superpowers:test-driven-development per task — write the failing test first, then the code. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let an operator opt a local-libvirt Resource into guest egress via a `guest_egress` field on the `[[local_libvirt]]` `systems.toml` block, so a booted guest renders `restrict=off` (SLIRP NAT + DNS) and an agent can `dnf`/`apt install` at runtime. Default (absent/false) renders `restrict=on`, unchanged.

**Architecture:** `guest_egress` is resolved **op-time from `systems.toml` by the allocated Resource's `name`** (mirroring remote's `remote_config_for_resource`), not persisted — no migration. It activates the existing-but-no-op local `rebind_for_resource` seam and threads a keyword-only `guest_egress` bool through `render_domain_xml` → `_append_ssh_forward`, flipping `restrict`. Knob is unreachable from the request (confused-deputy avoidance).

**Tech stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`. No migration, no new tool/RBAC/config-env.

## Global Constraints

- ADR: [0313](../../adr/0313-local-libvirt-operator-gated-egress.md). Spec: [egress-optin-1031](../../specs/2026-07-05-local-libvirt-egress-optin-1031.md). Amends ADR-0218 §1.
- Guardrails run individually in CI: `just lint`, `just type` (whole tree, src+tests), `just test`. Doc guards: `just docs-links`, `just adr-status-check`, `just docs-check`, `just config-docs-check`, `just config-guard`. Run the whole `just test` before push (guards stop at first failure).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, whole tree.
- `ErrorCategory` from `domain/errors.py` — most specific existing value, never invent strings. No new category here.
- Absolute imports only. Google-style docstrings on non-trivial public APIs.
- Doc-style: **Milestone** not "Sprint"; plain prose (no "critical"/"robust"/"comprehensive"/"elegant"/"significant").
- **Secure default is `restrict=on`.** Every task must keep the no-`guest_egress` path byte-for-byte identical to today.

---

### Task 1: `guest_egress` field on `LocalLibvirtInstance`

**Where it fits:** The operator-facing surface — the `[[local_libvirt]]` inventory block gains the opt-in field.

**Files:**
- Modify: `src/kdive/inventory/model.py` (`LocalLibvirtInstance`, ~line 179)
- Test: `tests/inventory/test_model.py` (or the existing inventory-parse test module)

- [ ] Write a failing test: a `[[local_libvirt]]` block with `guest_egress = true` parses to `instance.guest_egress is True`; a block omitting the key defaults to `False`; a file with the key absent entirely parses unchanged.
- [ ] Add `guest_egress: bool = False` to `LocalLibvirtInstance` with a one-line field doc-comment (operator opt-in to guest outbound egress; default off preserves `restrict=on`).
- [ ] `just lint type` + the model test green.

**Acceptance:** Field parses, defaults `False`, back-compatible with keyless files.

---

### Task 2: Thread `guest_egress` through the renderer

**Where it fits:** The one line that hardcodes `restrict=on` becomes flag-driven. Independent of Tasks 1/3.

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/xml.py` (`render_domain_xml` ~line 38, `_append_ssh_forward` ~line 188/206)
- Test: `tests/providers/local_libvirt/test_provisioning.py`

- [ ] Write failing render tests:
  - default (`_render()` / no `guest_egress`) → `-netdev` arg contains `restrict=on` (keep/adapt existing assertions).
  - `render_domain_xml(..., guest_egress=True)` → `-netdev` arg contains `restrict=off`; assert the **full** netdev arg so only `restrict` flips (`user,id=kdivessh,restrict=off,hostfwd=tcp:127.0.0.1:<port>-:22`), and the `-device virtio-net-pci,netdev=kdivessh,addr=0x10` arg is unchanged.
  - explicit `guest_egress=False` → `restrict=on`.
- [ ] Add keyword-only `guest_egress: bool = False` to `render_domain_xml`; forward it to `_append_ssh_forward(domain, ssh_port, guest_egress=guest_egress)`. In `_append_ssh_forward`, compute `restrict = "off" if guest_egress else "on"` and interpolate into the `netdev` string.
- [ ] Update both docstrings: `restrict=on` is now the **default of an operator policy** (ADR-0313), not unconditional; the egress case renders `restrict=off`.
- [ ] `just lint type` + the render tests green.

**Acceptance:** Renderer flips `restrict` on the flag; default path byte-for-byte unchanged; `ssh_port=None` still raises `CONFIGURATION_ERROR` (untouched).

---

### Task 3: Op-time resolver `local_guest_egress_for_resource`

**Where it fits:** Reads the operator field from `systems.toml` at op time, keyed by resource name — the migration-free seam. **Depends on Task 1** (the resolver reads `instance.guest_egress`, so the field must exist first). Independent of Task 2.

**Files:**
- Create: `src/kdive/providers/local_libvirt/config.py`
- Test: `tests/providers/local_libvirt/test_egress_config.py` (new)

- [ ] Write failing resolver tests (drive the **real** loader against a `tmp_path` `systems.toml` via `KDIVE_SYSTEMS_TOML`, not a stub):
  - block `guest_egress = true` for name `N` → `local_guest_egress_for_resource("N") is True`.
  - block present, key omitted / `false` → `False`.
  - no `[[local_libvirt]]` block naming `N` → `False` (no error).
  - absent `systems.toml` → `False`.
  - **malformed `systems.toml` → `False`** (degrades to secure default, warning logged) — assert it does **not** raise. This is the F1 blast-radius fix.
- [ ] Implement `local_guest_egress_for_resource(resource_name: str) -> bool`: load via `load_inventory_optional(systems_toml_path())` (mirror `remote_libvirt/config.py::_load_inventory_doc` shape), select the `[[local_libvirt]]` instance by `name`, return its `guest_egress` or `False`. Catch `InventoryError`, log a warning (`logging.getLogger(__name__)`) naming the file + that egress defaults off, return `False`. Log the consulted `resource_name` at debug so a name-mismatch no-op is diagnosable.
- [ ] `just lint type` + resolver tests green.

**Acceptance:** Resolver returns the operator value; degrades to `False` on missing/malformed/no-match; never raises on the op path.

---

### Task 4: Provisioner carries `guest_egress` into `provision()`

**Where it fits:** Between the resolver (Task 3) and the renderer (Task 2). Depends on Task 2.

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` (`LocalLibvirtProvisioning`, `from_env` ~line 176, `provision` ~line 234)
- Test: `tests/providers/local_libvirt/test_provisioning.py`

- [ ] Write a failing test: a `LocalLibvirtProvisioning` built with `guest_egress=True` renders a domain whose `-netdev` carries `restrict=off`; the default (`from_env()`) renders `restrict=on`.
- [ ] Add a `guest_egress: bool = False` field to the provisioner; `from_env` accepts `guest_egress: bool = False` (keyword) and stores it; `provision()` passes `guest_egress=self._guest_egress` to `render_domain_xml`. Keep every other `from_env` caller working (optional kwarg, default preserves today's behavior).
- [ ] `just lint type` + provisioning tests green.

**Acceptance:** Provisioner-level flag reaches the rendered NIC; default unchanged.

---

### Task 5: Activate the `rebind_for_resource` seam in local composition

**Where it fits:** Wires the resolver (Task 3) to the provisioner (Task 4) through the per-op resolver chokepoint. Depends on Tasks 3 + 4.

**Files:**
- Modify: `src/kdive/providers/local_libvirt/composition.py` (`build_runtime` ~line 94)
- Test: `tests/providers/local_libvirt/test_composition.py` (new or existing composition test)

- [ ] Write failing tests, mirroring `tests/providers/remote_libvirt/test_composition.py`'s rebind tests (`SecretRegistry()` is constructed directly; the resolver is **monkeypatched**, not driven off a real file — the real-loader path is Task 3's test): (a) `build_runtime(secret_registry=SecretRegistry()).rebind_for_resource is not None`; (b) with `monkeypatch.setattr(composition, "local_guest_egress_for_resource", lambda name: name == "egress-on")`, `runtime.for_resource("egress-on").provisioner` renders `restrict=off` and `runtime.for_resource("egress-off").provisioner` renders `restrict=on`.
- [ ] Add `resource_name: str | None = None` to `build_runtime`. When set, resolve `local_guest_egress_for_resource(resource_name)` and build `LocalLibvirtProvisioning.from_env(guest_egress=...)`; when `None`, keep egress-off. Set `rebind_for_resource=lambda name: build_runtime(secret_registry=secret_registry, resource_name=name)`.
- [ ] Confirm no recursion hazard: `for_resource` is called once per op by the resolver; `build_runtime`'s hook calls `build_runtime` with a `resource_name` but nothing re-invokes `for_resource` inside it (identical to remote's `_rebind_for_resource`).
- [ ] `just lint type` + composition test green. Run the full `just test` — the rebind now runs for every local op, so confirm no existing local provider test regressed on the new per-op `systems.toml` read (they should hit the absent-file → `False` default).

**Acceptance:** The operator field reaches the rendered NIC end-to-end via the resolver chokepoint; every non-egress local op still works with no `systems.toml`.

---

### Task 6: Docs

**Where it fits:** Acceptance criterion — state when runtime installs work and how the operator enables egress.

**Files:**
- Modify: `systems.toml.example` (document `guest_egress` on the `[[local_libvirt]]` block)
- Modify: `docs/operating/runbooks/image-lifecycle.md` (when runtime installs work; how to enable egress; residual-threat note; how to read the resource `name` so the field matches; reprovision-to-take-effect)
- Modify: `docs/guide/toolsets/systems.md` (the #998 "guest is yours as root" guidance now qualified: runtime installs need operator-enabled egress on local-libvirt)

- [ ] Add a commented `guest_egress = true` example to the `[[local_libvirt]]` block in `systems.toml.example` with a one-line security note (drops `restrict=on`; operator's network zone is the boundary).
- [ ] Runbook: a short "Runtime tool installs on local-libvirt" section — default is no egress (`restrict=on`); enable per-Resource via `guest_egress`; name must match the discovery-created resource (say how to read it); residual threat operator accepts; takes effect on next provision.
- [ ] Toolset guide: qualify the runtime-install guidance for local-libvirt.
- [ ] `just docs-links docs-check config-docs-check config-guard` green (regenerate generated docs if any `@app.tool`/`Field`/config surface changed — none expected here, but verify no drift).

**Acceptance:** An operator can find how to enable egress and what they are accepting; the #998 guidance no longer over-promises on local-libvirt.

---

### Task 7 (deferred / live_vm): behavioral proof

**Where it fits:** Verifies acceptance criterion 1 (a package actually installs) — the render tests cannot prove guest DHCP/DNS.

- [ ] `live_vm`-gated: provision an egress-enabled local System and assert an in-guest `dnf`/`apt install` (or DNS-resolves + mirror-reachable) succeeds. If a live drive is out of scope for the shipping PR, state the deferral explicitly in the spec and the PR body (the necessary-but-not-sufficient / guest-DHCP live-proof risk, ADR-0218 §1). Do not claim the behavioral criterion met from the render alone.

**Acceptance:** Either a green `live_vm` proof, or an explicit, visible deferral of it — never a silent gap.

---

## Rollback / cleanup

Pure-additive: a `bool = False` model field, a new resolver module, a keyword arg with a default, and a rebind hook. Reverting the branch restores today's behavior exactly (the default path is unchanged throughout). No migration to reverse, no data to clean up.
