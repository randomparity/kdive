# 0860 — Preserve accepted object-stream cleanup failures

## Status

Accepted (2026-11-24)

Refines [ADR0400](0400-streaming-object-read-for-combined-tar-extract.md).

## Context

Accepted object streams must close on context exit, including early consumption. The current
unguarded close can replace a consumer or classified read failure; a standalone close error
escapes the store's infrastructure taxonomy. Metadata rejection has a different lifetime owner.

## Decision

Track the stream lifetime's primary exception explicitly. On ordinary close failure, retain
that primary and add static phase/type-only evidence; otherwise raise the existing categorized
infrastructure error with close phase, object key and SDK code/type. Suppress raw standalone
close context. Control exceptions from close propagate; ordinary close faults cannot mask
consumer/read control exceptions. Always attempt close once; do not promise successful release.

Keep error mapping in the existing store helper. Buffered reads, rejected metadata, consumer
contents and [libvirt cleanup](0859-local-retrieve-native-failure-boundaries.md) stay unchanged.

## Consequences

The initiating failure still controls recovery. A standalone close failure remains an operation
failure but now has actionable typed context. Broken transports may still retain resources;
no retrying close loop or generic cleanup framework is added.

## Considered & rejected

- **Keep unconditional close.** verified: `git show 65c218e38874:src/kdive/store/objectstore.py`
  shows get_artifact_stream closing in an unguarded finally; actual ObjectStore consumer/read
  probes on Python3.14.8/Linux propagate close OSError instead of the initiating failure.
- **Ignore standalone close faults.** verified: that same current stream reports close failure
  even after successful consumption. Silencing it would remove an existing failure signal.
- **Introduce a cleanup framework.** judgment: this one stream lifetime needs no new service or
  helper; existing error mapping and local primary tracking suffice.
