# Diagnostics worker-vantage ID registration (#2344)

Issue: [#2344](https://github.com/randomparity/kdive/issues/2344). Plan:
[2026-09-13-diagnostics-worker-id-registration.md](../plans/2026-09-13-diagnostics-worker-id-registration.md).

## Problem

`result_codec` keeps a hand-written set of worker-vantage check IDs. That set has drifted from
provider contributions repeatedly. A descriptor cannot be the authority: the remote contribution
builds descriptors by iterating configured instances, so its output is empty before configuration
loads and varies by deployment.

## Decision

Extend the existing diagnostics-owned `DiagnosticProviderContribution` contract with a static
`worker_vantage_ids` sequence. Each provider contribution supplies its stable IDs directly from
`diagnostics.checks`; descriptor and check construction remain configuration-dependent fan-out.

`_worker_vantage_dispatch_mode` passes that contribution-owned static sequence to its
`JobWorkerCheckDispatcher`. The dispatcher supplies it to `deserialize_results`, which validates
only the result for that dispatched provider. `result_codec` therefore owns JSON reconstruction but
does not restate a provider catalog or import provider packages.

No ADR is added. This refines the data carried by the existing diagnostic-provider contribution
contract and the worker dispatch already established by ADR-0164; it creates no new runtime seam,
provider ownership rule, compatibility window, or persistence contract.

## Rejected approaches

- Derive IDs from `unavailable_worker_checks`: rejected because the remote list fans out over live
  configuration and may be empty despite valid worker result IDs.
- Import production composition from `result_codec`: rejected because it inverts the diagnostics ←
  providers dependency direction.
- Keep one global codec allowlist: rejected because it is a second registration authority and has
  already drifted.

## Flow and invariants

```
provider contribution static IDs → dispatcher's allowed IDs → result codec reconstruction
provider configuration → descriptors/checks                  (unchanged fan-out)
```

- Every ID accepted from a provider's worker job is declared by that contribution's static list.
- The accepted set does not depend on provider configuration or instance count.
- An unexpected ID still becomes that item's error result; malformed batch handling is unchanged.

## Verification

The regression guard compares each production contribution's static list with IDs emitted by its
actual `worker_checks()` builder under a deterministic remote-libvirt configuration and authority
sender fixture. It does not compare the list with the value merely passed to the dispatcher. A
controlled test fault removes a registered ID while its check builder still emits it; the guard must
fail, then the source is restored and the guard re-runs green. Focused codec, dispatch,
provider-contract, and factory tests and `just ci` prove the change.
