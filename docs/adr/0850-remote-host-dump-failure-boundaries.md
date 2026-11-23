# 0850 — Remote host-dump failure boundaries

## Status

Proposed

## Context

Remote capture suppresses required stale-volume deletion, starts fresh cleanup too late and
lets unlink replace a persistence failure. Refresh and local spool preparation can escape raw.
The existing owner and public entrypoint already express this workflow.

## Decision

Keep the owner. Require stale deletion to succeed or raise a chained infrastructure error.
Begin cleanup immediately after resolving the fresh volume. Attempt unlink, directory removal
and volume deletion independently, warning on their native OSError/libvirtError without raw
exception text or traceback. Preserve the original result or failure across those expected
cleanup faults. Categorize native refresh, spool-creation and size-stat failures with context;
retain digest and stream contracts. Remove the broad-suppression volume helper.

## Consequences

Capacity/spool failures attempt fresh cleanup. Successful capture may retain cleanup residue
with a warning; cleanup before a fresh handle exists and abrupt worker death still rely on the
existing next-capture/orphan policy. Unexpected programming/process-control failures retain
propagation. No generalized cleanup framework, migration, privilege or public API is introduced.

## Considered & rejected

- **Keep current handling.** verified: at73690492c, the public-capture private reproduction
  `.desloppify/repro_host_dump_boundaries.py` fails four cases in0.49s: dump occurs after failed
  stale deletion; refresh/spool errors escape; unlink masks the original store failure.
- **Wrap the whole workflow in one broad handler.** judgment: would blur required preparation,
  transfer and best-effort cleanup, changing error identities outside the verified gaps.
- **Add a reusable cleanup framework.** judgment: three short native cleanup blocks in one
  existing owner need no extra dispatch or abstraction.
