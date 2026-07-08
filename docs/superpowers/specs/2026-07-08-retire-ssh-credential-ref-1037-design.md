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
  `drgn_live_uses_bootstrap_key(profile) -> bool` — "drgn-live is realized over the loopback SSH
  forward and authenticates with the per-System bootstrap key, so `start_session` must gate on the
  key's presence and seed redaction from it."
  - local-libvirt → `True`
  - remote-libvirt → `False` (guest-agent realization, no bootstrap key)
  - fault-inject → `False`

The boolean shape and provider values are identical to the method it replaces; only the name and
its meaning change. `Protocol` + all three adapters update together.

### 3. Session start (`mcp/tools/debug/sessions_lifecycle.py`)

Replace `_resolve_credential` (ref-based) with a bootstrap-key seed for drgn-live. In
`_prepare_attach_request`, after the transport-support check, for a drgn-live transport where
`profile_policy.drgn_live_uses_bootstrap_key(profile)` is true:

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

Remove the now-dead credential plumbing on the debug-session path only:

- `_credential_backend`, `secret_backend_factory` param/attr on `DebugSessionLifecycle`
  (`sessions_lifecycle.py:245,254,265,273,371–376`), and the `_secret_backend_factory` builder +
  its wiring in `sessions.py:82–121`.
- Confirm nothing else on the debug path consumes `secret_backend_factory`. (Remote-libvirt's many
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

- `mcp/resources/_content/toolsets-introspect.md`, `toolsets-debug.md`, `agent-index.md`
  (the served resources) and `docs/guide/toolsets/introspect.md` if it mirrors them.
- `images/capability_signals.py:127` — the `drgn` capability message referencing `ssh_credential_ref`.
- Comments in `providers/local_libvirt/lifecycle/provisioning.py:239` and `xml.py:65,132`.

Regenerate committed generated docs so their gates stay green:

- `just docs` → tool reference (`docs-check` gate).
- config docs (`config-docs-check` gate) — the profile-schema section drops the field.
- `just resources-docs-check` if the served resources are snapshot-guarded.

### 6. Tests

- `tests/profiles/test_provisioning.py`: drop the field-presence test; rename/retarget the three
  `drgn_live_requires_credential` policy tests to `drgn_live_uses_bootstrap_key`; add a test that a
  local-libvirt profile document carrying `ssh_credential_ref` is now rejected at parse.
- `tests/mcp/debug/test_debug_tools.py`: replace the ref-resolution start_session tests with:
  - drgn-live `start_session` on a ready local System with **no** `ssh_credential_ref` succeeds;
  - the bootstrap key value is registered in the redaction registry after a drgn-live start;
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
   redaction registry (masked from logs/responses).
4. A drgn-live `start_session` against a System with no bootstrap-key row fails closed with
   `configuration_error` and `reason="no_bootstrap_key"`.
5. remote-libvirt and fault-inject drgn-live start behavior is unchanged (no bootstrap-key
   gate/seed; `drgn_live_uses_bootstrap_key` is `False`).
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
  (process-global, retained for the process lifetime), matching how `introspect.run` already seeds
  the same key. The old drgn path used a session scope released at `end_session`; process-global is
  strictly more conservative (the key stays masked) and the key is long-lived and reused across
  sessions anyway. Noted as an intentional consequence, not a leak.

## Rollback

No DB migration. Rollback is a branch revert. There is no persisted state change to undo.
