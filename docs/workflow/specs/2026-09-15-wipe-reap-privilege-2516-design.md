# `--wipe` aims `sudo virsh` at an operator-owned session socket (#2516) — design

## Problem

`stack-down.sh --wipe` runs every mutating reap call under `sudo` and enumerates bare. The
endpoint is usually a **session** socket: `install-live-worker-lifecycle.sh` adopts one only when
the live pid's uid is the operator's, publishes it mode `0770` group `kdive-live-libvirt`, and
creates `/var/lib/kdive/rootfs` as `operator:kdive-live-libvirt` mode `2770`. Root owns none of
that, so escalation bypasses the group gate instead of satisfying it — and on a plain
`qemu:///session` it reaches a different per-uid daemon, the enumerate/mutate split
`stack-down.sh:64-67` names. A fixed privilege cannot be right everywhere: `resolve_libvirt_uri`
still resolves root-owned `qemu:///system` on a host with no lifecycle contract.

## Scope

In: `scripts/live-stack/stack-down.sh` gains one privilege decision derived from
`KDIVE_LIBVIRT_URI` (ADR-0662), applied to the enumeration, `destroy`, `undefine`, and the
overlay `rm`. `deploy/systemd/README.md` states that tooling reaches the published endpoint and
the provider data directories with the operator's own credentials.
`tests/scripts/test_live_stack_scripts.py` gains one arm per branch.

Out: the reap's exit-code and reporting behaviour (#2515, merged — preserved unchanged); the
unused `kdive_domains()` and its duplicated predicate (#2559); the probe grading connectivity
rather than daemon identity (operator-owned question); the endpoint publication contract
(ADR-0659).

No ownership transition: `stack-down.sh` already owns the reap and keeps it.

## Failure model

**Actors and deployments.** A local operator running `stack-down.sh --wipe` on (a) a host with
the lifecycle contract — session endpoint, operator in `kdive-live-libvirt`; (b) a bare dev host
— `qemu:///system`, operator able to `sudo`. CI runs the shell-shape tests only, never the reap.

**Invariants and assets at stake.**

- The reap is irreversible: domains, overlays, and compose volumes do not come back.
- A reap that removes nothing must not report success (#2515's contract, already held).
- The list grading a reap and the calls performing it must not come from different identities.

**Accepted failure classes.**

- An operator on a session host outside `kdive-live-libvirt` is refused at the gate rather than
  escalating around it. Accepted: that is the contract's access path, and the README already
  tells them to start a new login session.
- An operator on a bare host without `sudo` is refused at the gate instead of mid-wipe.
  Accepted: strictly earlier than the existing failure, and bounded to a refusal.
- A URI whose scope is not in its path is misclassified. Accepted: not reachable — both
  published URIs and the bare-host default carry it there.

**Covered elsewhere.** Wrong-daemon endpoints — #2559 and the operator-owned probe-identity
question. Endpoint validation and the degraded state — ADR-0659, `libvirt-uri.sh`.

## Threat model

**Boundary inventory.** None added. One **narrowed**: on a session host the reap's calls against
the libvirt socket and the overlay directory stop crossing from the operator to root.
`KDIVE_LIBVIRT_URI` is read at one new place — the classifier — as a string to test, never as a
command word.

**Actor model.** Trusted: the invoking operator, and the root-published contract file
(`require_exact_libvirt_env` validates root:root 0644 against a two-value allowlist). No new
untrusted party: the reap is a local operator tool with no remote input.

**Control per boundary.** The classifier tests `${KDIVE_LIBVIRT_URI%%\?*}` against a literal
`*/session` pattern; a non-match falls to the escalating branch, so the failure direction is
today's behaviour, not a silent loss of privilege. The endpoint stays quoted at every `virsh -c`
use. Nothing new is logged — the reap banner already prints the endpoint.

**Explicitly out of scope.** A caller who can set `KDIVE_LIBVIRT_URI` already chooses the daemon
the reap talks to by design (ADR-0659), so choosing its privilege class adds no capability that
caller lacks.

## Success

1. On a session endpoint none of the four reap commands — enumeration, `destroy`, `undefine`,
   overlay `rm` — runs under `sudo`.
2. On a non-session endpoint all four keep `sudo`.
3. The enumeration carries the same privilege as the mutations on both branches.
4. #2515's reporting contract is unchanged: the existing reap arms stay green untouched.
5. `deploy/systemd/README.md` states the operator-credential expectation for tooling.

## Validation

| Contract | Mode |
| --- | --- |
| Success 1 | `focused-test`: `test_wipe_reaps_a_session_endpoint_without_sudo` — a recording `sudo` stub is never invoked across the whole run |
| Success 2 | `focused-test`: `test_wipe_reaps_a_system_endpoint_under_sudo` — the stub's log carries the `list`, the `undefine`, and the `rm` |
| Success 3 | `focused-test`: both arms assert over the whole run, so the gate's and the reap's enumerations are inside the assertion on either branch |
| Success 4 | `focused-test`: the existing `test_wipe_*` arms, run unmodified |
| Success 5 | `task-test-not-applicable`: operator prose with no executable consumer; a test searching for wording would assert nothing about behaviour |
