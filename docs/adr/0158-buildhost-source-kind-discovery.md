# ADR 0158 — Surface build-host accepted source kinds at the MCP boundary

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0157](0157-create-time-build-host-source-check.md)
  (the shared `check_source_kind_compatibility` helper this reuses and extends),
  [ADR-0099](0099-remote-build-host-targets.md) (§5 fail-closed `kernel_source_ref`
  matrix; the `build_hosts` inventory), [ADR-0124](0124-provisioning-profile-discoverability.md)
  (the `systems.profile_examples` discovery pattern the new tool mirrors),
  [ADR-0117](0117-projects-list-whoami.md) (the auth-only read posture).
- **Spec:** [`../specs/2026-06-17-buildhost-source-kind-discovery.md`](../specs/2026-06-17-buildhost-source-kind-discovery.md)
- **Issue:** [#536](https://github.com/randomparity/kdive/issues/536)

## Context

ADR-0099 §5 makes `kernel_source_ref` builder-dependent: a `local` build host accepts
a warm-tree string only, an `ssh`/`ephemeral_libvirt` host a git `{git:{remote,ref}}`
ref only. ADR-0157 rejects an incompatible pairing at `runs.create` via the shared
`check_source_kind_compatibility` helper. But the rule is still exposed only as a
rejection: no read tool a caller consults before building advertises which lane a host
requires. `build_hosts.list` returns each host's `kind` but no source-kind, and there
is no build-profile examples tool (`systems.profile_examples` covers provisioning
profiles only, and never shows the warm-tree string form). An agent learns the
convention by trial and error.

ADR-0157 explicitly left this seam: it factored the matrix into one helper precisely
so #536 could read the same mapping instead of re-deriving it.

## Decision

We will advertise each build host's accepted source kind(s) on two read surfaces,
both deriving from one shared mapping function so the advertisement can never drift
from the enforced rule:

1. Add a pure `accepted_source_kinds(host_kind: BuildHostKind) -> tuple[SourceKind, ...]`
   beside `check_source_kind_compatibility` in `services/runs/build_host_selection.py`,
   with a closed `SourceKind` token set (`warm-tree`, `git`). Rewrite
   `check_source_kind_compatibility` to consume it (its error strings, category, and
   details are preserved byte-for-byte, so ADR-0157's behavior is unchanged).
2. `build_hosts.list` gains a derived `supported_source_kinds` field per host
   (`["warm-tree"]` for `local`, `["git"]` for `ssh`/`ephemeral_libvirt`), computed
   from `kind` via `accepted_source_kinds`. No new column, migration, or
   authorization.
3. Add a new read-only, auth-only `runs.profile_examples` tool, sibling to
   `systems.profile_examples` but pool-backed (the build-host inventory is in
   Postgres, not `systems.toml`). It emits one ready-to-edit build profile per
   registered build host: a warm-tree string `kernel_source_ref` for `local` hosts, a
   `{git:{...}}` object for remote hosts, with next-action pointers to
   `runs.create`/`runs.build`. Each emitted profile parses via `BuildProfile.parse`
   and is compatible with its host.

## Consequences

- The host-kind → source-kind matrix has one definition (`accepted_source_kinds`).
  `check_source_kind_compatibility`, `build_hosts.list`, and `runs.profile_examples`
  all consume it; a parameterized test over every `BuildHostKind` pins that the
  validator raises iff the submitted kind is absent from the advertised set, so the
  advertised lane and the enforced lane cannot disagree.
- `build_hosts.list` callers learn the required lane from the same
  `platform_auditor`-gated read they already use; the field is derived, carries no
  secret, and is present on every row including the seeded `worker-local`.
- A cold agent can call `runs.profile_examples`, copy the example for its target host,
  replace the placeholders, and reach `runs.create`/`runs.build` without first hitting
  a rejection. The example is schema-valid as emitted (anti-rot, enforced by a
  parse-and-compat test), but not buildable as-is: the `REPLACE_ME` source placeholder
  must be filled in.
- No schema, migration, or new error category. `runs.profile_examples` reuses the
  existing `runs.*` pool and registrar; `build_hosts.list`'s response gains one
  additive field (a list of strings) — no field is removed or renamed.

## Alternatives considered

- **Re-derive "ssh ⇒ git" inline in `build_hosts.list`.** Smallest diff, but the
  advertisement and the ADR-0157 validator would have independent copies of the
  matrix and could drift — the tool could advertise a lane the validator rejects.
  Rejected for the shared `accepted_source_kinds` function both consume.
- **Store `supported_source_kinds` as a column on `build_hosts`.** Lets an operator
  override per host. Rejected: the rule is a pure function of `kind` (ADR-0099 §5), not
  per-host configuration; a stored copy is a second source of truth that can disagree
  with the validator, and #536 is about surfacing the *existing* rule, not making it
  configurable. Derive, do not store.
- **Extend `systems.profile_examples` to also emit build profiles.** One tool, one
  call. Rejected: `systems.profile_examples` projects the file-based `systems.toml`
  provider inventory (ADR-0124); build profiles key off the Postgres `build_hosts`
  inventory and a different lifecycle (`runs.create`/`runs.build`). Overloading one
  tool across two inventories and two next-action chains muddies both; a sibling
  `runs.profile_examples` keeps each tool's projection and next-actions coherent.
- **Make `runs.profile_examples` inventory/file-backed like its sibling.** Symmetric
  with `systems.profile_examples`. Rejected: build hosts live in Postgres, not
  `systems.toml`; reading the DB is the only way to emit one example per
  *registered* host, which is what the issue asks for.
- **Emit a single generic build-profile example, not one per host.** Less DB work.
  Rejected: the whole point is to show the correct source form *for the host the
  caller will target*; a generic example reintroduces the guesswork (which lane does
  *my* host want?) the issue is removing.
- **Advertise `supported_source_kinds` as a scalar string, not a list.** Simpler
  field. Rejected: a list is forward-compatible with #530 (multiple selectable source
  repos could widen a host's accepted set) and reads naturally as "the kinds this host
  accepts"; the cost is nil today (always one element).
