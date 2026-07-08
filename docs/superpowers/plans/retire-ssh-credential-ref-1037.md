# Plan: Retire the vestigial drgn-live `ssh_credential_ref` (#1037)

- **Issue:** [#1037](https://github.com/randomparity/kdive/issues/1037)
- **Spec:** [`docs/superpowers/specs/2026-07-08-retire-ssh-credential-ref-1037-design.md`](../specs/2026-07-08-retire-ssh-credential-ref-1037-design.md)
- **ADR:** [ADR-0315](../../adr/0315-retire-drgn-live-ssh-credential-ref.md)
- **Branch:** `feat/retire-vestigial-ssh-credential-ref-1037` (off `main`)
- **Guardrails (run individually; CI gates each):**
  `just lint` · `just type` · `just test` · `just docs-check` · `just config-docs-check` ·
  `just resources-docs-check` · `just docs-links` · `just docs-paths` · `just adr-status-check`

## Orientation

drgn-live `debug.start_session` currently fails closed unless the local-libvirt profile carries
`ssh_credential_ref` pointing at an operator-placed key file — a knob no MCP tool can create — even
though `introspect.run` authenticates with the per-System bootstrap key (ADR-0289) and the transport
open is only a read-only SSH banner probe. This plan retires that vestigial credential path, gates
drgn-live on the bootstrap key's presence, and seeds redaction from the bootstrap key. See the spec
for the full mechanism and rejected alternatives.

**Hard scope boundary:** `ssh_credential_ref` is an overloaded name. The **build-host** use (a
`build_hosts` DB column + `providers/shared/build_host/**` SSH transport + `build_hosts.*` tools) is
a different store and is **out of scope — do not touch it or its tests**. Every task below is the
drgn-live / local-libvirt profile path only.

**TDD:** each code task writes/updates the failing test first, then the change. Run the named
guardrail(s) before committing. One logical change per commit; conventional-commit subject ≤72 chars;
`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer. Stage explicit paths
(never `git add -A`).

**Global acceptance gate (check after Task 6):** `rg ssh_credential_ref src/ tests/ docs/` shows
matches **only** in the build-host store. Any local-libvirt-profile-context match is a miss.

---

## Task 1 — Retire the `ssh_credential_ref` profile field

**Where it fits:** the schema change everything else follows from. Do it first so the parse break is
visible to every dependent test.

**Files:**
- `src/kdive/profiles/provisioning.py` — remove `LibvirtProfile.ssh_credential_ref` (line ~142) and
  the `ssh_credential_ref` sentence from the `LibvirtProfile` docstring (lines ~118–121). Leave the
  `ADR-0039 §2` provenance comment only if it still describes a present field; otherwise drop the
  `ssh_credential_ref` clause from the provenance line.
- `tests/profiles/test_provisioning.py` — remove `test_ssh_credential_ref_defaults_to_none`,
  `test_ssh_credential_ref_parses_when_present`,
  `test_ssh_credential_ref_returns_none_for_provider_without_ssh_credentials`,
  `test_ssh_credential_ref_rejects_blank`, and the accessor assertions at ~193 / ~757. **Add**
  `test_local_libvirt_ssh_credential_ref_now_rejected`: a profile document whose `local-libvirt`
  section carries `ssh_credential_ref` raises `CategorizedError` /
  `ErrorCategory.CONFIGURATION_ERROR` from `ProvisioningProfile.parse` (the `extra="forbid"` path).

**TDD:** write the new rejection test first (it fails while the field exists — actually it passes
only once removed; so write it, watch it fail because the field parses, then remove the field).

**Acceptance:** `uv run python -m pytest tests/profiles/test_provisioning.py -q` green; `just type`
green. `LibvirtProfile` no longer has an `ssh_credential_ref` attribute.

**Note:** this task alone will red-fail other suites (policy adapters, sessions). That is expected —
Tasks 2–3 land in the same series before running the full suite. Keep the branch compiling by doing
Tasks 1–3 close together, or accept transient red between commits (each commit still runs its own
scoped guardrail).

---

## Task 2 — Collapse the two vestigial `ProfilePolicy` methods into one seam

**Where it fits:** removes the `ssh_credential_ref` accessor and repurposes the boolean gate to the
bootstrap-key seed decision.

**Files:**
- `src/kdive/profiles/provider_policy.py` — in the `ProfilePolicy` Protocol: delete
  `ssh_credential_ref`; rename `drgn_live_requires_credential` →
  `drgn_live_seeds_bootstrap_key`, docstring: "True iff the drgn-live transport-open at
  `start_session` authenticates over the loopback SSH forward, so `start_session` must gate on the
  per-System bootstrap key and seed redaction from it."
- `src/kdive/providers/local_libvirt/profile_policy.py` — delete `ssh_credential_ref`; rename the
  method, keep `return True`.
- `src/kdive/providers/remote_libvirt/profile_policy.py` — delete `ssh_credential_ref`; rename,
  keep `return False`. Add a one-line comment: remote has+uses a bootstrap key at `introspect.run`
  but opens its transport over the guest agent, so no start-time seed.
- `src/kdive/providers/fault_inject/profile_policy.py` — delete `ssh_credential_ref`; rename, keep
  `return False`.
- `tests/profiles/test_provisioning.py` — rename the three `drgn_live_requires_credential` tests
  (~774–788) to `drgn_live_seeds_bootstrap_key`, asserting `True` (local) / `False` (remote,
  fault-inject).

**Acceptance:** `grep -rn "ssh_credential_ref\|drgn_live_requires_credential" src/kdive/profiles
src/kdive/providers/*/profile_policy.py` returns nothing; the four policy modules expose
`drgn_live_seeds_bootstrap_key`; `just type` green;
`uv run python -m pytest tests/profiles/test_provisioning.py -q` green.

---

## Task 3 — Rewire `start_session` to gate+seed on the bootstrap key; delete dead credential plumbing

**Where it fits:** the behavioral core — makes drgn-live reachable and moves the redaction seed to
the real secret.

**Files:** `src/kdive/mcp/tools/debug/sessions_lifecycle.py`, `src/kdive/mcp/tools/debug/sessions.py`.

**Change:**
1. In `_prepare_attach_request` (`sessions_lifecycle.py`), replace the `backend =
   self._credential_backend(...)` + `_resolve_credential(...)` block with: for a `_DRGN_LIVE`
   transport where `resources.profile_policy.drgn_live_seeds_bootstrap_key(profile)` is true,
   ```python
   try:
       await load_system_bootstrap_private_key(
           conn, system.id, secret_registry=self._secret_registry
       )
   except CategorizedError as exc:
       return ToolResponse.failure_from_error(str(system.id), exc)
   ```
   (parse the profile from `system.provisioning_profile` as `_resolve_credential` did, or reuse the
   already-parsed profile). Keep it **before** `_open_transport` runs (it is — `_prepare_attach_request`
   returns the request, then `start_session` calls `_open_transport`), preserving ADR-0039 §2 ordering.
   Import `load_system_bootstrap_private_key` from `kdive.prereqs.system_bootstrap_key`.
2. Delete `_resolve_credential`, `_credential_backend`, the `secret_backend_factory` constructor
   param + `self._secret_backend_factory` attribute on `DebugSessionLifecycle`, and the
   `secret_backend_factory` plumbing through the `create`/`__init__` chain
   (`sessions_lifecycle.py:245,254,265,273,371–376`).
3. Delete the now-registrant-less session-scoped release machinery: `_secret_scope`,
   `_release_failed_attach_secret`, the `secret_scope` local + `_release_failed_attach_secret`
   calls in `start_session` (`307,316,331`), the `self._secret_registry.release(secret_scope)` in
   the except branch (`329`), and `end_session`'s `self._secret_registry.release(_secret_scope(uid))`
   (`417`). Keep `self._secret_registry` (the loader registers through it). Verify gdbstub registers
   no secret (it does not) so nothing else depends on these.
4. `sessions.py` — delete `_secret_backend_factory` builder (`98–121`) and its wiring into the
   lifecycle construction (`82–92, 121`).
5. Rewrite the `start_session` docstring (`286–294`) to drop `ssh_credential_ref` /
   `secret_backend_factory` and describe the bootstrap-key gate+seed. No `ssh_credential_ref` string
   may remain in this module.

**TDD (`tests/mcp/debug/test_debug_tools.py`):** replace the ref-resolution start_session tests with:
- drgn-live `start_session` on a ready local System whose profile has **no** `ssh_credential_ref`
  succeeds (inserts a `live` session);
- after that start, `secret_registry.snapshot()` contains the System's bootstrap key value;
- a System with no bootstrap-key row → `configuration_error` with `reason="no_bootstrap_key"`;
- remote / fault-inject drgn-live start performs no bootstrap-key seed (unchanged path).
Grep the existing tests for the removed factory/`ssh_credential_ref_missing` fixtures and delete or
retarget them.

**Acceptance:** `uv run python -m pytest tests/mcp/debug/test_debug_tools.py -q` green;
`grep -n ssh_credential_ref src/kdive/mcp/tools/debug/` empty; `just type` green.

---

## Task 4 — Reword the connect-plane "no SSH forward" error

**Where it fits:** removes the last source reference in the connect path.

**Files:**
- `src/kdive/providers/local_libvirt/lifecycle/connect.py` — `_resolved_ssh_port` (~329): reword
  "reprovision with the profile's `ssh_credential_ref` set" to a field-free message, e.g.
  `f"System {domain_name!r} has no recorded SSH forward; reprovision to obtain one."` Also scrub the
  `ssh_credential_ref` mentions in the module/`recorded_ssh_endpoint` docstrings if any remain.
- `tests/providers/local_libvirt/test_connect.py` — update the asserted error substring.

**Acceptance:** `uv run python -m pytest tests/providers/local_libvirt/test_connect.py -q` green;
no `ssh_credential_ref` in `connect.py`.

---

## Task 5 — Scrub `ssh_credential_ref` from agent-facing docs, comments, and capability message

**Where it fits:** fixes the actual P6 discoverability defect and keeps the served resources honest.

**Files (source of served content + comments):**
- `src/kdive/mcp/resources/_content/toolsets-introspect.md` (~13),
  `toolsets-debug.md` (~49), `agent-index.md` (~94, 111), and the pre-provision-checklist served
  content that lists `ssh_credential_ref` as a provision-bound knob — reword to: drgn-live needs no
  credential provisioning; it works on any ready local System (the SSH forward always renders,
  ADR-0281). State the real prerequisite is a drgn-capable guest image (`introspect.run` reports
  `missing_dependency` otherwise).
- `src/kdive/images/capability_signals.py` (~127) — reword the `drgn` capability message to drop
  `ssh_credential_ref` ("drgn liveness depends on provider introspection and a drgn-capable guest").
- `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` (~239) and `xml.py` (~65, 132) —
  update the code comments that describe `ssh_credential_ref` as the drgn-live gate.

**Tests:**
- `tests/mcp/resources/test_pre_provision_checklist_docs.py` (~32) — drop `ssh_credential_ref` from
  the asserted token set.
- `tests/mcp/lifecycle/test_recovery_redaction.py` (~81, 97) and `test_recovery_helpers.py` (~80) —
  these plant `provider.local-libvirt.ssh_credential_ref` as a redaction/allowlist marker. Read each
  test's intent, then drop the now-subjectless `test_system_envelope_excludes_ssh_credential_ref` and
  the `ssh_credential_ref` plant in `test_recovery_helpers.py`; the `kernel_source_ref` plant already
  present in both tests keeps the envelope-exclusion / allowlist path covered. Do **not** invent a new
  secret field to keep the test alive — the section has none.
- Update any served-resource snapshot / `test_provisioning_for_debugging_docs` assertions that
  reference the removed text.

**Acceptance:** `uv run python -m pytest tests/mcp/resources -q` green; the reworded docs contain no
`ssh_credential_ref`.

---

## Task 6 — Fix the two integration fixtures and regenerate generated docs

**Where it fits:** clears the remaining local-libvirt-profile parse breaks and syncs generated
references so their CI gates pass.

**Files:**
- `tests/integration/test_live_stack.py` (~197 fixture; ~79, 180, 662, 665 narration) and
  `tests/integration/test_console_parts_live.py` (~132 fixture; ~169 error assertion) — remove the
  profile `ssh_credential_ref`; a default local-libvirt profile now renders the same SSH forward.
  Update `test_console_parts_live.py:169` to the reworded connect error. These are `live_stack`/`live`
  gated but their fixtures parse in setup — the change must keep them parseable.
- Regenerate: `just docs` (tool reference), then the config-doc generator so
  `just config-docs-check` passes (the profile-schema section drops the field), then confirm
  `just resources-docs-check` (served-resource snapshot) is in sync with Task 5.

**Acceptance:** `just docs-check`, `just config-docs-check`, `just resources-docs-check` all green;
committed generated files match a fresh generation.

---

## Task 7 — Full guardrail sweep + global grep gate

**Where it fits:** the closing verification before review.

**Steps:**
1. `rg ssh_credential_ref src/ tests/ docs/` — confirm matches are **only** the build-host store
   (`db/build_hosts.py`, `db/schema/0027*.sql`, `0029*.sql`,
   `inventory/reconcile/build_hosts.py`, `mcp/tools/ops/build_hosts/**`,
   `providers/shared/build_host/**`, and their tests). Zero local-libvirt-profile matches.
2. Run each guardrail: `just lint`, `just type`, `just docs-check`, `just config-docs-check`,
   `just resources-docs-check`, `just docs-links`, `just docs-paths`, `just adr-status-check`, then
   the full `just test`.
3. Confirm acceptance criteria 1–8 from the spec are each satisfied by a test or a grep.

**Acceptance:** every guardrail green; grep gate clean.

---

## Rollback / cleanup

No DB migration and no persisted-state change — rollback is a branch revert. If a stored profile in a
running dev DB carries the retired field it will now fail to parse (accepted pre-release break, spec
criterion 2 + ADR consequences); reprovision that System rather than adding a compat shim.
