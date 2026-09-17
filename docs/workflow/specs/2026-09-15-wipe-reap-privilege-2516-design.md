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
overlay `rm`. `deploy/systemd/README.md` states that the `--wipe` reap reaches the published
session endpoint and the overlay directory with the operator's own credentials. Scoped to the
reap, not to tooling generally: `stack-services.sh` legitimately uses `sudo` against the same
tree — `install -d` to create the provider data directories and `systemctl enable` on a bare
host — so a blanket "sudo is not the access path" would be contradicted in-tree.
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
- On the non-session branch the gate's enumeration is now the run's first `sudo`, so a host
  configured to prompt asks for the password before the irreversibility warning and the
  `Type 'wipe'` confirmation, and the refreshed sudo timestamp outlives an abort at that prompt.
  Accepted: refusing before asking spares the operator a confirmation on a run that cannot
  proceed, and `--wipe --yes` skips the prompt entirely, so the ordering only shows on the
  interactive bare-host path. Not because the ordering is forced — the confirmation block itself
  precedes every destructive step, so moving the gate below it would still refuse before anything
  is stopped. Two alternatives were weighed and declined as confirmation-UX changes independent
  of the privilege decision: moving the gate below the confirmation, and printing the warning
  ahead of the gate while leaving the confirmation where it is (which separates the warning from
  the confirmation it qualifies and warns about irreversibility on runs the gate then refuses).
- A URI whose scope is not in its path is misclassified. Accepted, but **not** because it is
  unreachable — an earlier draft of this entry said so and was wrong. The two published URIs and
  the bare-host default all carry the scope in the path, but they are only three of the four
  producers: `resolve_libvirt_uri`'s else branch adopts a caller-supplied `KDIVE_LIBVIRT_URI`
  verbatim (ADR-0659, reported since ADR-0661), and this change's own `_wipe_reap(uri=)` test
  helper drives exactly that route. What makes the class acceptable is the *direction*: the
  `case` anchors `*/session` at end-of-string, so every value that does not end in a `/session`
  path component falls to the escalating branch, which is today's behaviour. A misclassification
  can cost an unnecessary `sudo`; it cannot silently drop privilege.
- The overlay `rm` is always local, while the endpoint it takes its privilege from need not be:
  `qemu+ssh://operator@remote/session` classifies as session and de-escalates a purely local
  unlink. Accepted: the operator owns the mode-`2770` overlay directory, so the unlink normally
  succeeds anyway, and where it does not the `[[ ! -e "$overlay" ]]` re-read routes the shortfall
  into `unreaped` and exits 1. Loud, never silent. A remote endpoint is outside what the
  lifecycle contract publishes in any case.

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
`*/session` pattern anchored at end-of-string; a non-match falls to the escalating branch, so the
failure direction is today's behaviour, not a silent loss of privilege. The endpoint stays quoted
at every `virsh -c` use and is never a command word — `reap_run` forwards `sudo "$@"` or `"$@"`,
so the URI is always argv. Nothing new is logged — the reap banner already prints the endpoint.

The four governed commands do not all cross the same boundary: the three `virsh` calls go to
whatever daemon the endpoint names, local or remote, while the overlay `rm` is unconditionally
local. One privilege governs both because the reap is one operation, but a remote session
endpoint therefore de-escalates a local unlink — recorded as an accepted failure class above
rather than left implicit here.

**Explicitly out of scope.** A caller who can set `KDIVE_LIBVIRT_URI` already chooses the daemon
the reap talks to by design (ADR-0659), so choosing its privilege class adds no capability that
caller lacks.

## Success

1. On a session endpoint none of the four reap commands — enumeration, `destroy`, `undefine`,
   overlay `rm` — runs under `sudo`.
2. On a non-session endpoint all four keep `sudo`. The up-front gate's liveness probe is not one
   of the four: it grades nothing, so it runs as the invoking account on **both** branches and
   stays the operator's own authorization for the endpoint they aimed at. An operator who cannot
   reach that daemon is refused before anything is stopped or dropped. This is what keeps
   escalation from being the thing that makes an unreachable endpoint reachable — with an
   escalated probe, a run aimed at the wrong daemon passes the gate, drops the data volumes, finds
   zero domains, and sweeps overlays whose domains are alive elsewhere, exiting 0.
3. The libvirt enumeration that grades the domain reap carries the same privilege as the domain
   mutations, on both branches. The overlay half is symmetric only on the session branch: on the
   non-session branch the `-d`/`-r`/`-x` tests, the glob and the `[[ ! -e ]]` re-read stay in the
   calling shell while `rm` escalates. That residue is not closed here and does not need to be —
   the existing `! -r || ! -x` refusal already turns an unlistable directory into a named failure
   rather than an empty sweep, which is the only outcome the asymmetry could corrupt.
   The session branch inverts which half is weaker: `rm` is now the calling shell's, so unlinking
   needs **write** on the directory, which `-r`/`-x` never covered. That **is** a regression this
   change introduces, and the precondition below is what closes it. The earlier reading — that the
   same host already produced one denied `rm` per overlay — was wrong: on `main` the block's only
   `rm` is an unconditional `sudo rm -f`, so root unlinks whatever the directory's mode, no denial
   is produced, and write on the directory is never required. Routing that `rm` through the
   endpoint-derived privilege is what makes it required.
   The precondition is in the up-front `--wipe` gate: on the session branch, an overlay directory
   that is not writable and holds at least one `*-overlay.qcow2` refuses the run before anything
   is stopped or dropped. It is keyed on that **non-empty glob**, never on the mode alone, and
   that condition is load-bearing rather than incidental — a bare `! -w` refusal was written and
   reverted inside this change's review round because, keyed on permissions alone, it reported a
   fully successful reap of an empty overlay directory as a failure, which is #2515's defect. The
   gate bounds the writability class only; an unreadable directory still refuses at the sweep,
   after the volume drop, as it does on `main`.
4. #2515's reporting contract is unchanged: the existing reap arms stay green untouched.
5. `deploy/systemd/README.md` states the operator-credential expectation for the `--wipe` reap.

## Validation

| Contract | Mode |
| --- | --- |
| Success 1 | `focused-test`: `test_wipe_reaps_a_session_endpoint_without_sudo` — a recording `sudo` stub is never invoked across the whole run |
| Success 2 | `focused-test`: `test_wipe_reaps_a_system_endpoint_under_sudo` — the stub's log carries the `list`, the `undefine`, and the `rm` |
| Success 2 (probe) | `focused-test`: `test_wipe_refuses_an_endpoint_the_operator_cannot_reach_even_when_sudo_can` -- a system endpoint that answers root and refuses the invoking account is refused at the gate, with an empty teardown event log, the overlays still present, and the escalation log never created |
| Success 3 | `focused-test`: both arms assert over the whole run, so the reap's enumeration and the end-state re-read are inside the assertion on either branch; the gate's probe is deliberately outside it, being unescalated on both branches by Success 2. The non-session branch's overlay residue is asserted as stated, not as absent: `test_wipe_reaps_a_system_endpoint_under_sudo` requires the `rm` in the escalation log while the surrounding tests stay the shell's. The writability precondition adds four arms: `test_wipe_refuses_an_unwritable_overlay_directory_before_dropping_the_volumes` (refused with an empty teardown event log, so the refusal precedes the volume drop), `test_wipe_reaps_an_unwritable_but_empty_overlay_directory_cleanly` (the non-empty-glob condition — a clean reap is not graded as a failure), `test_wipe_keeps_sweeping_an_unwritable_overlay_directory_on_a_system_endpoint` (branch-local, so the escalating branch is not refused), and `test_wipe_refuses_a_readable_but_non_traversable_overlay_directory_at_the_gate` (`0400` expands the glob, so it is caught here rather than at the sweep) |
| Success 4 | `focused-test`: the existing `test_wipe_*` arms, run unmodified |
| Success 5 | `task-test-not-applicable`: operator prose with no executable consumer; a test searching for wording would assert nothing about behaviour |
