# Remote reaper metadata read failure

## Problem

`_recorded_pool` returns None on every libvirt XMLDesc error. `teardown_on_host`
then destroys/undefines the domain and deletes its named overlay from the configured
fallback pool. A failed metadata read is therefore treated as missing pool metadata.

## Design and scope

Keep the existing helpers and their successful behavior. `_recorded_pool` returns
None only when XML was successfully read and parsed without recorded pool information.
It re-raises VIR_ERR_NO_DOMAIN unchanged; other libvirt errors become chained
`_infra("reading leaked domain XML", domain=name)` failures. Parser errors retain
existing behavior. `teardown_on_host` catches that disappeared-domain result and
returns False before domain mutation or volume deletion, matching lookup absence.
Do not swallow an unexpected raw libvirt error in this catch.
Keep grouped-metadata precedence, source pool selection, successful legacy fallback,
owned names, fleet search and destroy/undefine error handling unchanged. Add no
new configuration, exception class, tuple/sentinel result or cleanup service.

## Success

1. Unexpected XML errors preserve their cause and bounded operation/domain context,
   and perform no domain or storage mutation.
2. A domain lost between lookup and XML read returns False and performs no mutation.
3. Successfully parsed metadata chooses the same pool and retains legacy fallback.

## Failure model

- Actors/deployments: the reconciler reads configured remote-libvirt hosts and reaps
  domains selected by the existing ownership policy; hypervisor RPCs can fail or race.
- Invariants/assets: unreadable metadata cannot authorize fallback storage deletion;
  domain mutation does not precede obtaining the metadata needed for cleanup.
- Accepted failures: disappearance can leave storage not identified by this attempt;
  this is the existing lookup-absence behavior, preferred to guessing a pool.
- Covered elsewhere: domain ownership, strict XML/pool validation, volume deletion,
  transport/TLS and fleet scheduling retain their existing implementations.

## Threat model

- Existing boundary: hypervisor XML RPC success/failure enters destructive cleanup
  decisions; no new/widened network, credential or mutation boundary.
- Actors: configured remote host behavior, transport/permission errors and concurrent
  domain removal. No new trust is granted to XML or configured fallback pool names.
- Control: require successful metadata read before fallback; categorized failure or
  achieved disappearance exits before destructive calls. Error details use domain
  context and exception chaining, without embedding raw provider text in the message.
- Out of scope: new orphan-volume recovery, host administration and changed ownership
  policy; this unit neither proves nor performs storage cleanup after disappearance.

## Validation

Extend the existing _FakeDomain XMLDesc boundary with injectable libvirt errors.
Unexpected internal/permission errors must raise chained infrastructure failures;
NO_DOMAIN must return False. Assert destroyed/undefined flags stay false and volume
calls stay empty for each failed read. Preserve recorded/grouped/legacy XML cases.
Expected red is missing categorized error or destructive fallback after disappearance.
Use controlled broad-swallow and disappearance-result faults to prove the tests bite;
restore source. Run remote reaping tests, lint/whole-tree type and hooks. No production
host or live destructive operation is needed for this dependency-boundary contract.
