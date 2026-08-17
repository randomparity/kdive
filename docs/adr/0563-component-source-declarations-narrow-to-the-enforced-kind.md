# 0563 — Component-source declarations narrow to the kind that is enforced

## Status

Accepted (2026-08-17)

## Context

`ComponentSourceCapabilities.accepted_component_sources` (`components/validation.py`) is a
per-provider map of `ComponentKind` to the set of `ComponentSourceKind` values that provider
accepts for it. All three providers declared it — `local_libvirt`, `remote_libvirt` and
`fault_inject` — and `components/references.py` defines six kinds: `ROOTFS`, `KERNEL`, `INITRD`,
`CONFIG`, `PATCH`, `VMLINUX`.

The map has exactly one consumer, `reject_unsupported_component_source`, and that helper had
exactly one call site in `src/`, with the kind hard-coded (#1942):

```
src/kdive/services/systems/validation.py:147-151, inside validate_profile_for_provider
    reject_unsupported_component_source(capabilities, component_kind=ROOTFS_COMPONENT, ref=rootfs)
```

So `KERNEL`, `VMLINUX`, `CONFIG`, `PATCH` and `INITRD` were declared and never asked about. A
caller could not have tripped the declaration even in principle, because there is no entry point
that carries a ref of those kinds:

- `ProvisioningProfile` carries one component ref, `rootfs`, and `ProfilePolicy` exposes one
  accessor, `rootfs_source()` (`profiles/provisioning.py`).
- `runs.kernel_ref` is written from build output and from upload finalization, never from a caller.
- The server-build MCP tools that once carried config and patch refs were removed in `fde55d70e`,
  and their tables dropped by schema migration `0062_drop_server_build_tables.sql`.
- Every `ComponentRef` consumer left in `src/` resolves a rootfs (or remote's
  `base_image_source`, a `LocalComponentRef`). No resolver reads a KERNEL, VMLINUX, CONFIG, PATCH
  or INITRD ref.

Three consequences followed from that. Provider documentation and the capability surface stated an
acceptance rule the runtime did not apply. ADR-0430's `## Decision` asserted that
`reject_unsupported_component_source` "already keeps the existing `configuration_error` shape … so
an `artifact` or `component-upload` KERNEL/VMLINUX ref is still rejected unchanged" — not true, as
nothing called the helper for those kinds, which makes the runtime behaviour before and after
#1432 identical. And the #1428 parity guard compared the two providers' maps entry by entry, so for
five of six kinds "parity" meant the two files agreed, not that the two providers behaved alike.

## Decision

Declare only the kinds that are enforced. `accepted_component_sources` becomes `ROOTFS` alone in all
three providers, in lockstep:

- `local_libvirt`: `ROOTFS: {catalog, local}` (unchanged).
- `remote_libvirt`: `ROOTFS: {local}` (unchanged; ADR-0440, #1433).
- `fault_inject`: `ROOTFS: {catalog, local}` (unchanged).

`ROOTFS` enforcement is untouched: same call site, same helper, same `configuration_error` shape
(`provider` / `component_kind` / `source_kind` / `accepted_source_kinds`). Nothing else about the
helper, the six-member `ComponentKind` enum, or the `ComponentRef` union changes — the enum still
names the roles the build and install planes talk about; only the *capability declaration* narrows.

Removing an entry is not a behaviour change. `reject_unsupported_component_source` treats an absent
kind as accepting nothing, so a ref of a narrowed kind is rejected with
`accepted_source_kinds: []` rather than being silently admitted — and no code path passes one, so
the rejection is unreachable in both the old shape and the new one. What changes is only what the
provider claims.

Extend the #1428 parity guard (`tests/providers/test_capability_parity.py`) so the defect class
cannot recur. It parses `src/kdive/` with `ast` and collects the `component_kind` argument of every
`reject_unsupported_component_source` call, then fails when any provider declares a kind no call
site names. Enforcement is a property of the call graph, so the guard reads the call graph rather
than importing and calling: a call site naming the kind is exactly what the removed declarations
lacked. The guard fails closed in the directions that matter — a call site whose `component_kind` is
not a resolvable member is reported rather than skipped, and a rename of the helper empties the
enforced set and reddens two guards rather than passing vacuously. Both halves are asserted: the
inert set must be empty, and `ROOTFS` must still be declared by all three providers and still be
enforced.

The guard proves that a call site naming the kind exists, not that it is reachable from a request, so
a call in dead code would satisfy it. That weaker claim is deliberate: deciding reachability needs a
whole-program analysis, and the defect here was the absence of any call site at all. The per-kind
behaviour tests beside each provider carry the stronger property.

Re-declaring a kind is therefore a three-part change — the caller entry point, the enforcement call
site, and the declaration — in one commit. The guard is what makes that ordering mandatory.

## Consequences

- The capability surface stops advertising an acceptance rule the runtime does not apply. An
  operator or agent reading a provider's declaration now reads something enforced.
- ADR-0430 is superseded by this record. Its decision was to add `KERNEL: {local}` and
  `VMLINUX: {local}` to remote-libvirt's map; those entries are removed here, so its decision no
  longer stands. Its `## Decision` and `## Consequences` carry amendments naming the two claims
  this record contradicts: the rejection shape that was never reached, and the consequence "a
  remote caller can supply an already-built `vmlinuz+modules` bundle from a worker-host path" —
  which has no runtime path, since `RemoteLibvirtInstall.install()` installs `run.kernel_ref` and
  that column is written from build output. **Whether #1432 should be reopened is not decided
  here**: the capability it asked for was declared but never made reachable, and reopening it is a
  product call about whether a caller-supplied kernel is wanted at all. Filed as a follow-up on
  #1942 rather than settled by an implementer.
- **#1436 is unblocked, and its answer is now the narrow one.** Its acceptance criteria wanted a
  provision-time `CONFIGURATION_ERROR` for a supplied INITRD. There is no INITRD entry point to
  supply one through, so the honest outcome is a declaration that promises nothing, which is what
  this record establishes. Real per-kind enforcement is a follow-up on #1942, to land with the
  first caller entry point (#1436 / epic #1423).
- The `support.component_sources.initrd:local` waiver in the parity guard is removed: local no
  longer declares INITRD, so remote's silence is no longer a gap and the stale-waiver guard would
  have reddened on it. The remote↔local `rootfs:catalog` waiver stands.
- ADR-0096 §3 ("add `catalog` to `CONFIG_COMPONENT` in both providers' component-source
  declarations and teach both `_resolve_config_ref` functions") no longer has any implementation in
  the tree. It had none before this change either — `_resolve_config_ref` does not exist at HEAD,
  removed with the server-build plane in `fde55d70e` — so the `CONFIG_COMPONENT` declaration was
  its last visible trace, and removing it makes the drift legible instead of hidden. Reconciling
  ADR-0096 with the tree is out of scope here and is named in the follow-up.
- The parity guard now depends on the *name* `reject_unsupported_component_source` and on
  `component_kind` being passed as a member rather than computed. Both are asserted by the guard
  itself, so the coupling fails loudly instead of degrading into a guard that checks nothing.
- Parsing every file under `src/kdive/` costs one `ast.parse` per module. Measured at 0.38 s for 711
  files, run once per test that needs it — three of them, so about 1.1 s added to the default
  `just test` gate, against a suite that takes over two minutes. Left uncached deliberately: a cache
  on a function returning a mutable set is a footgun worth more than the second it saves. It needs no
  libvirt host (ADR-0076).

## Considered & rejected

- **Enforce the five kinds instead of narrowing (the other half of #1942's fork).** Call
  `reject_unsupported_component_source` for each non-ROOTFS kind at the point its ref enters a
  System or Run, which is the option that would make ADR-0430's rejection-shape claim true.
  Rejected because there is no such point: no profile field, tool input, or Run column carries a
  caller-supplied ref of those kinds, so the call site would have to be invented along with the
  entry point that reaches it — designing a caller-supplied kernel, config and patch surface, which
  is a feature (#1436, epic #1423) and not a defect fix. Writing enforcement first would add a
  guard over an input nobody can send, which is the same unreachable-code defect in a new place.
- **Leave the map alone and only extend the parity guard to report the gap as a warning.** Cheapest
  change, and it keeps the declarations available for whoever wires enforcement later. Rejected:
  a warning nothing fails on is how the five inert entries survived from ADR-0430 through #1428 to
  #1942 in the first place, and the declarations are wrong in the meantime.
- **Delete the unused `ComponentKind` members along with the declarations.** Tempting as dead-code
  removal, but the kinds are not dead — `KERNEL` and `VMLINUX` name real artifacts the install and
  introspect planes handle, and `provider_components` records a component's kind. Only the
  *capability declaration* was inert. Rejected as over-reach that would force the enum back on the
  first entry point.
- **Keep the entries and document them as aspirational in a comment.** Rejected: a comment saying
  a declaration is not enforced leaves two sources of truth for one question, and the map is read
  by the parity guard and by anyone auditing provider capabilities, neither of which reads comments.
- **Narrow only `remote_libvirt`, since ADR-0430 is the record that made the false claim.** Rejected
  as leaving the same defect in two providers, and it would redden the parity guard's local-only-gap
  check for four kinds at once, forcing four waivers for a difference that is not deliberate.
