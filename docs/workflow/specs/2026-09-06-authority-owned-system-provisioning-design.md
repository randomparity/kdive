# Authority-owned System provisioning design

ADR-0623 governs this design. It adds production initial provisioning for
authority-enabled local-libvirt and remote-libvirt Resources without changing
ordinary Resources or fabricating activation and Run identities.

## Required behavior

`systems.provision` continues to validate the submitted profile, Allocation,
Resource, quota, and root provenance under the existing lock order. When the
resolved Resource has a complete authority binding, the same admission
transaction:

1. inserts the provisioning System and immutable root provenance;
2. enqueues the ordinary `provision` job with an `authority_system_v1` marker;
3. inserts one `authority_system_ownership` row derived from the stored rows;
4. activates the Allocation and writes the existing audit records.

The marker contains stable IDs, provider/Resource/authority names, canonical
profile and root digests, operation, and operation identity. It contains no
profile document, bootstrap key, activation, Run, plan, path, URI, XML,
credential, or provider port.

The generic queue may claim and heartbeat the marked job. Generic completion,
failure, and abandoned-job repair reject it. The worker creates or reads the
existing durable `system_bootstrap_keys` row before allocating an authority
attempt. Allocation recomputes and writes its public-key digest once.

## Persistence

`authority_system_ownership` has immutable System, Allocation, Resource,
provider kind, Resource name, authority instance, profile digest, root digest,
and creation time. Mutable fields are:

- state: `provisioning`, `ready`, `activated`, `repair-required`,
  `teardown-requested`, or `torn-down`;
- positive next generation;
- last positively acknowledged attempt;
- write-once bootstrap digest and first activation ID;
- the global journal sequence, digest, phase, and canonical record.

The journal begins at sequence zero and the fixed zero digest. It is one chain
per authority instance and System across every attempt generation.

`authority_system_attempts` stores one immutable claimed generation: authority
ID, System/generation, operation, exact job/attempt/worker, request nonce,
operation identity and digest, state, write-once acknowledgement, write-once
terminal-head anchor, immutable terminal receipt bytes/digest/disposition, and
write-once consumption time. Attempt states are `allocating`, `current`,
`superseded`, and `terminal`.

An allocating successor does not change the predecessor or ownership's current
attempt. The authority serializes it after the predecessor's completion-owned
task and appends a watermark to the global chain. Only a positive-quiescence ACK
atomically supersedes the predecessor and selects the successor.

## Protocol

All models forbid extra fields, validate lowercase SHA-256 values and bounded
UTF-8 identifiers, and cap envelopes at 1 MiB.

`AuthoritySystemMarkerV1` fields are:

```text
schema, system_id, allocation_id, resource_id, provider_kind, resource_name,
authority_instance, profile_identity, root_identity, operation,
operation_identity
```

Takeover and mutation requests add authority ID, generation, request attempt ID,
operation digest, and the durable bootstrap digest. Commit context contains the
request attempt ID, operation, journal sequence, and journal digest.

`AuthoritySystemJournalRecordV1` has exactly:

```text
schema, authority_id, generation, system_id, allocation_id, resource_id,
provider_kind, resource_name, authority_instance, profile_identity,
root_identity, bootstrap_identity, operation, operation_identity,
operation_digest, attempt_id, sequence, previous_digest, phase, observation,
outcome, canonical_record
```

Legal phases are `watermark-installed`, `takeover-superseded`,
`takeover-acknowledged`, `admitted`, `mutation-started`, `provider-returned`,
`observed`, and `terminal`.

Terminal proof dispositions are:

- `provision-ready`: literal domain-owned, root-storage-owned, boot-ready, and
  bootstrap-ready predicates; no quarantine; stable aware-UTC completion time.
- `provision-failed`: a closed error category, retained-state flag, and stable
  aware-UTC completion time.
- `preactivation-absent`: literal domain, root storage, baseline, and private
  intent absence; no quarantine; stable aware-UTC completion time.
- `retained-quarantine`: an observation digest only; it cannot terminalize the
  System or discharge cleanup.

The provider journal advance accepts the exact canonical proof bytes separately
from the JSON record. A terminal advance validates that the record names those
bytes, then writes the global head and attempt receipt atomically. Nonterminal
advance requires absent proof bytes. SQL hashes and compares supplied UTF-8
bytes; it does not reserialize JSONB.

## Trusted snapshot

The provider-authority repository resolves the acknowledged attempt together
with the System, Allocation, Resource, stored provisioning profile, immutable
`system_root_provenance`, and durable `system_bootstrap_keys.public_key`.
Before provider dispatch it:

- parses the profile as `ProvisioningProfile` and recomputes its canonical
  prefixed digest;
- parses `RootSpecV1` and checks image digest, image ID, project, and
  architecture against ownership and the System;
- hashes the exact public-key UTF-8 bytes and checks the write-once bootstrap
  digest;
- rejects any changed or malformed stored value as conflict.

The resulting typed snapshot contains the canonical profile, root provenance,
and public key. The worker never forwards those values.

## SQL functions and roles

Only `kdive_server` executes:

```text
register_authority_system_ownership(uuid,uuid,text,text,text,text,text) -> text
request_authority_system_preactivation_teardown(uuid,uuid,text) -> text
claim_authority_system_first_activation(uuid,uuid) -> text
```

Only `kdive_worker` executes:

```text
allocate_authority_system_attempt(bytea,uuid,integer,uuid)
acknowledge_authority_system_attempt(
  bytea,uuid,integer,uuid,bigint,uuid,bigint,text,text)
finalize_authority_system_attempt(
  bytea,uuid,integer,uuid,bigint,bigint,text,bytea)
```

Only `kdive_provider_authority` executes:

```text
resolve_allocating_authority_system_attempt(text,uuid,bigint)
resolve_current_authority_system_attempt(text,uuid,bigint,bigint,text)
read_authority_system_journal_head(text,uuid,bigint,text)
advance_authority_system_journal_head(
  text,uuid,bigint,bigint,text,jsonb,bytea)
list_authority_system_journal_heads(text)
```

Only `kdive_reconciler` executes:

```text
repair_terminal_authority_system_attempts(integer) -> integer
```

Every function is `SECURITY DEFINER SET search_path=''`; the two tables grant no
runtime role direct access. Allocation requires an active fence-protocol worker,
exact running claimed job/attempt/live lease, exact marker, current System and
Allocation state, and configured authority binding. Resolve requires the
authenticated peer and allocating/current generation. Before
`mutation-started`, current resolve also requires the live job lease. Later
journal completion relies on the immutable acknowledged authority and global
head CAS so actual provider completion can be recorded after disconnect or
lease expiry.

Worker finalization requires the current ACK, terminal head, exact proof bytes,
job and charged attempt. It consumes once. Exact receipt replay remains applied
after System/job terminal; different bytes conflict. The reconciler repair
function, bounded to 100 candidates with `SKIP LOCKED`, may consume only an
already-authenticated terminal receipt after the original active lease cannot
finish it. It selects the attempt referenced by
`authority_system_ownership.current_attempt_id`, including after that attempt's
state becomes `terminal`; it does not filter for `attempts.state = 'current'`.
It cannot create evidence.

## State and concurrency

Normal provision changes ownership `provisioning -> ready`, System
`provisioning -> ready`, succeeds the job, opens billing, and audits in one
transaction. A provision failure changes ownership to `repair-required` and
System/job to failed without releasing capacity. A retained proof changes no
core lifecycle or cleanup state.

`jobs.cancel` on authority provision atomically cancels the job, changes
ownership to `teardown-requested`, and enqueues or replays the preactivation
teardown job under the System lock. A late ready receipt is consumed to retain
accurate physical facts but cannot publish the System ready.

`systems.teardown` before first activation changes ownership to
`teardown-requested`. Teardown waits for or adopts the terminal provision
intent, then destroys only verified owned state. `preactivation-absent` changes
the System and ownership to torn down and performs existing core cleanup/audit.
Retained quarantine keeps the System nonterminal and capacity charged.

First activation inserts its activation and calls
`claim_authority_system_first_activation` in the same System-locked transaction.
Only ownership `ready` succeeds and becomes `activated`; teardown-requested
blocks. Migration 0148 patches the existing 0147 finalizer so authenticated
activated teardown also changes ownership to `torn-down` atomically.

No authority-owned System reaches ordinary provisioner or reprovisioner ports.
External-boot operations are allowed only after `activated` and retain their
existing authority route. Other unsupported destructive/mutation ports fail
before provider dispatch.

## Host execution and inventory

An owner-only manifest maps `(provider_kind, resource_name, authority_instance,
root_identity)` to fixed private topology. Local uses the fixed session socket,
digest-keyed staged base, private artifact roots, and Resource guest-egress
setting. Remote uses the same fixed session connection/pool as the module host,
digest-keyed staged base volume, and fixed network/machine/GDB/SSH facts. No
worker TLS credentials or caller override is reachable.

Before mutation the provider persists exact host-derived intent including XML
identity, owned artifact identities, selected ports, bootstrap digest, and one
UTC deadline. Retry converts the remaining deadline to the new monotonic clock;
it never grants a new budget. Observation is read-only. Live and inactive XML,
storage graphs, normalized paths, and sibling references are validated before
deletion.

The distinct journal path is
`<state_dir>/system-operations/<system-id>.jsonl`. Readiness requires an exact
DB/file head bijection, lowercase UUID names, authority ownership, 0600 files
beneath a 0700 root, no symlinks, at most 4096 files, 1024 records per file,
16 MiB per file, and 1 MiB per record.

## Verification

Focused model, migration, repository, service, transport, worker, server, and
provider tests must cover malformed and cross-System values, wrong Resource or
authority, worker/job/attempt/lease loss, takeover during predecessor
completion, lost response replay, receipt conflict, process restart,
cancellation, DB/file split brain, read-only observation, aggregate bounds,
live/inactive ownership, aliases, foreign references, and sibling survival.
The real-DB repair case must consume a terminal attempt through
`ownership.current_attempt_id` without requiring its attempt state to remain
`current`.

Connected real-DB tests drive public provision through a real worker and
authenticated authority to ready for local and remote, then first activation and
existing authority teardown. A second path cancels or tears down before
activation and proves actual private absence. Authority-off local and remote
paths remain covered. The native operator fixture is reported separately and
cannot satisfy production provision acceptance.

## Exclusions

This change does not adopt or copy worker-daemon Systems, add authority-owned
reprovision, expose host topology, add a generic operation framework, or add
cloud/bare-metal provider behavior. The already-proven appliance runtime closure
is consumed as a deployment prerequisite and is not reimplemented here.
