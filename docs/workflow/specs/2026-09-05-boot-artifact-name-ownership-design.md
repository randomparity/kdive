# Remote boot-artifact name ownership design

Issue: #2240
Decision: [ADR-0599](../../adr/0599-boot-artifact-ownership-lives-in-volume-names.md)

## Goal and scope

Make remote-libvirt kernel and initrd volumes recognizable and safely reapable after real libvirt
discards submitted storage-volume metadata. This change is limited to boot-artifact name rendering,
parsing, materialization, inventory/reaping, the shared dir-pool double methods this path exercises,
and focused/native-x86_64 proof. It does not change database state, live-owner semantics,
remote-module names, provider-neutral models, or ppc64le coverage.

## Contract

`boot_artifact_name.py` owns the one persisted grammar from ADR-0599. Its renderer takes a closed
kind, canonical UUID values, a `sha256:` digest, final/partial state, and an attempt UUID only for a
partial. It rejects malformed digests or invalid state/attempt combinations. Its parser accepts
only byte-for-byte canonical output: anchored lowercase grammar, canonical UUID spellings, known
kind, exact version, and exactly one final/partial suffix. Rendered names are ASCII and at most 203
bytes.

The parsed record contains `name`, `kind`, `system_id`, `run_id`, `digest`, `partial`, and
`attempt_id`. Its `owner` is the existing live-owner tuple `(kind, system_id, run_id, digest)`.
There is no fallback to XML metadata, prefix matching, a legacy parser, or inferred ownership.

Materialization computes the complete-byte digest before opening the pool. It renders the final and
partial names from that digest. An existing exact final name is reused only after a complete stream
rehash matches. A same-System/Run artifact with different bytes naturally has a different name; the
old name remains for the reaper rather than being overwritten. Upload, clone, verification, and
failure cleanup retain their existing fail-closed behavior, but every lookup uses the new name.
Volume XML becomes the minimum raw-volume document: name, capacity, and target format.

Inventory refreshes and enumerates the configured pool, parses only the name, then streams the
complete bytes. It returns an object only when the digest matches. Reaping follows the same order,
then skips a live owner and deletes only a parsed, byte-matching orphan. Stream/read failure is a
non-match and no deletion. A non-absence libvirt failure remains an infrastructure error with only
pool and volume name in details.

## External-system double

The shared `FakeStoragePool` remains a dir-pool model: `XMLDesc` is rendered from frozen retained
state and does not echo submitted XML. This change adds only operations the boot path drives:
upload/download byte storage, clone-by-`createXMLFrom`, refresh/list, and connection-backed streams.
Each method gets a focused behavior test. Boot materialization and reaping tests use that shared
double so an implementation that returns to XML metadata fails under ordinary unit coverage.

## Compatibility and failure behavior

- Legacy or unknown-version names are foreign and untouched.
- Uppercase, noncanonical UUID, wrong kind, short/long/nonhex digest, suffix ambiguity, extra text,
  and attempt-on-final shapes are malformed and untouched.
- A syntactically owned name whose bytes hash differently is untouched.
- A valid matching object whose owner tuple is live is untouched.
- Final and partial matching orphans are deleted; an already-absent delete is an achieved state.
- The implementation never logs or returns volume bytes, XML, credentials, or host paths.

## Verification

Focused tests first demonstrate red against metadata-free readback, then cover renderer/parser
round trips and every negative class above; materialization retry/failure behavior through the
shared double; and inventory/reaper behavior through the same double. Controlled faults restore
metadata dependence and weaken the digest/content or live-owner gates to show the tests bite.

The native x86_64 carrier uses the already-authorized private Ubuntu remote host and an isolated
temporary dir pool. It materializes kernel and initrd bytes, reconnects to libvirt, confirms
readback contains no submitted metadata, inventories both by name and digest, reaps both with an
empty live-owner set, verifies the pool is empty, and cleans the pool on every exit. Environment
values remain private and are never copied into public artifacts. Native ppc64le is excluded.
