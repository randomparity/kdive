# 0588 — Remote module volume ownership lives in the volume name

## Status

Accepted (2026-09-02)

## Context

ADR-0585 requires the reaper to delete "only attempt-scoped staging volumes whose durable owner
and digest match", and to apply "the same identity checks after worker death". It never names the
channel that carries those durable identities. #2129 implemented the channel as a libvirt storage
volume `<metadata>` element. That element does not exist.

Reproduced against libvirt 12.0.0 and libvirt-python 12.0.0 on x86_64 (2026-09-02):

- `dir(libvirt.virStorageVol)` contains no metadata accessor. `virDomain` carries both `metadata`
  and `setMetadata`. There is no storage-volume counterpart to `virDomainSetMetadata`.
- `/usr/share/libvirt/schemas/storagevol.rng` defines no `metadata` element;
  `/usr/share/libvirt/schemas/domaincommon.rng` defines one.
- `virStorageVolCreateXML` accepts a `<metadata>` child without error, and `XMLDesc(0)` on the
  created volume returns only `name`, `key`, `capacity`, `allocation`, `physical`, and `target`.
  The submitted element is discarded silently.

So every ownership readback fails closed. No module attempt can be validated, deleted, or reaped,
and recovery is inert.

The same channel is used three times, not once, and a decision that settles only the first leaves
the other two failing the same way:

- the attempt volumes, `urn:kdive:remote-module-volume:v1`, read back in
  `remote_module_volumes.py` and again in `remote_module_operation.py`;
- the reap-marker journal volumes, `urn:kdive:remote-module-reap:v1`
  (`remote_module_operation.py`), whose *entire* payload is an `attempt-reap` element in the
  discarded `<metadata>` — so a journal volume today carries nothing but its name, and the five
  call sites that read terminal evidence out of it are already inert on the branch;
- the remote boot-artifact volumes on `main`, which #2158 tracks separately.

Reap needs two facts about a volume whose creating worker is gone. Whether the volume is
unambiguously KDIVE attempt-scoped storage — deleting a foreign volume is operator data loss —
and whether anything still needs it. Only the first has to come out of the volume. KDIVE's state
of record is Postgres, and the reconciler already reads a durable owner set from it (ADR-0021);
`reap_orphaned_boot_artifacts` already takes one as `live_owners`.

The second fact is not "is the attempt running". ADR-0585 retains a scratch volume "until recovery
or successful baseline commitment makes it unnecessary", and retains it "for diagnosis" when
restoration fails — both long after the writing attempt has ended.

## Decision

The durable ownership channel for remote module volumes is **the volume name**. Libvirt persists
a volume's name because the name is the object's identity, and it is written by the same
`virStorageVolCreateXML` call that creates the volume, so no window exists in which the volume
is present and its ownership is not.

### The name carries the whole owner tuple

The provider already renders the complete owner tuple into the name. That shape is kept and given
a parser, so the change is a recognition rule rather than a new naming scheme:

```text
kdive-module-<system-uuid>-<run-uuid>-<operation-nonce>-<kind>
```

`system-uuid` and `run-uuid` are canonical lowercase UUIDs, `operation-nonce` is the 32 lowercase
hex characters `RemoteModuleOperation` already validates, and `kind` is one of `source.ext4`,
`scratch.ext4`, `reaping.journal`, or `reaped.journal`. All four are shapes the provider already
renders; the grammar covers them because a kind it omits is a volume the sweep classifies as
foreign and leaks forever. The longest name the grammar can render is 135 bytes —
`13 + 36 + 1 + 36 + 1 + 32 + 1 + 15`, the last term being `reaping.journal`. Measured on the same
host, a dir-pool volume name round-trips byte-identically up to 255 bytes and is refused at 256 —
the filesystem `NAME_MAX` a dir pool inherits, surfacing *after* a clean parse as libvirt error
code 38, `File name too long`, from the `open()` the pool performs, and not as a parse refusal
from libvirt's own name grammar (which would be code 8) — so the grammar holds 120 bytes of
headroom. A test for the 256-byte case asserts the host failure class, not an invalid-argument
one.

Recognition is a single anchored parse of the whole name. A name that does not match is a foreign
volume: never read further, never deleted, never counted. There is no prefix match, no partial
credit, and no fallback to another channel. The surviving prefix match in `inventory()`
(`remote_module_operation.py`) is replaced by this parse.

The content digest is deliberately absent from the name. It is not known when the name must be
chosen, it is not needed to decide ownership, and ADR-0585 already re-verifies content against the
manifests it stores.

### Ownership recovery when the writing worker is gone

The reaper is a reconciler sweep (ADR-0021) with the durable store in reach. Its reads are ordered:

1. `pool.refresh(0)` and enumerate the pool, parsing each name under the grammar. No match ends
   that volume's evaluation — it is not ours.
2. A match *is* the recovery: `(kind, system_id, run_id, operation_nonce)` comes off the name
   itself, so nothing has to be reconstructed from a dead worker's memory, from console output, or
   from the volume's bytes.
3. **Then** read the durable retained-owner set, and only then. Reading it before the enumeration
   would let a volume created after the read be judged against a set that predates it, which
   deletes a live worker's storage; see *Durable intent precedes the volume*.
4. An owner in the retained set is left alone. An owner absent from it has no outstanding claim on
   the volume, so it is a candidate for deletion.
5. Then resolve the referenced-path set — the backing paths of every disk in every active and
   inactive domain definition on the host — and refuse to delete any candidate whose path is in
   it, reporting it as a conflict. Resolve it after the candidates are known and immediately
   before the deletions, so the window in which a domain can be defined against a candidate is as
   short as the sweep can make it. This is the whole-pool form of ADR-0585's exclusivity
   requirement, and it is new code: `inspect_module_attachments` does the attempt-scoped form, and
   `protected_volume_paths` only maps caller-supplied names to paths.
6. Delete. `VIR_ERR_NO_STORAGE_VOL` is an achieved post-state, so the sweep is idempotent.

### What "retained" means

The retained-owner set is the set of attempts with an **un-discharged durable obligation**, not
the set of attempts currently running. ADR-0585 remains in force, and it requires a scratch volume
to outlive its writing attempt: it is the recovery point that holds the only copy of the System's
prior `lib/modules/<release>` tree, retained "until recovery or successful baseline commitment
makes it unnecessary" and retained "for diagnosis" when restoration fails closed. A sweep keyed on
"is the attempt running" deletes exactly that volume on the ordinary path — the attempt completes,
the sweep runs, restoration has not happened yet — and ADR-0585's only remaining escape is System
teardown.

There are two obligations, both keyed on the attempt tuple `(system_id, run_id,
operation_nonce)` and never on the volume kind, because an obligation is a property of the attempt
and one attempt owns several volumes:

- **The mutation obligation** covers `source.ext4` and `scratch.ext4`. It opens before the first
  volume is created and discharges at ADR-0585's durable `restored`, at baseline commitment, or on
  ADR-0585's terminal escape — System teardown, or an operator-acknowledged close of a parked
  recovery conflict. The third event is what stops a dead or failed-closed attempt from holding
  10 GiB forever: without it the first two are unreachable for a worker killed mid-mutation, for a
  restoration that failed closed, and for a parked conflict, and the design would recover
  ownership while never reclaiming anything. The teardown and conflict-close paths must clear the
  durable row; that is a requirement this record places on them.
- **The reap obligation** covers `reaping.journal` and `reaped.journal`. It cannot follow the
  mutation obligation's rule, because the journal volumes are created strictly *after* `restored`
  — the branch's `_validate_terminal_evidence` refuses to build a marker unless the terminal
  result is a successful restore — so the mutation obligation is already discharged at the instant
  the first journal volume exists. Under one shared rule the sweep would delete the marker the
  crash-resume path calls authoritative, and an attempt that crashed after its scratch volume was
  deleted would have neither. So the reap obligation opens when the reap sequence starts and
  discharges when that sequence reaches its own durable terminal state.

### Durable intent precedes the volume

A worker writes the durable row naming the attempt tuple `(system_id, run_id, operation_nonce)`
**before** it creates any of that attempt's volumes. Together with step 3's read ordering this
closes the race in both directions: a volume cannot exist whose attempt has no row, and the sweep
never judges a volume against a set older than the volume. A row with no volume is benign and the
ordinary crash residue; the attempt path already reconciles it.

Without both halves the sweep deletes a live worker's volume. Writing the volume first opens the
window between `createXML` and the durable write; reading the owner set first opens the window
between that read and the enumeration. The metadata element this record replaces had the first
window and never addressed it; the second is a property of the sweep, not of the channel.

### Test doubles model what libvirt discards

`just ci` passed in full, 15,304 tests, against a design that cannot work in production, because
the `Pool.createXML` double echoed the submitted XML back verbatim. A double that accepts
everything asserts nothing.

So the storage double stops echoing. It parses the submitted XML, retains the whole of what
libvirt persists for a dir-pool volume, and renders `XMLDesc` from that retained state, dropping
everything else exactly as libvirt drops it. The modelled set is the whole observed readback and
not the subset today's code reads, because a subset is this same defect one level down. The design
spec fixes the field list.

Because the double is now load-bearing, it owes a fidelity test of its own in the `live_vm` tier,
comparing a real dir-pool readback against the double's for the same submitted XML.

A wrong implementation is therefore caught as follows. An implementation that puts ownership
anywhere libvirt does not persist gets nothing back from the double, so its reap tests fail on an
unrecoverable owner rather than passing on an echo. A double that drifts from libvirt fails the
fidelity test on a host with libvirt. A name grammar that overruns `NAME_MAX`, omits a kind the
provider renders, or admits a foreign shape fails the budget, coverage, and negative tests. A
sweep that keys retention on the running attempt rather than the un-discharged obligation deletes
a completed-but-unrestored attempt's scratch volume, and the test for that case is the one that
fails.

## Consequences

- Ownership is inspectable with `virsh vol-list` and needs no XML, no volume read, and no second
  object. An operator reading a pool listing can see which System and Run owns each volume.
- The channel binds the provider to pool types whose volume names persist at least 135 bytes — the
  grammar's longest rendered name, with no margin claimed beyond it. `dir` is the pool type the
  remote-libvirt runbook provisions and the only one supported today
  (`docs/operating/runbooks/remote-libvirt-host-setup.md`).
  Adding a pool type with a tighter name limit — LVM logical volumes are the realistic case —
  requires revisiting this record rather than silently truncating a name.
- A scratch volume is reclaimable only after ADR-0585's `restored`, baseline commitment, or
  terminal escape, so recovery material accumulates for as long as those obligations stand.
  ADR-0585 already accounts 10 GiB per in-flight external boot; this record makes explicit that
  the accounting is keyed on the obligation rather than on the attempt, and that teardown and
  conflict-close now carry a durable-row clearing step they did not have before.
- Deleting the `attempt-reap` element leaves the reap-marker journal volumes carrying nothing but
  their names, which is all they carried in production anyway. The attributes that element held
  have to come from the durable store instead. That is work #2129 owns, and it has to land before
  the element is removed — deleting the code is safe only once its replacement exists, even
  though the element itself never persisted.
- The owner tuple is now part of a name, and a name is not versionable in place. The
  `kdive-module-` prefix and the fixed field order are a compatibility surface: changing either
  strands volumes created by an earlier worker, which then read as foreign and are never reaped.
  A future change of shape has to sweep the old shape before retiring its parser.
- Intent-before-volume ordering adds one durable write to the front of every module attempt, and
  makes a row with no volume an expected state the attempt path must tolerate.
- The general rule the test-double defect yields: a double standing in for an external system must
  model what that system **discards**, not only what it accepts. A double that only accepts is
  incapable of failing the design it is meant to check.
- #2158 tracks migrating the remote boot-artifact path onto this channel; until it lands, the two
  remote reap paths use different ownership channels.

## Considered & rejected

- **Keep the volume `<metadata>` element.** verified: on libvirt 12.0.0 / libvirt-python 12.0.0,
  `virStorageVolCreateXML` accepts a `<metadata>` child and the created volume's `XMLDesc(0)`
  returns only `name`, `key`, `capacity`, `allocation`, `physical`, and `target`;
  `storagevol.rng` defines no such element and `virStorageVol` exposes no metadata accessor.
  There is nothing to keep.
- **An in-volume header.** verified: `prepare_attempt_volumes`
  (`remote_module_volumes.py`) creates each volume with `createXML` and only then streams its
  contents, so a header is written in a second operation and is absent whenever the worker died
  between the two — precisely the case the channel exists for. Cost is not the objection: the
  provider already streams a pool volume's bytes in `_inspect_remote_source`, so the read is one
  it performs today.
- **A sidecar journal volume.** verified: the branch already has one. `_reap_marker_name`
  (`remote_module_operation.py`) creates `…-reaping.journal` and `…-reaped.journal` volumes whose
  whole payload is an `attempt-reap` element in `<metadata>`, read back in three places. Libvirt
  discards that element, so the journal channel is as inert as the ownership channel it was meant
  to support, and what survived is the journal's name — this decision, reached the expensive way.
  Creating a volume and appending its journal entry are also two operations, so a crash between
  them leaves an unowned volume; and the journal's own orphaning needs a name convention anyway.
- **The durable row records the created volume name; recognition is set membership.** judgment:
  this is a real simplification — it removes the grammar, its parser, the `NAME_MAX` budget, the
  fixed-field-order compatibility surface, and the forged-name path — and the sweep reads the
  store either way, so store reachability is not the ground that separates them. What separates
  them is what happens when a *row* is lost or was never written. Under set membership a volume
  with no row is unrecognisable: it cannot be told from an operator's, so it can never be
  reclaimed and never be safely deleted. Under a name-encoded tuple the same volume still says
  what it is, and the sweep can retain or reclaim it on its own terms. Reap is the path that runs
  after something has already gone wrong, so the channel that keeps working with less state
  intact wins.
- **A libvirt domain as a metadata carrier.** verified: domains do persist `<metadata>` —
  `domaincommon.rng` defines it and `virDomain.setMetadata` exists — so the channel is real. It
  carries the same two-operation crash window as the index volume, and it makes an inert domain
  definition per attempt on a host where `reaping/domains.py` already sweeps kdive-named domains,
  so the carrier has to be excluded from that sweep by name. Ownership by name, without the
  domain.
- **A match key in the name plus a durable-store lookup for the rest of the tuple.** verified:
  this was the shape #2157 assumed the name channel would take, on the premise that a name can
  carry only a match key. A 255-byte dir-pool name holds the 135-byte full tuple with 120 bytes
  spare — measured on the host above — and the provider's existing names already carry it, so the
  premise does not hold and the reduced form buys nothing.
- **A reap grace window on the volume's timestamps.** verified: a dir-pool volume readback does
  expose `<target><timestamps>` with `mtime` and `ctime`, so a window is implementable. It is the
  remote host's clock rather than the reconciler's, it is mutable by anything that touches the
  file, and it makes safety depend on a tuning constant. Intent-before-volume ordering removes the
  same race with no clock and no constant.
- **Do nothing and let reap stay inert.** judgment: it leaves #2129's 80 commits unmergeable and
  leaves every remote module attempt's storage unreclaimable, which is the outcome #2157 was filed
  to prevent.
