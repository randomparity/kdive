# Reusable-build pin recovery

An install worker holds a durable reusable-build pin while it may read kernel artifacts. A stale
job lease does not prove that worker stopped, so the reconciler and garbage collector do not remove
the pin automatically.

Use `ops.build_uses_list` with a platform-operator token to inspect a bounded oldest-first page of
pins. For a local worker, `ops.recover_build_use` verifies the exact worker incarnation against
Linux process identity: hostname, boot ID, PID, and process start time. It refuses a live process,
a foreign host, malformed identity, or an observation error. The caller cannot supply the death
evidence.

Pass the exact `use_id` and `holder` returned by the list tool plus a concise operator reason. A
successful recovery atomically retains the generated evidence and reason in
`investigation_build_use_recoveries`, writes the platform audit row, and deletes only that use pin.
Both evidence and reason are bounded in the API and database. Repeat listing before recovery if the
holder may have changed.

Remote workers require a deployment-specific authoritative death verifier; the built-in verifier
fails closed for worker identities from another host.
