# Self-service kernel build from a developer-named git URL (#778)

- **Status:** Draft
- **Date:** 2026-06-25
- **Issue:** [#778](https://github.com/randomparity/kdive/issues/778) (re-scoped from a doc-vs-behavior
  bug into the build-source redesign it exposed)
- **ADR:** [ADR-0242](../../adr/0242-self-service-build-from-url.md)
- **Supersedes:** [ADR-0136](../../adr/0136-runs-build-reachability.md) (its decision to add *no*
  create-time URI guard and treat every URI-looking bare string as a warm-tree label).
- **Builds on:** [ADR-0029](../../adr/0029-build-plane-local-make.md) (build profile + parse
  boundary), [ADR-0162](../../adr/0162-local-git-build-lane.md) (the worker-local remote allowlist,
  **unchanged** — it gates only the control-plane-adjacent `worker-local` clone, not the isolated
  build hosts), [ADR-0234](../../adr/0234-external-build-default-and-contributor-role.md)
  (`contributor` role), [ADR-0238](../../adr/0238-build-log-artifact-capture.md) (build-log artifact
  on failure), [ADR-0148](../../adr/0148-rbac-scoped-tool-exposure.md) (RBAC tool exposure),
  [ADR-0123](../../adr/0123-tool-error-detail-surfacing.md) (self-correcting, value-free errors),
  [ADR-0214](../../adr/0214-root-build-privilege-drop.md) (`KDIVE_BUILD_USER` build-subprocess
  demotion).

## Problem

The build-source model assumes **one** kernel source tree: an operator pre-stages a single warm
tree at `$KDIVE_KERNEL_SRC`, and a Run's bare-string `kernel_source_ref` is a *decorative,
unverified label* for it (`is_git_source` dispatches on type, never content; the worker always
`rsync`s from the one staged tree regardless of the label). That is wrong for real kernel work: a
developer routinely needs to build a **specific** tree — a distro tree derived from RHEL SRPMs, a
personal feature branch, mainline — each at a chosen ref, and frequently hosted on an internal
GitHub Enterprise. Three concrete gaps follow:

1. **No build-environment *discovery*.** The structured `{"git": {"remote", "ref"}}` form already
   clones a URL on a registered remote/isolated build host — and the worker-local remote allowlist
   (ADR-0162) gates **only** the `worker-local` lane (`git_source.remote_allowed` /
   `LOCAL_BUILD_REMOTE_ALLOWLIST`; `dispatch.py` applies it solely on the `BuildHostKind.LOCAL`
   branch), so an `ephemeral_libvirt`/`ssh` host clones an arbitrary remote with no allowlist today.
   The blocker is not the gate: a developer cannot **discover** which build host carries which
   toolchain (`build_hosts.list` is `platform_auditor`), so there is no developer-facing path to
   "build this URL at this ref on a fitting env."
2. **No build-environment choice surface.** A RHEL-9 kernel, mainline, and an old 4.x tree need
   different host toolchains (gcc/binutils/flex/bison/libelf). The toolchain lives on an operator-
   registered build host (`base_image_volume`), selectable only by naming a host the developer
   cannot see, with no human description of what it provides.
3. **No provenance.** Because the bare label is never reconciled with the tree actually built, an
   MCP client cannot learn *what* was built. The single-tree assumption made the label "good
   enough"; multi-tree makes it a correctness hole.

A bare string that *looks* like a git URL (`https://ghe.corp/me/linux`) is silently treated as a
warm-tree label and built against the wrong (or missing) tree — the footgun the original #778 filed.

## Decision (direction)

Make **self-service build-from-URL** first class: a `contributor` names a git URL + ref and a
build environment they pick from a discoverable catalog, and the worker clones and builds it on that
environment, with verified provenance and the existing inspectable failure logs — no per-build
operator action. The single warm tree becomes one option among many, not the centre of gravity.

The build environment is a **view over the existing build hosts**, not a new entity: a build host
already binds a toolchain (`base_image_volume`) and a runner; we surface it to developers and let
them select it. This keeps the data model unchanged and — critically — keeps every new seam
**discriminated on a generic `kind` / env *property*, never on a hardcoded VM-vs-host check**, so a
future lighter-weight `container` build environment (explicitly out of scope here) is a drop-in: a
new `BuildHostKind` value plus a transport, with discovery, selection, provenance, and the trust gate
unchanged.

### Components

#### 1. Build-environment discovery — `build_envs.list` (new, `contributor`-readable)

A read-only tool returning a **projection** of registered build hosts:
`{name, kind, toolchain_desc, enabled}`. It deliberately **omits** `address`,
`ssh_credential_ref`, and the raw `base_image_volume` — infra/secret detail that keeps the existing
`build_hosts.list` at `platform_auditor`. The projection is kind-agnostic, so a future `container`
env lists with no change. Exposure: `contributor` (the role that can build, ADR-0234) via the
ADR-0148 exposure map.

#### 2. Toolchain descriptor — `toolchain_desc` on host registration (new optional field)

`build_hosts.register_ssh` / `…_ephemeral_libvirt` gain an optional `toolchain_desc: str` (operator-
asserted prose, e.g. `"gcc11, binutils2.40; suits rhel9/5.14"`) so the catalog is self-describing. It
is operator-asserted, not verified — stated plainly in the tool/doc. Persisted on the build-host row
(the one schema change; see Migration).

#### 3. Build-environment selection — reuse `build_profile.build_host`

The existing `ServerBuildProfile.build_host` names the env. No new field (the container wrinkle does
not need one — `build_host` denotes "the build *executor*"; `kind` carries VM-vs-container). Naming
an unknown or disabled env is a `configuration_error` enumerating the valid env names (self-
correcting, ADR-0123), never echoing the submitted value.

#### 4. Self-service URL build — wire the structured form to a selected env

The structured `{"git": {"remote", "ref"}}` source (already parsed) is the documented, discoverable
path. With an isolated build env selected, the worker clones the remote at the ref and builds in the
env's toolchain image. No new request shape — `kernel_source_ref` (structured) + `build_host`
(an env) already express it.

#### 5. Bare-URL guard (supersedes ADR-0136)

Reject a **bare-string** `kernel_source_ref` that begins with a **recognized git clone-URL scheme**
— the transports git accepts (`https://`, `http://`, `ssh://`, `git://`), the bare `git:` form the
issue calls out, and the common `git+ssh://` ssh alias — at the `ServerBuildProfile` parse boundary,
with a `configuration_error` pointing at the structured `{"git": {...}}` form and `build_envs.list`.
ADR-0136 rejected this guard because a URI-looking bare string was the *established label
convention*; that rationale dissolves once URL builds are first-class, so ADR-0136 is superseded.

**Scoping to bound blast radius.** Two URI-looking shapes are deliberately **spared** because the
existing fixtures lean on them as warm-tree *labels* and rejecting them would churn dozens of
fixtures for little gain: `git+https://` (the entrenched `git+https://git.kernel.org/…#v6.9` label
idiom — and not a transport git clones; git speaks `https://`) and `file://` (a local path form,
rejected as a git remote anyway). The scp-style `git@host:path` is also spared — it carries no scheme
prefix, so it does not match the rule. This pragmatic carve-out (not a purity rule) keeps the
migration small: the fixtures that must move to plain labels are the few using a *rejected* scheme as
a bare string — `git:abc123` (→ `abc123`) and any bare `https://`/`http://`/`ssh://`/`git://`/
`git+ssh://` label.

**Exact reject set** (ordered longest-first so the reported scheme is the descriptive one), matched
against the whitespace-stripped, lowercased value:
`("git+ssh://", "git://", "ssh://", "https://", "http://", "git:")`.
The validator is a `mode="after"` `field_validator` on `ServerBuildProfile.kernel_source_ref`,
skipping any non-`str` (structured) value. The message names only the matched scheme token, never the
submitted value (which may carry a `…@host` credential).

The fixture migration is mechanical and counted in the plan; the full suite must stay green.

#### 6. Build provenance (new, no migration) — both build paths

The resolved commit must be captured on **both** clone paths, and the remote path is the *primary*
one for self-service URL builds (an isolated env builds via the transport, not the local lane):

- **Local lane** (`worker-local`): `clone_tree` already runs `git fetch --depth 1` then
  `rev-parse --verify FETCH_HEAD` (`providers/shared/build_host/workspaces/workspace.py`) — capture
  the verified `FETCH_HEAD` SHA there.
- **Remote/transport lane** (`ephemeral_libvirt` / `ssh`): the clone is
  `ShellTransport.clone(remote, ref, dest)` (`providers/shared/build_host/transports/shell_transport.py`),
  which today `fetch`+`checkout FETCH_HEAD`s but **returns `None`** and surfaces no SHA. Extend the
  transport `clone` seam to run one more `git rev-parse HEAD` on the host after checkout and
  **return the resolved commit**, threading it back through the transport build result so the worker
  records the same provenance for remote builds. Without this the provenance feature would not cover
  its main lane.

Capture `{remote, ref, resolved_commit, build_host}` into the build-step result, where:

- `remote` is **userinfo-stripped** (any `https://…@host/path` userinfo component is dropped, leaving
  `https://host/path`) — an embedded credential never persists;
- `resolved_commit` is the `FETCH_HEAD` SHA (a safe value);
- warm-tree builds record best-effort `{label, resolved_commit?}` — `rev-parse HEAD` of the staged
  tree when it is a git checkout, else `{label}` with no commit;
- **provenance capture never fails the build**: any error degrades to recording what is known.

`runs.get` surfaces it as `data.build_provenance` via the generic free-form envelope (`data` is
schema-free, #565 / the ADR-0239 pattern), so no committed outputSchema/snapshot is invalidated.

#### 7. Trust gate — document the existing isolation shape (no functional change)

The clone+`make` of developer-named source is **build-time** code execution (Kbuild, host tools,
Makefiles/Kconfig run on the build host); the compiled kernel binary is never run on the worker (it
boots in an isolated guest). The gate is **already shaped by isolation** and this spec does **not**
change it — verified against the code:

| Build env | Isolation | Remote allowlist applied today? |
|---|---|---|
| `worker-local` (`BuildHostKind.LOCAL`) | none (shares the control plane) | **yes** — `dispatch.py` LOCAL branch enforces `remote_allowed` (ADR-0162) + `KDIVE_BUILD_USER` demotion |
| `ephemeral_libvirt` | strong (throwaway VM, destroyed post-build) | **no** — routed to the transport, which never consults the allowlist |
| dedicated remote `ssh` | host-level (operator-owned build box) | **no** — same transport path |
| *future* `container` | (declares its level) | (would route by kind) |

So self-service URL builds on an isolated host already clone arbitrary remotes ungated today; the
allowlist guards only the control-plane-adjacent `worker-local` lane. This spec **keeps that exactly
as is** — no relaxation, no supersession of ADR-0162. The only addition is **naming**: expose a
read-only `provides_isolation` property derived from the build-host kind (so the future `container`
kind slots into the same isolation-shaped reasoning) and document the obligation below; the gate
decision stays at the worker/`dispatch.py` boundary keyed on `host.kind`, where `KDIVE_*` worker
config is in scope (the server/admission cannot see it — the ADR-0136 layering constraint).

**Operator obligation (ADR text):** registering an isolated build host (`ephemeral_libvirt`/`ssh`)
already authorizes it to clone arbitrary developer remotes — so an isolated build env must carry no
platform secrets and should constrain egress (the ephemeral build VM must not mount platform
credentials, and an `ssh` build host must be a dedicated build box, not a shared bastion). This is
stated as a registration obligation, not a new gate. **The security-sensitive new surface in this
spec is the discovery exposure widening (component 1), which goes through `security-review`** — not
this gate, which is unchanged.

### Inherited unchanged

Build-log capture on `make`/`olddefconfig` failure (ADR-0238) — Run-owned, redacted, tail-capped
`build-log` artifact surfaced as `refs["build-log"]` on `runs.get` and fetchable via `artifacts.get`
— covers both the local and remote/transport internal-build paths, so the URL lane inherits it.

## Data flow

```
contributor → build_envs.list                       # discover envs (name, kind, toolchain_desc)
            → runs.create(build_profile = {
                  kernel_source_ref: {git: {remote, ref}},
                  build_host: "rhel-9-toolchain"})    # select an env (validated: known + enabled)
runs.build  → worker dispatch.py keys on host.kind:
                LOCAL   → workspace clone_tree (ADR-0162 allowlist applies) → rev-parse FETCH_HEAD
                non-LOCAL → ShellTransport.clone (no allowlist; isolated) → rev-parse HEAD (returned)
            ─► record build_provenance{remote(stripped), ref, resolved_commit, build_host}
            → make in env toolchain image
            → on make failure: build-log artifact (ADR-0238)
runs.get    → data.build_provenance{remote(stripped), ref, resolved_commit, build_host}
            → refs["build-log"] on failure
```

## Migration

One schema change: a nullable `toolchain_desc` column on the build-host table (next free migration
number, assigned at plan time). Additive, no backfill (existing rows have `NULL`, rendered as "no
description" in `build_envs.list`). No change to Run/build_profile persistence (provenance rides the
free-form `data` envelope; `build_host` already exists).

## Error handling

All failures use the existing `configuration_error` envelope, self-correcting and value-free
(ADR-0123):

- unknown/disabled `build_host` → enumerates valid env names;
- bare cloneable-URL `kernel_source_ref` → names the structured form + `build_envs.list`, echoes only
  the matched scheme;
- non-isolated env (`worker-local`, the default when `build_host` is omitted) + non-allowlisted
  remote → the ADR-0162 rejection, **extended** to also name the self-service alternative ("or select
  an isolated build environment from `build_envs.list`") alongside the existing operator-allowlist
  guidance — so the most common URL-build mistake (forgetting to pick an isolated env) surfaces the
  one-field self-service fix instead of steering the developer to an operator action. The allowlist
  guidance stays for the genuinely worker-local-intended case;
- clone failures (bad ref, unreachable remote) → the existing redacted `git fetch` errors;
- `make` failures → ADR-0238 build-log artifact.

## Testing

Boundary-driven units (no `live_vm` for the unit surface):

- **Discovery:** `build_envs.list` projects `{name, kind, toolchain_desc, enabled}`,
  **excludes** `address`/`ssh_credential_ref`/`base_image_volume`, is `contributor`-gated (a
  `viewer` is denied), and lists a `NULL`-descriptor host as "no description".
- **Descriptor:** registration round-trips `toolchain_desc`; omitting it stores `NULL`.
- **Selection:** an unknown/disabled `build_host` → `configuration_error` enumerating valid envs, no
  submitted value leaked.
- **Self-service discoverability:** a git-source build rejected on the non-isolated `worker-local`
  lane (non-allowlisted remote) → the rejection names `build_envs.list` / "select an isolated build
  environment" alongside the operator-allowlist guidance, so the self-service fix is reachable from
  the failure.
- **Bare-URL guard:** each rejected scheme (`git:`, `git://`, `git+ssh://`, `ssh://`, `https://`,
  `http://`, plus an uppercase `HTTPS://`) → `configuration_error` naming the structured form, with a
  **`runs.create` tool-boundary no-leak test** (a planted-token userinfo URL → neither host nor token
  nor a literal `input` key in the serialized envelope, exercising `BindingErrorMiddleware`); spared
  shapes (`file://`, `git+https://…`, scp-style `git@h:p`, plain label) still parse.
- **Trust gate (unchanged behavior, characterization tests):** `dispatch.py` routes a non-LOCAL
  (isolated) host to the transport with **no** allowlist consultation, and the LOCAL branch still
  enforces `remote_allowed` (the ADR-0162 message unchanged); the `provides_isolation` property is
  derived from `host.kind`, asserted via a fake host without a kind literal at the call site
  (container-readiness). These pin existing behavior so a future change can't silently widen the
  worker-local path.
- **Provenance (both paths):** local `clone_tree` records `resolved_commit == FETCH_HEAD`; the
  **remote transport** `clone` returns the resolved `HEAD` and the worker records it (the primary
  lane); `remote` is userinfo-stripped; warm-tree degrades to `{label}`; a capture failure does not
  fail the build; `runs.get` surfaces `data.build_provenance` for a remote build.
- **Fixture migration:** the full suite stays green after URI-looking *cloneable-scheme* labels are
  migrated to plain labels.
- **Integration (gated):** a `live_vm`/`live_stack` end-to-end — build a real GHE-style URL + ref on
  an `ephemeral_libvirt` env, asserting provenance and a booted kernel — is the integration proof,
  gated as usual (`live_vm`).

## Explicitly out of scope (future specs)

- **Container build environments.** Designed *for* (kind-agnostic seams, `provides_isolation`
  property) but not implemented; a future `container` `BuildHostKind` + transport.
- **Operator-curated multi-tree *warm* catalog** (named warm trees beyond the single
  `$KDIVE_KERNEL_SRC`). The URL lane covers the multi-tree need without it; a warm catalog is a
  separate optimization.
- **Per-project remote trust** (delegating clone trust to project owners). The isolation-property
  gate covers the immediate need; per-project trust is a separable RBAC extension.
- **Verifying `toolchain_desc`** against the image contents. Operator-asserted prose now; automated
  verification is a follow-on.
