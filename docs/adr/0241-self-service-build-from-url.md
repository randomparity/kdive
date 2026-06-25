# ADR 0241 — Self-service kernel build from a developer-named git URL (#778)

- **Status:** Accepted
- **Date:** 2026-06-25
- **Deciders:** kdive maintainers
- **Supersedes:** [ADR-0136](0136-runs-build-reachability.md) (its decision to add *no* create-time
  URI guard and to treat every URI-looking bare string as a warm-tree label) and
  [ADR-0162](0162-local-git-build-lane.md) (its global deny-by-default remote allowlist as the only
  gate on a git-clone build).
- **Builds on (does not supersede):** [ADR-0029](0029-build-plane-local-make.md) (build profile +
  parse boundary), [ADR-0234](0234-external-build-default-and-contributor-role.md) (`contributor`
  role), [ADR-0238](0238-build-log-artifact-capture.md) (build-log artifact on failure),
  [ADR-0148](0148-rbac-scoped-tool-exposure.md) (RBAC tool exposure), [ADR-0123](0123-tool-error-detail-surfacing.md)
  (self-correcting, value-free errors), [ADR-0214](0214-root-build-privilege-drop.md)
  (`KDIVE_BUILD_USER` demotion).
- **Issue:** [#778](https://github.com/randomparity/kdive/issues/778) (re-scoped from a doc bug).
- **Spec:** [`../superpowers/specs/2026-06-25-self-service-build-from-url-778.md`](../superpowers/specs/2026-06-25-self-service-build-from-url-778.md).

## Context

The build-source model assumes one kernel source tree: an operator pre-stages a single warm tree at
`$KDIVE_KERNEL_SRC`, and a Run's bare-string `kernel_source_ref` is a decorative, unverified label
for it (the worker always builds from the one staged tree regardless of the label). Real kernel work
is multi-tree: a developer builds a distro tree derived from RHEL SRPMs, a personal feature branch,
or mainline — each at a chosen ref, often on an internal GitHub Enterprise. ADR-0136 leaned on the
single-tree assumption to keep the bare label "good enough" and to reject any create-time URI guard
as breaking the bare-label convention; ADR-0162 gated the only URL path (the structured
`{"git": {...}}` form) behind a global operator allowlist. Neither gives a developer a self-service
"build this URL at this ref on a toolchain that fits it" path, and the unverified label means an MCP
client cannot learn what was actually built.

The clone+`make` of developer-named source is **build-time** code execution (Kbuild, host tools,
Makefiles run on the build host); the compiled kernel binary is never executed on the worker — it
boots in an isolated guest. The relevant risk axis is therefore *whose code, on how-isolated an
environment*, not a global URL allowlist.

## Decision

Make self-service build-from-URL first class. A `contributor` names a git URL + ref (the structured
`{"git": {...}}` source) and selects a **build environment** from a discoverable catalog; the worker
clones and builds it on that environment with verified provenance and the existing inspectable
failure logs (ADR-0238), with no per-build operator action.

A build environment is a **view over the existing build hosts** (a host already binds a toolchain
`base_image_volume` and a runner), not a new entity. Every new seam discriminates on a generic
`kind` / env *property*, never a hardcoded VM-vs-host check, so a future lighter `container` build
environment (out of scope here) is a drop-in: a new `BuildHostKind` value + transport, with
discovery, selection, provenance, and the trust gate unchanged.

1. **`build_envs.list`** (new, `contributor`-readable): a projection of registered build hosts —
   `{name, kind, toolchain_desc, enabled}` — omitting `address`/`ssh_credential_ref`/raw
   `base_image_volume` (the infra/secret detail that keeps `build_hosts.list` at `platform_auditor`).
2. **`toolchain_desc`** (new optional registration field, persisted on the build-host row): operator-
   asserted prose so the catalog is self-describing (not verified against image contents).
3. **Selection reuses `build_profile.build_host`** to name the env (no new field); an unknown/disabled
   env is a `configuration_error` enumerating valid env names.
4. **Bare-URL guard (supersedes ADR-0136):** a bare-string `kernel_source_ref` beginning with a
   recognized git clone-URL scheme — `git+ssh://`, `git://`, `ssh://`, `https://`, `http://`, `git:`
   — is rejected at the `ServerBuildProfile` parse boundary, pointing at the structured form +
   `build_envs.list`; the message names only the matched scheme, never the value. Two URI-looking
   shapes the fixtures lean on as labels are deliberately spared to bound migration churn:
   `git+https://` (the entrenched kernel.org label idiom, not a transport git clones) and `file://`
   (a local path, rejected as a git remote anyway); scp-style `git@h:p` has no scheme and is unmatched.
   The few fixtures using a rejected scheme as a bare label migrate to plain labels.
5. **Build provenance (no migration):** at `clone_tree` (which already runs `rev-parse FETCH_HEAD`)
   record `{remote (userinfo-stripped), ref, resolved_commit, build_host}`, surfaced on `runs.get` as
   `data.build_provenance` via the free-form `data` envelope; warm-tree records best-effort
   `{label, resolved_commit?}`; capture failure degrades and never fails the build.
6. **Trust gate keyed on isolation (supersedes ADR-0162):** clone any remote when
   `env.provides_isolation` (an `ephemeral_libvirt` throwaway VM, a dedicated remote `ssh` host, or a
   future `container` at its declared level); keep the ADR-0162 allowlist + `KDIVE_BUILD_USER`
   demotion only for the non-isolated `worker-local` lane (which shares the control plane).
   `provides_isolation` is a resolved env property, not a kind literal at the call site.

One additive schema change (nullable `toolchain_desc` column, no backfill). The trust-gate change
goes through `security-review` before ship.

## Consequences

- A `contributor` discovers build environments (`build_envs.list`), names a git URL + ref, picks a
  toolchain env, and builds it self-service — including an internal GitHub Enterprise URL on an
  isolated `ephemeral_libvirt` env with no operator allowlist step.
- `runs.get` reports the exact remote + resolved commit + env that produced a build, replacing the
  decorative-label hole with verified provenance.
- The control-plane-adjacent `worker-local` lane is unchanged (allowlist + demotion retained); the
  allowlist is dropped only where the env isolates the build, with an explicit operator obligation
  that an isolated env carries no platform secrets and constrains egress.
- The bare-URL footgun the original #778 filed is closed coherently — now that URL builds are first
  class, a bare cloneable-URL string is a recognizable mistake, not the established convention.
- Discovery, selection, provenance, and the trust gate are kind-agnostic, so a future `container`
  build environment lands without touching them.
- Cost: a new read tool, a new optional field + migration, a parse-boundary validator, the
  isolation-aware gate, and a mechanical migration of the cloneable-scheme URI-looking fixtures to
  plain labels.

## Considered & rejected

- **Correct the doc only / keep ADR-0136 as-is** (the original #778 Option 2, and ADR-0136's stance).
  Rejected — it entrenches the single-tree assumption and the unverified label, and leaves the
  developer with no self-service URL path; the operator chose to build the capability the model was
  missing.
- **A new first-class `build_env` catalog entity** (table + `build_envs.register` + a `build_env`
  profile field). Rejected — a build host already binds a toolchain image and a runner; a parallel
  entity duplicates that binding and adds a table/tools/RBAC for no capability the view-over-hosts
  projection lacks.
- **Rename `build_profile.build_host` → `build_env`** now for the container future. Rejected as a
  field + fixture migration for cosmetics; `build_host` reads as "the build *executor*" and the
  `kind` discriminator carries VM-vs-container. Reopenable if a clearer name proves worth the churn.
- **Allow-by-default cloning with sandbox-only isolation everywhere** (drop the allowlist even on
  `worker-local`). Rejected — `worker-local` shares the control plane; demotion alone is a weaker
  boundary than a throwaway VM, so the allowlist stays there.
- **Developer-supplied build images** (developer references an arbitrary toolchain image). Rejected
  for now — adds image-provenance/trust and pull surface; the operator-curated env catalog covers the
  toolchain-choice need. A future spec.
- **Infer the toolchain from the tree.** Rejected as primary — kernel trees do not reliably declare
  host-toolchain needs, so mis-detection yields cryptic build failures; explicit env selection is the
  contract.
- **Auto-coerce a bare URI string into a `GitKernelSource`.** Rejected (as in ADR-0136) — the parse
  from a bare URI to `{remote, ref}` is ambiguous and silently rewriting a caller's provenance is the
  surprise class this issue is about; reject-and-point is the honest path.
- **Container build environments in this spec.** Designed *for* (kind-agnostic seams,
  `provides_isolation` property) but deferred to keep this change shippable and the trust-gate
  security review focused.
