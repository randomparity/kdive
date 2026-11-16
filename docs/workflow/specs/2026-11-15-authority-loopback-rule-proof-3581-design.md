# Guest-loopback nftables rule on a provider authority host (#3581) — design

## Problem

The runbook's optional `inet kdive_guest_loopback` rule rejects new loopback connections from the
uid QEMU runs as. `docs/operating/runbooks/image-lifecycle.md` says its effect on a host carrying
the `provider_authority_host` role is "unproven because no lab host carries
`provider_authority_host`", and [ADR-0810](../../adr/0810-guest-egress-host-loopback-exposure-is-accepted-and-documented.md)'s
2026-11-04 amendment records the remote authority arm as unknown. On such a host the role gives the
session libvirt daemon (and so QEMU) and the authority service one account,
`kdive-provider-authority`.

A code read predicts the answer. The authority's readiness loop
(`src/kdive/providers/external_boot_authority/host.py`, `run_authority_host`) re-runs
`check_tls_health` every 30 s, and for a network listener `_check_tls_connection` opens a TCP
connection from the service to its own listener address (`127.0.0.1` when bound to `0.0.0.0`).
Both destinations route through `lo`, so the rule, keyed to the shared uid, should reject that
probe, retract readiness and stop the service. The issue asks for the live result, not the
prediction.

## Scope

Outcome and criteria are frozen in the issue's `WORK:SCOPE` (token `q3581-b2d307d1`).

Proof, on the private lab (control plane plus one Fedora 44 host provisioned with
`provider_authority_host` through `site.yml`, network listener enabled, one manifest entry with
`guest_egress: true`):

1. Baseline without the rule: authority active and ready; an authority System provisions; from
   the egress guest, a TCP connection to `10.0.2.2:<port>` of a listener bound to host
   `127.0.0.1` succeeds.
2. Read the QEMU uid from the running process (`ps -eo uid=,comm=`) and confirm it equals the
   authority service's uid.
3. Load the runbook's rule text unchanged, keyed to that uid, with the DNS exemption for the
   host's loopback resolver.
4. Record (a): the same guest connection with the rule loaded.
5. Record (b): authority unit state (`systemctl show -p ActiveState,SubState,Result,NRestarts`)
   and journal message text (`journalctl -o cat`) across at least two readiness intervals, then
   a provision and teardown attempt through the lane. The unit's `StartLimitBurst=5` in 600 s can
   leave it `failed` with `Result=start-limit-hit`; record that as distinct from the rule's effect.
6. Remove the rule. If the unit is `failed`, run `systemctl reset-failed` then `systemctl start`;
   record the pre-reset state and whether the unit then reached active and ready. Tear down the
   System through the lane only once the authority is serving; otherwise rely on step 7.
7. Roll both guests back to the pre-run snapshot.

Guest commands run through the QEMU guest agent as the authority account, so no guest SSH key
is needed. If the authority cannot be stood up on the lab (environment failure outside this
issue), the run parks as blocked with the exact failure.

Documentation, written after the proof and stating only what it showed:

- `image-lifecycle.md`: replace the "unproven" sentence with the observed result for an authority
  host and why (or, if (b) held, the conditions under which it held).
- ADR-0810: append `### Amendment (2026-11-15): ... (#3581)` with method, results table, limits.
  Earlier text is not edited. If the result is "not usable", say exactly which check failed; do
  not generalise beyond the arrangement that ran.

No code, rule or role change (operator exclusions in `WORK:SCOPE`).

### Failure model

1. **Actors and deployments** — the campaign worker on the private lab; readers are operators of
   a remote authority host deciding whether to load the rule.
2. **Invariants and assets at stake** — published security guidance: a runbook that calls the rule
   safe where it stops the authority, or unsafe beyond what ran; no lab identifiers in public
   text; lab guests returned to their pre-run snapshot.
3. **Accepted failure classes** — an AF_UNIX-only authority (no network listener) is not a
   remote-authority arrangement and is not run; documented as untested if mentioned. Rocky,
   openSUSE, Ubuntu authority hosts and ppc64le stay unproven (one Fedora 44 host ran).
4. **Covered elsewhere** — making the rule safe under a shared uid (operator exclusion); btrfs
   proof (#3580); `pki.yml` client file modes (separate follow-up).

## Success

- Outcomes (a) and (b) each recorded as observed, with the uid evidence, in the ADR amendment.
- The runbook no longer says "unproven" for the authority host and matches the amendment.
- (b) states whether the start limit was hit and what restored the unit; a unit left `failed` by
  the start limit is attributed to it, not to the rule.
- Both lab guests rolled back. The diff, commit messages, PR body and issue comments carry no lab
  hostname, FQDN or domain suffix, address, username, lab name or VM id; each is scanned for the
  authority host's short name, FQDN, domain suffix and addresses before it is committed or posted.

## Validation

- Live proof: steps 1-6 above; the transcript of each check is the evidence
  (`task-test-not-applicable`: prose records; no executable consumer validates them).
- `just docs-links`, `just docs-paths`, `just adr-status-check`, `just records`, `just lint`.
