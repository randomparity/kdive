# Remote external-boot coordinator contract

Issue: #2200
Status: Implementing

This slice defines only the closed durable recovery record and the six-operation boundary consumed
by the remote coordinator. Construction, host service wiring, job handling, and provider
advertisement remain owned by follow-on integration.

The record binds one exact System, Run, and activation to the plan and materialization identities,
the validated source/target domain definition pair, the V2 module recovery geometry, both provider
state identities, prior power, and the complete sorted set of provider-owned recovery objects. It
contains no URI, credential, request-selected path, XML fragment, or command beyond the already
validated closed definition value. Rehydration recomputes ownership and identity relationships and
rejects unknown fields or records larger than 1 MiB.

`RemoteExternalBootOperations` exposes exactly materialize, prepare, activate, observe, recover,
and cleanup. Every method receives one caller-established absolute monotonic deadline. Later
coordinator work may sequence these methods but may not add a general filesystem, libvirt, command,
or transport escape hatch.

Focused tests pin the six names and deadline parameters, canonical round trips, exact ownership,
source/target state relationships, V2-only module geometry, sorted unique recovery objects, and
the serialized bound.
