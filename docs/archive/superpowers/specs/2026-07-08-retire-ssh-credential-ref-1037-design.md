# Spec: Retire the vestigial `ssh_credential_ref` and gate drgn-live on the per-System bootstrap key (#1037)

- **Issue:** [#1037](https://github.com/randomparity/kdive/issues/1037)
- **ADR:** [ADR-0315](../../adr/0315-retire-drgn-live-ssh-credential-ref.md)
- **Status:** Draft
- **Date:** 2026-07-08
- **Provider scope:** local-libvirt (remote-libvirt / fault-inject drgn-live behavior unchanged)

## Problem

The `introspect.*` (drgn-live) surface — a headline capability — is unreachable in the
`local-dev` deployment (BLACK_BOX_REVIEW.md P6). A reader following the "provision for live
introspection" guidance hits a dead end: `debug.start_session(transport="drgn-live")` fails
closed unless the provisioning profile carries a `ssh_credential_ref` pointing at an
operator-placed key file under `KDIVE_SECRETS_ROOT`, and **no MCP tool creates that file**.

The gate is vestigial. Source review establishes:

1. **`introspect.run` / `introspect.script` do not use `ssh_credential_ref`.** They authenticate
   with the per-System **bootstrap key** (ADR-0289): `load_system_bootstrap_private_key` →
   `materialized_private_key` → `key_path` (`mcp/tools/debug/introspect.py:212,279,371`). Their
   schemas take only `session_id` + `helper`/`script`/`timeout`.
2. **The transport open at `start_session` never authenticates.** `LocalLibvirtConnect._open_ssh`
   (`providers/local_libvirt/lifecycle/connect.py:162`) does a read-only SSH **banner probe** — it
   reads the `SSH-` identification string and returns. The resolved `ssh_credential_ref` value is
   never threaded into the connector.
3. **The `ssh_credential_ref` gate's only live effects** are therefore: (a) an opt-in gate that
   fails closed with `reason="ssh_credential_ref_missing"` (`sessions_lifecycle.py:_resolve_credential`),
   and (b) seeding the redaction registry with the *file's* value — a secret that is never used to
   authenticate anything.
4. The SSH forward itself renders on **every** local domain (ADR-0281/0218), and every ready
   System has a bootstrap key (ADR-0289). drgn-live has **no residual provisioning prerequisite**.

So the gate blocks the capability while adding no authentication value, and seeds redaction with
the wrong secret.

## Decision (summary)

Retire the `ssh_credential_ref` credential path for drgn-live. Gate a drgn-live `start_session`
on the **per-System bootstrap key's presence** (fail closed if absent) and seed redaction from the
bootstrap key value — the secret actually in play — before the transport opens, preserving the
ADR-0039 §2 "seed-before-output" ordering. Remove `ssh_credential_ref` from the local-libvirt
profile section and the two vestigial `ProfilePolicy` methods.

**No opt-in knob.** drgn-live is available on every ready local System. It is read-only,
contributor-RBAC-gated, and non-destructive; the SSH forward always renders and the bootstrap key
is always present. Re-introducing a required profile field would recreate the exact P6
discoverability dead-end this fixes. The real prerequisite — drgn installed in-guest — is still
enforced downstream: `introspect.run` reports `missing_dependency` off a prepared host
(`introspect.py:242`). Access control is RBAC's job, not a per-profile boolean.

Full rationale and rejected alternatives: ADR-0315.

## Scope boundary

`ssh_credential_ref` is an **overloaded name** with two unrelated uses:

- **Build-host SSH auth** — a column on `build_hosts` + the build-host SSH transport
  (`db/build_hosts.py`, `providers/shared/build_host/transports/ssh_transport.py`, the
  `build_hosts.*` tools). **Out of scope; untouched.**
- **drgn-live debug credential** — the `LibvirtProfile.ssh_credential_ref` profile field and its
  `ProfilePolicy` accessors. **This spec retires only this one.**

Every change below is scoped to the drgn-live path. Build-host code and tests are not modified.

## Design

### 1. Profile schema (`profiles/provisioning.py`)

Remove `LibvirtProfile.ssh_credential_ref` (line 142) and the `ssh_credential_ref` clause from the
`LibvirtProfile` docstring (lines 118–121). The models are `extra="forbid"` + `frozen`, so a
submitted or **stored** profile document that still carries `ssh_credential_ref` in its
local-libvirt section is now rejected at parse with `configuration_error` (the standard
unknown-field mapping in `ProvisioningProfile.parse`).

This is a pre-release greenfield break (ADR-0024 profiles are immutable request inputs; there is no
cross-version profile-compatibility promise, and the repo policy is replace-don't-deprecate with no
migration shims). No DB migration is needed — the profile lives in the `systems.provisioning_profile`
jsonb; there is no column.

### 2. ProfilePolicy seam (`profiles/provider_policy.py` + 3 adapters)

Replace the two vestigial methods:

- **Remove** `ssh_credential_ref(profile) -> str | None`.
- **Replace** `drgn_live_requires_credential(profile) -> bool` with
  `drgn_live_seeds_bootstrap_key(profile) -> bool` — "True iff the drgn-live transport-open **at
  `start_session`** authenticates over the loopback SSH forward, so `start_session` must gate on the
  per-System bootstrap key's presence and seed redaction from it."
  - local-libvirt → `True`
  - remote-libvirt → `False`
  - fault-inject → `False`

The boolean shape and provider values are identical to the method it replaces; only the name and
its meaning change. `Protocol` + all three adapters update together.

**Precise remote-libvirt rationale (do not misdescribe):** remote-libvirt systems *do* have a
per-System bootstrap key, and remote drgn-live *does* use it — the shared `introspect.run` path
(`introspect.py:212`) calls `load_system_bootstrap_private_key` for **every** provider and hands the
key to `RemoteLibvirtLiveIntrospect.introspect_live(key_path=...)`. Remote returns `False` here only
because its drgn-live transport is opened over the **guest-agent seam, not a bootstrap-key SSH
probe**, so nothing needs seeding/gating *at start_session*. The seam name is deliberately scoped to
the start-time seed — it must not be read as "remote never touches the bootstrap key."

### 3. Session start (`mcp/tools/debug/sessions_lifecycle.py`)

Replace `_resolve_credential` (ref-based) with a bootstrap-key seed for drgn-live. In
`_prepare_attach_request`, after the transport-support check, for a drgn-live transport where
`profile_policy.drgn_live_seeds_bootstrap_key(profile)` is true:

```python
try:
    await load_system_bootstrap_private_key(
        conn, system.id, secret_registry=self._secret_registry
    )
except CategorizedError as exc:
    return ToolResponse.failure_from_error(str(system.id), exc)
```

`load_system_bootstrap_private_key` (`prereqs/system_bootstrap_key.py:109`) already:

- raises `CONFIGURATION_ERROR` with `details={"reason": "no_bootstrap_key", "system_id": ...}` when
  the key row is absent — the fail-closed gate; and
- registers the key value into the passed `SecretRegistry` for redaction — the seed.

So the new path reuses tested machinery. It runs under `conn` (already open in
`_prepare_attach_request`) and before `_open_transport`, satisfying ADR-0039 §2 ordering (the seed
precedes any transport output; the banner probe produces none anyway).

Remove the now-dead credential plumbing on the debug-session path only. Because
`load_system_bootstrap_private_key` registers the key with `scope=None` (process-global, see §Failure
modes), **no secret is ever registered under `_secret_scope(session_id)` on the debug path anymore**,
so the entire session-scoped seed/release machinery goes dead and must be removed together — leaving
it would falsely imply drgn secrets are session-scoped and released:

- `_credential_backend`, the `secret_backend_factory` param/attr on `DebugSessionLifecycle`
  (`sessions_lifecycle.py:245,254,265,273,371–376`), and the `_secret_backend_factory` builder +
  its wiring in `sessions.py:82–121`.
- `_resolve_credential` itself (replaced by the loader call above).
- The session-scoped secret release machinery that now has no registrant:
  `_secret_scope`, `_release_failed_attach_secret`, the `secret_scope` local in `start_session`
  (`sessions_lifecycle.py:307,316,331`), the `self._secret_registry.release(secret_scope)` in
  `start_session`'s except path (`329`), and `end_session`'s
  `self._secret_registry.release(_secret_scope(uid))` (`417`).
  Verify no gdbstub-path dependency before deleting — gdbstub registers no secret, so there is
  none. `self._secret_registry` stays a dependency (the loader registers through it).
- **Docstrings:** rewrite the `start_session` docstring (`sessions_lifecycle.py:286–294`), which
  still describes `ssh_credential_ref` + the secret backend factory, and drop the ADR-0039-§2
  `ssh_credential_ref` narration; the `_resolve_credential` successor docstring must describe the
  bootstrap-key gate/seed. Acceptance criterion 6 requires no source references `ssh_credential_ref`
  on the drgn-live path.

Confirm nothing else on the debug path consumes `secret_backend_factory`. (Remote-libvirt's many
`secret_backend_factory` uses are separate TLS plumbing — untouched.)

The `ssh_credential_ref_missing` reason string is retired; the fail-closed reason is now
`no_bootstrap_key` (existing, emitted by the shared loader).

### 4. Connect-plane error string (`providers/local_libvirt/lifecycle/connect.py:329`)

`_resolved_ssh_port` raises "reprovision with the profile's `ssh_credential_ref` set" when a domain
records no forwarded SSH port. Since the forward now renders on every domain (ADR-0281), that state
means the domain predates the always-render change. Reword to drop the retired field, e.g.
"System …  has no recorded SSH forward; reprovision to obtain one."

### 5. Agent-facing docs and generated references

Remove every "set `ssh_credential_ref`" instruction for drgn-live and state drgn-live needs no
credential provisioning on a ready local System:

- `mcp/resources/_content/toolsets-introspect.md:13`, `toolsets-debug.md:49`,
  `agent-index.md:94,111` (the served resources), the pre-provision checklist served content
  (the doc `test_pre_provision_checklist_docs.py` guards, which lists `ssh_credential_ref` as a
  provision-bound knob), and `docs/guide/toolsets/introspect.md` if it mirrors them.
- `images/capability_signals.py:127` — the `drgn` capability message referencing `ssh_credential_ref`.
- Comments in `providers/local_libvirt/lifecycle/provisioning.py:239` and `xml.py:65,132`.

Regenerate committed generated docs so their gates stay green:

- `just docs` → tool reference (`docs-check` gate).
- config docs (`config-docs-check` gate) — the profile-schema section drops the field.
- `just resources-docs-check` if the served resources are snapshot-guarded.

### 6. Tests

**Acceptance gate (do not rely on an enumerated list):** after the change, a repo-wide
`rg ssh_credential_ref` must show matches **only** in the build-host store — `db/build_hosts.py`,
`db/schema/0027*.sql`, `0029*.sql`, `inventory/reconcile/build_hosts.py`, the `build_hosts.*` tools,
`providers/shared/build_host/**`, and their tests. Any match in a local-libvirt profile context
(source, tests, served docs, generated docs) is a miss. Enumerated touchpoints below are the known
set at spec time, not a substitute for the grep.

- `tests/profiles/test_provisioning.py`: remove/retarget the field tests
  (`test_ssh_credential_ref_defaults_to_none`, `_parses_when_present`,
  `_returns_none_for_provider_without_ssh_credentials`, `_rejects_blank`, and line 757 / 193 policy
  accessors); rename the three `drgn_live_requires_credential` policy tests (774–788) to
  `drgn_live_seeds_bootstrap_key`; add a test that a local-libvirt profile document carrying
  `ssh_credential_ref` is now rejected at parse with `configuration_error`.
- `tests/providers/local_libvirt/test_provisioning.py`: drop the `ssh_credential_ref="guest_key.pem"`
  overrides (389, 473, 920, 967) — the SSH forward already renders unconditionally post-ADR-0281, so
  a default profile exercises the same path — and update the referencing comments (409, 946).
- `tests/integration/test_live_stack.py` (197 + the 79/180/662/665 narration) and
  `tests/integration/test_console_parts_live.py` (132 fixture, 169 error-string assertion): remove
  the profile `ssh_credential_ref` and update the assertion to the reworded connect error. These are
  `live_stack`-gated but their fixtures still `parse` in setup.
- `tests/mcp/resources/test_pre_provision_checklist_docs.py:32`: drop `ssh_credential_ref` from the
  asserted token set (it is removed from the served checklist).
- `tests/mcp/lifecycle/test_recovery_redaction.py:81,97` and `test_recovery_helpers.py:80`: these
  **plant** `provider.local-libvirt.ssh_credential_ref` as a redaction/allowlist marker (raw dicts —
  no parse break, but caught by the grep gate). Their subject is the field being removed. Decision:
  the `local-libvirt` section no longer carries any secret-bearing field, so **repoint**
  `test_system_envelope_excludes_ssh_credential_ref` to plant `kernel_source_ref` (it is the only
  dedicated `system_envelope` assertion — keep it guarding the `system_envelope → summary` wiring),
  and **drop** the redundant `ssh_credential_ref` plant in `test_recovery_helpers.py` whose
  `kernel_source_ref` plant already covers the `provisioning_profile_summary` allowlist directly.
- `tests/mcp/debug/test_debug_tools.py`: replace the ref-resolution start_session tests with:
  - drgn-live `start_session` on a ready local System with **no** `ssh_credential_ref` succeeds;
  - the bootstrap key value is registered in the redaction registry after a drgn-live start
    (observe via `secret_registry.snapshot()` containing the key value — falsifiable);
  - a System with no bootstrap key row fails closed with `configuration_error` /
    `reason="no_bootstrap_key"`;
  - remote / fault-inject drgn-live start needs no bootstrap-key seed (unchanged).
- `tests/providers/local_libvirt/test_connect.py`: update the reworded `_resolved_ssh_port` error
  assertion.
- Doc-snapshot tests (`test_provisioning_for_debugging_docs`, resource docs) update with the guides.

## Acceptance criteria

1. `debug.start_session(transport="drgn-live")` on a ready local System whose profile has **no**
   `ssh_credential_ref` opens the transport and inserts a `live` session — the P6 dead-end is gone.
2. A local-libvirt profile document containing `ssh_credential_ref` is rejected at parse with
   `configuration_error`.
3. After a drgn-live `start_session`, the per-System bootstrap key value is present in the
   redaction registry — falsifiable via `secret_registry.snapshot()` containing the key value (and a
   redactor built from the registry masks it).
4. A drgn-live `start_session` against a System with no bootstrap-key row fails closed with
   `configuration_error` and `reason="no_bootstrap_key"`.
5. remote-libvirt and fault-inject drgn-live start behavior is unchanged (no bootstrap-key
   gate/seed; `drgn_live_seeds_bootstrap_key` is `False`).
6. No source, generated doc, config doc, or agent resource references `ssh_credential_ref` for the
   drgn-live path; the introspect/debug guides state drgn-live needs no credential provisioning.
7. Build-host `ssh_credential_ref` (DB column, transport, tools, tests) is unchanged.
8. Guardrails green: `just lint`, `just type`, `just test`, `just docs-check`,
   `just config-docs-check`, `just resources-docs-check`, `just adr-status-check`.

## Failure modes and edges

- **Stored profile carrying the retired field** → parse `configuration_error` on any op that reads
  it (`start_session`, `runs.get`, reconciler). Accepted pre-release break (criterion 2 makes it
  explicit). Documented in ADR-0315 consequences.
- **No bootstrap key** (System predates ADR-0289 or was never provisioned) → fail closed,
  `no_bootstrap_key` (criterion 4). Same category the old path returned, different reason.
- **drgn absent in-guest** → unchanged: `introspect.run` returns `missing_dependency`. Not gated at
  `start_session` (would duplicate the downstream check and re-add a prerequisite surface).
- **Redaction lifetime** — `load_system_bootstrap_private_key` registers with `scope=None`
  (process-global, retained for the process lifetime, `system_bootstrap_key.py:134`), matching how
  `introspect.run` already seeds the same key today. The old drgn path used a session scope released
  at `end_session`; that session-scoped release machinery now has **no registrant** and is removed as
  dead code (§3), not left in place. Process-global is strictly more conservative for masking (the
  key stays masked), and the key is long-lived and reused across sessions. The only downside — the
  redaction set grows by one entry per distinct System key over the process lifetime and never
  shrinks — is a **pre-existing** property of `introspect.run`'s own seeding, unchanged in magnitude
  by this change (the same key would be registered by the first `introspect.run` anyway); bounding it
  is out of scope for #1037.

## Rollback

No DB migration. Rollback is a branch revert. There is no persisted state change to undo.
