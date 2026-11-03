# Bound remote release finalize; keep System teardown unbounded (#3475)

## Problem

Remote `finalize_cleanup_quarantine` (release cleanup finalize and the orphan delete) parses
every `.lifecycle-request`, `.request` and `.preparation` file in the provider store
(`_activation_file_groups`, `_owned_operation_nonce`). Its scans have been unbounded since #3419
because no remedy existed past a bound. #3472 and #3486 added that remedy: a completed System
teardown collects the store files of every activation of the System it can prove safe, and the
runbook section "Clear store files of torn-down Systems" clears a drained host.

`teardown_preparation` still refuses past 4096 `.request` names. Teardown is the remedy, so that
bound can strand a host: past it no System teardown reaches `complete`, nothing is collected,
and no operator action short of hand deletion frees names (attempt-1 park note on #3475).

Operator decision (2026-11-03, "bound release only"): bound finalize, unbound teardown.

## Design

### Finalize bound

`RemoteModuleVolumePreparationStore._finalize_cleanup_quarantine` starts with one count, under
the store lock that `finalize_cleanup_quarantine` already holds and after its torn-down no-op
check, before the marker write. It counts directory names of the exact shape the finalize
scans read: 64 lowercase hex characters followed by `.request`, `.lifecycle-request` or
`.preparation`. When the count exceeds 4096 it raises `ProviderRecoveryRefusal` with a fixed
literal and writes nothing. At 4096 or fewer it proceeds unchanged.

Refusal text (one literal, under the 512-character ADR-0707 bound, no interpolation):

> remote preparation store holds more than 4096 request, lifecycle-request and preparation
> records, so finalize is refused; tear down Systems on this host so teardown collects their
> records, or, once every System on this host is torn down, follow the runbook procedure Clear
> store files of torn-down Systems, then retry

Both remedies are true: a completed teardown removes its provable activations' counted files
(#3486), and the runbook procedure removes every counted file. Three kinds of counted file no
teardown removes, and only the second remedy clears: an activation whose volumes were present at
teardown; a System torn down before #3472; and a counted file that does not parse (collection
leaves it with a warning, and the strict scans in `activation_claims` then fail, so
`_prove_siblings` keeps every never-finalized sibling activation on the host). The runbook paragraph says so, so an operator whose
count is held by these does not tear down live Systems for nothing.

A refused release finalize follows the existing failure path: the authority publishes the
cleanup quarantine (ADR-0615) and a later release attempt or orphan delete retries finalize.

The literal is added to `ProviderRecoveryRefusalReason`
(`src/kdive/providers/external_boot_authority/teardown.py`); the retired teardown literal
`"remote preparation store exceeds teardown scan bound"` is removed from it.

### What is counted, and why

- Counted: the three suffixes finalize parses by content. A completed teardown removes them for
  every activation it collects, so the remedy lowers the count.
- Not counted: `.cleanup-finalized` markers and `.teardown` records. Finalize reads a marker by
  its exact name and never scans either by content; each `.teardown` record stays for the life
  of the host, so counting them could make the refusal permanent. Bounding them is excluded.
- Not counted: `.recovery`. Finalize reads its own record by identity; only collection scans
  `.recovery` by content. Orphan `.recovery` collection (F3) is excluded.
- `os.listdir` itself still lists every name; finalize's cost past the count is name-only for
  uncounted files, as #3419 already accepted for `teardown_preparation`.

### Teardown and collection unbounded

`teardown_preparation` drops its 4096 check and its comment. System teardown, its
`system_activations` discovery, `activation_claims`, `_collect` and `_collected_identities`
stay unbounded: they are the remedy, and a bound there recreates the trap. Teardown's cost
grows with the counted files, as finalize's did before this change.

### Records

ADR-0787 gains an append-only amendment (2026-11-03, #3475) that supersedes the #3419
"Accepted limitation: the finalize directory scans stay unbounded" bullet, the #3419 bullet
keeping `teardown_preparation`'s bound, and the 2026-11-02 sentence "The #3419 accepted
limitation on unbounded scans stands until #3475 replaces it", and records why teardown is
unbounded. The runbook section names the release refusal as a trigger for its procedure.
`_scan_owned`'s docstring states which callers are bounded.

## Failure model

1. Actors and deployments: the remote-libvirt external-boot authority service on a provider
   host; the operator reading its log and the runbook. Release jobs and orphan resolution call
   finalize; System teardown calls teardown and collection.
2. Invariants and assets: no store file is removed by the refusal (it writes nothing); System
   teardown is never refused by store size; the refusal text is a fixed literal under the
   ADR-0707 allowlist and names only actions that exist.
3. Accepted failure classes:
   - Teardown and `os.listdir` cost grows with the store: teardown collection parses about
     (activations of the System) x (counted files), since each sibling proof rescans; nothing
     bounds it, and it runs after `complete` and logs instead of raising. Accepted: the
     alternative is the trap.
   - A host whose uncollectable counted files (kept activations, Systems torn down before
     #3472, unparseable files) exceed the bound refuses every release and orphan delete,
     possibly from the first deploy, until it is drained and the runbook applied; each refusal
     publishes a cleanup quarantine. Accepted: the text and the runbook name that path.
   - A stage of another System between the count and the scans can add names: accepted, the
     bound is a cost bound, not a correctness invariant.
4. Covered elsewhere: logging policy for interpolated refusals (#3476); F2 managed re-check, F3
   orphan `.recovery`, marker and `.teardown` growth, local-libvirt store (operator exclusions).

## Validation

- Refusal: a store holding 4097 counted names refuses `adapter.finalize` with the literal text;
  no marker is written and the activation's files remain.
- Boundary: exactly 4096 counted names finalizes.
- Remedy: after `collect_torn_down_activation` (the store call teardown makes) removes the
  padding activation's `.request` files, the same release finalizes. This proves the count drops
  when collection unlinks files; the padding shares the released activation's System, so the
  sequence is not one production reaches, and teardown-then-collect over the bound rests on the
  Teardown test below and the #3486 collection tests.
- Teardown: `_system_teardown` completes and collects on a store holding more than 4096 counted
  names.
- Markers: the existing 4097-marker test keeps passing (markers are uncounted).
- Literal set: `test_remote_refusal_reasons_are_never_raised_as_plain_value_errors` and pyright
  accept the new literal.
- Controlled faults (after commit): replace the `raise` with `pass`; count markers; restore the
  teardown bound; use `>=`. Each turns its named test red by assertion, not by import error.
