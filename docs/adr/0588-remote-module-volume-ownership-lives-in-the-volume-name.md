# 0588 — Remote module volume ownership lives in the volume name

## Status

Proposed

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
and recovery is inert. The same channel is used for remote boot-artifact volumes on `main`; that
occurrence is #2158.

Reap needs two facts about a volume whose creating worker is gone. Whether the volume is
unambiguously KDIVE attempt-scoped storage — deleting a foreign volume is operator data loss —
and whether its owning attempt is still live. Only the first has to come out of the volume.
KDIVE's state of record is Postgres, and the reconciler already reads a durable live-owner set
from it (ADR-0021); `reap_orphaned_boot_artifacts` already takes one as `live_owners`.

## Decision

The durable ownership channel for remote module volumes is **the volume name**. Libvirt persists
a volume's name because the name is the object's identity, and it is written by the same
`virStorageVolCreateXML` call that creates the volume, so no window exists in which the volume
is present and its ownership is not.

### The name carries the whole owner tuple

The provider already renders the complete owner tuple into the name. That shape is kept and given
a parser, so the change is a recognition rule rather than a new naming scheme:

```text
kdive-module-<system-uuid>-<run-uuid>-<operation-nonce>-<purpose>.ext4
```

`system-uuid` and `run-uuid` are canonical lowercase UUIDs, `operation-nonce` is the 32 lowercase
hex characters `RemoteModuleOperation` already validates, and `purpose` is `source` or `scratch`.
The longest name the grammar can render is 132 bytes. Measured on the same host, a dir-pool volume
name round-trips byte-identically up to 255 bytes and is refused at 256 — the filesystem
`NAME_MAX` a dir pool inherits — so the grammar holds 123 bytes of headroom.

Recognition is a single anchored parse of the whole name. A name that does not match is a foreign
volume: never read further, never deleted, never counted. There is no prefix match, no partial
credit, and no fallback to another channel.

The content digest is deliberately absent from the name. It is not known when the name must be
chosen, it is not needed to decide ownership, and ADR-0585 already re-verifies content against the
manifests it stores.

### Ownership recovery when the writing worker is gone

The reaper is a reconciler sweep (ADR-0021) with the durable store in reach. For each volume in
the configured module pool, after `pool.refresh(0)`:

1. Parse the name under the grammar. No match ends the volume's evaluation there — it is not ours.
2. A match *is* the recovery: `(purpose, system_id, run_id, operation_nonce)` comes off the name
   itself, so nothing has to be reconstructed from a dead worker's memory, from console output, or
   from the volume's bytes.
3. Join that tuple against the durable live-attempt set. Present means an attempt still owns the
   volume; the sweep leaves it alone.
4. Absent means the owning attempt is gone. Before deleting, resolve the volume's path and refuse
   the deletion if any active or inactive domain definition references it — ADR-0585's exclusivity
   check, which `protected_volume_paths` already performs. A referenced volume is a conflict to
   report, never a deletion.
5. Delete. `VIR_ERR_NO_STORAGE_VOL` is an achieved post-state, so the sweep is idempotent.

### Durable intent precedes the volume

A worker writes the durable attempt row naming `(system_id, run_id, operation_nonce, purpose)`
**before** it creates either volume. This ordering is what makes step 4 safe: a volume cannot
exist whose attempt has no row, so "absent from the live set" always means the attempt is over
rather than that it has not started. A row with no volume is benign and the ordinary crash
residue; the attempt path already reconciles it.

Without this ordering the sweep can delete a live worker's volume in the window between
`createXML` and the durable write. That window exists for any ownership channel written into the
volume, including the metadata element this record replaces, so the ordering is a requirement the
previous design also needed and did not state.

### Test doubles model what libvirt discards

`just ci` passed in full, 15,304 tests, against a design that cannot work in production, because
the `Pool.createXML` double echoed the submitted XML back verbatim. A double that accepts
everything asserts nothing.

The storage double used by remote-libvirt tests parses the submitted volume XML, retains only the
fields libvirt persists for a dir-pool volume — `name`, `key`, `capacity`, `allocation`,
`physical`, and the `target` subtree — and renders `XMLDesc` from that retained state. Every other
submitted element is dropped, exactly as libvirt drops it. The double owes a fidelity test of its
own, in the `live_vm` tier, that creates a real dir-pool volume carrying an unmodelled element and
asserts the real readback and the double's readback agree on the retained set and both omit the
unmodelled element.

A wrong implementation is therefore caught as follows. An implementation that puts ownership
anywhere libvirt does not persist gets nothing back from the double, so its reap tests fail on an
unrecoverable owner rather than passing on an echo. A double that drifts from libvirt fails the
fidelity test on a host with libvirt. A name grammar that overruns `NAME_MAX`, admits a foreign
shape, or fails to round-trip fails the budget and round-trip tests over the full parameter space.

## Consequences

- Ownership is inspectable with `virsh vol-list` and needs no XML, no volume read, and no second
  object. An operator reading a pool listing can see which System and Run owns each volume.
- The channel binds the provider to pool types whose volume names persist at least 160 bytes.
  `dir` is the pool type the remote-libvirt runbook provisions and the only one supported today
  (`docs/operating/runbooks/remote-libvirt-host-setup.md`).
  Adding a pool type with a tighter name limit — LVM logical volumes are the realistic case —
  requires revisiting this record rather than silently truncating a name.
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
- **An in-volume header.** verified: the reap case is a worker that died, and the earliest death
  is between creating a volume and writing anything into it, so the header is absent in precisely
  the case the channel exists for. It also costs the reaper a read of a filesystem image built by
  a dead attempt — the source volume is ext4 — which is the worker-side disk access ADR-0585
  rejected on the ground that libguestfs `guestfish(1)` needs disks the worker can open locally.
- **A sidecar index volume.** judgment: creating the volume and appending its journal entry are
  two operations, so a crash between them leaves an unowned volume — the failure the channel
  exists to prevent, reintroduced. The index's own orphaning would then need a name convention,
  which is this decision with an extra object in front of it.
- **A libvirt domain as a metadata carrier.** verified: domains do persist `<metadata>` —
  `domaincommon.rng` defines it and `virDomain.setMetadata` exists — so the channel is real. It
  carries the same two-operation crash window as the index volume, and it makes an inert domain
  definition per attempt on a host where `reaping/domains.py` already sweeps kdive-named domains,
  so the carrier has to be excluded from that sweep by name. Ownership by name, without the
  domain.
- **A match key in the name plus a durable-store lookup for the rest of the tuple.** verified:
  this was the shape #2157 assumed the name channel would take, on the premise that a name can
  carry only a match key. A 255-byte dir-pool name holds the 132-byte full tuple with 123 bytes
  spare, measured on the host above, and the provider's existing names already carry it, so the
  premise does not hold and the reduced form buys nothing.
- **A reap grace window on the volume's timestamps.** verified: a dir-pool volume readback does
  expose `<target><timestamps>` with `mtime` and `ctime`, so a window is implementable. It is the
  remote host's clock rather than the reconciler's, it is mutable by anything that touches the
  file, and it makes safety depend on a tuning constant. Intent-before-volume ordering removes the
  same race with no clock and no constant.
- **Do nothing and let reap stay inert.** judgment: it leaves #2129's 80 commits unmergeable and
  leaves every remote module attempt's storage unreclaimable, which is the outcome #2157 was filed
  to prevent.
