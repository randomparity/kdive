# 0547 — Record the host's in-band service-processor path rather than gate on it

## Status

Proposed

## Context

[ADR-0539](0539-out-of-band-control-port.md) makes the service processor the recovery path for
an adopted host. Its premise is stated in one line of the M4 contract's carried invariants: the
out-of-band plane exists to work when in-band access is gone, so KDIVE never falls back in-band.
`force_crash` wedges the machine on purpose, and the BMC or HMC is what is left.

That premise assumes a direction. It says the operator's control path survives the kernel under
test. It does not say anything about the reverse direction, and the reverse direction exists.

`/redfish/v1/Managers/Self/HostInterfaces/Self` on a live AMI MegaRAC / AST2600 answered (#1847):

```json
{
  "Id": "Self",
  "InterfaceEnabled": true,
  "ExternallyAccessible": false,
  "HostInterfaceType": "NetworkHostInterface",
  "AuthenticationModes": ["AuthNone"],
  "Status": {"Health": "OK", "State": "Enabled"}
}
```

The Redfish Host Interface is the standardised in-band channel from a host's own OS to its
service processor. On that machine it is enabled and takes no credentials. `ExternallyAccessible:
false` bounds it to the host itself, and that bound is the whole of the protection.

KDIVE's position on this host is unusual. Epic #1814 adopts a machine an operator already owns
and then runs an operator-supplied kernel on it and crashes it deliberately. The kernel under
test is buggy by construction and, in a fuzzing context, may be attacker-influenced. It runs on
the trusted side of that interface. Reaching the BMC means power control, and on many
implementations virtual media and firmware write as well — which is state that outlives the run
and that [ADR-0541](0541-baseline-restore-or-cordon-teardown.md)'s teardown does not restore.
Teardown re-points a bootloader at a declared baseline kernel and verifies the host came back on
it. It has nothing to say about the service processor's own firmware or boot configuration.

Nothing on `main` reads, records, or checks this. `rg -ln HostInterface` over the repository
returns no matches, and `docs/design/m4-byo-host.md` records the M4 auth delta as "None" while
its carried invariants and non-goals address only the KDIVE-to-host direction.

Two facts constrain what can be decided about it.

**KDIVE cannot fix it.** Turning the Host Interface off, or giving it an authentication mode, is
a configuration write to the service processor. [ADR-0540](0540-adopt-only-provisioning.md) makes
`provision` adopt-only — no re-image, no boot-order change, no firmware write — and the milestone
lists firmware management as a non-goal outright. The remedy is the operator's, made once against
their own BMC, not something a run performs and reverses.

**A check keyed on the Redfish resource would not be sound.** `AuthNone` was observed on one BMC.
The mode is permitted by the Redfish specification but is a vendor and configuration choice, so a
check must read `AuthenticationModes` rather than assume it. More than that: a BMC may expose no
`HostInterfaces` collection at all and still carry an unauthenticated in-band channel, because
IPMI over KCS is exactly such a channel and predates Redfish's model of it. A gate that failed a
host reporting `AuthNone` would fail the implementations that answer honestly and pass the ones
that answer nothing, which inverts the signal it is meant to carry.

## Decision

**The host-OS-to-BMC path is read, reported, and recorded. It does not gate adoption.** KDIVE
stops treating the out-of-band plane as being outside the kernel under test's reach by
assumption, and starts carrying evidence about whether it is.

**Question 1 — do we accept that a kernel under test can drive its own BMC?** We accept that it
may, and we refuse to accept it silently. There is no blanket answer for every host, because the
consequence is not the same on every host: on a machine an operator dedicates to KDIVE, a kernel
that can power-cycle a host it already holds exclusively (`concurrent_allocation_cap = 1`,
ADR-0540) gains little from reaching the BMC for power; on a shared or loaner machine, the same
reach extends to virtual media and firmware, which is persistent out-of-band state that survives
the run and that teardown cannot return to baseline. That distinction belongs to the operator who
owns the machine, and this record does not take it away from them. What it removes is the option
of the distinction going unstated: the run records what the service processor reported, so the
question has an answer at incident time rather than a plausible reconstruction.

**Question 2 — is disabling or authenticating the Host Interface an operator prerequisite?** It
becomes a stated term of the BYO environment contract, in the form of a decision the operator has
to have made rather than a check that has to pass. The contract reads: disable the host's in-band
service-processor channels, or give them an authentication mode, or accept that a run can leave
persistent out-of-band state on this machine. `doctor` (#1824) reports which of those holds; it
does not decide it. The report has four outcomes — `disabled`, `authenticated`, `unauthenticated`,
`not_reported` — read from `InterfaceEnabled` and `AuthenticationModes` on each Host Interface,
never assumed. `not_reported` is reported as itself and never as a pass, because a BMC that
exposes no Host Interface resource has told us nothing about its KCS channel. The check reports
at warning severity, naming the specific interface and the remedy, and does not fail the host: a
gate KDIVE cannot remediate has one reachable resolution, an operator bypass flag, and a
suppressed report is worth less than an honest one.

Only the Redfish driver has this surface. A PowerVM LPAR reached through an HMC has no
per-host service processor and no analogue, so the check reports `not_applicable` there rather
than inventing one; an IPMI-only host reports `not_reported`, because the in-band KCS channel has
no Redfish-shaped read.

**Question 3 — does adopt record the state as a System fact?** Yes, unconditionally, whatever the
operator decided about questions 1 and 2. Adopt writes the observation into
`systems.byo_adopt_facts` — the jsonb column ADR-0540 already claims for what adopt established
about a live machine — as the per-interface enabled flag, the authentication modes, the
externally-accessible flag, and the time of the read, or an explicit `not_reported` when the
resource is absent or unreadable. It is a timestamped observation of what the BMC reported at
adopt, not a guarantee about the run. Its value is at incident time, when a vmcore accompanied by
an unexplained power event or a host that returns with different firmware needs to know whether
the path existed beforehand — a question a re-read after the fact cannot answer, because the
state may have changed in between. It is recorded per System rather than on the Resource's
declared capabilities because it is an observation dated to one adoption, not a declared host
fact. It is not a compared fact: ADR-0541's teardown continues to compare the baseline kernel and
nothing here, since a changed Host Interface state does not by itself mean a restore failed.

**This is the named caller the OOB port's non-goal requires.** The M4 contract scopes the ADR-0539
port to power and console and admits further surface "only against a named caller". The read
above is that caller, and it widens the port by exactly one read-only operation: `GET` the
Manager's `HostInterfaces` collection and its members. No write, no other member, and no standing
permission to read the rest of the surface #1816 surveys. The Manager is resolved through the
`Managers` collection rather than by hardcoding the `Self` identifier the observed BMC happens to
use, which the specification does not require. The response passes the ADR-0073 redactor like
every other out-of-band response, and the recorded fact carries the authentication *modes* only —
nothing from a `CredentialBootstrapping` object is read into it.

## Consequences

- The BYO environment contract gains a term an operator has to answer, and `doctor` gains a check
  that reports which answer the machine reflects. Neither refuses a host.
- #1824 owes the `doctor` check: read-only, warning severity, the four outcomes above,
  `not_reported` never reported as a pass, `AuthenticationModes` read rather than assumed, and
  `not_applicable` on the HMC driver. It is already `status:blocked` behind #1823's precondition
  module, and this adds to its scope rather than unblocking it.
- #1823 owes the `byo_adopt_facts` key, written by the same read at adopt.
- ADR-0539's port carries one more operation on the Redfish driver, and the IPMI and HMC drivers
  carry none. A driver that cannot answer the question reports that it cannot; it is not a
  reason to refuse the driver, because power and console remain the two mandatory capabilities.
- The M4 contract's "Auth / RBAC delta — None" stays true for KDIVE's own authorization surface
  and is qualified to say what it is a statement about: no new role, claim, or gate, and a
  boundary at the machine that the service processor's own authorization model sits outside of.
- A host with an unauthenticated in-band path stays adoptable. What changes is that its state is
  visible before allocation and recorded at adoption, so an operator who chose to accept it did
  so with the value in front of them.
- The recorded fact is an observation, not a control. Nothing prevents the Host Interface being
  enabled between adopt and the crash, and this record does not claim otherwise.
- Reading the interface costs one `GET` on a path an operator's BMC may answer with 404. That is
  a normal outcome here, not a transport failure, and it maps to `not_reported`.

## Considered & rejected

- **Accept it as a lab-host residual and record nothing.** The cheapest option, and defensible for
  a dedicated machine. Rejected because it leaves ADR-0539's premise — that the out-of-band plane
  is the recovery path because it sits outside the crashed kernel's reach — conditional on a
  property no record states and no run observes. An incident would then have to reconstruct
  whether the path existed, from a BMC whose state may have changed since.
- **Fail `doctor`, or refuse adoption, when a Host Interface reports `AuthNone`.** Precise about
  the machines that answer and unsound about the rest: it keys on a Redfish resource a BMC may not
  expose while still carrying an unauthenticated KCS channel, so it fails the honest reporters and
  passes the silent ones. It also fails what KDIVE cannot remediate — adopt-only (ADR-0540) and
  the no-firmware-management non-goal both refuse the write that would clear it — leaving an
  operator bypass flag as the only way past it, which turns a truthful report into a suppressed
  one.
- **Have KDIVE disable the Host Interface at adopt and re-enable it at teardown.** It is a
  firmware configuration write on a machine KDIVE was asked to borrow, which is the operation
  ADR-0540 exists to refuse. A failed restore would leave the operator's service processor in a
  state KDIVE changed and cannot prove it changed back — ADR-0541's `restore_incomplete` case
  over firmware instead of a kernel, on a plane with no second path to recover through.
- **Add a `[[byo_host]]` declaration field — `host_interface = "disabled" | "accepted"` — and gate
  admission on it.** An operator's declaration about the host's own configuration is a claim, and
  preferring the read value over the claim is the substance of this decision; a host declared
  `disabled` whose BMC reports `AuthNone` would be admitted on the wrong one of the two. It also
  puts a declaration key and an admission decision into entry 2's inventory schema for a property
  the same run already reads directly from the machine.
- **Leave it to #1816's survey and decide nothing now.** #1816 surveys what KDIVE could use from
  the out-of-band surface; this is what the host can use against KDIVE, which its charter does not
  cover, and it is a read-only investigation that produces findings rather than a record with
  normative force. This decision instead hands #1816 one narrowing: it names the single read the
  port's power-and-console non-goal now admits, so the survey does not have to re-open it.
- **Record the state in `resources.capabilities` beside the declared host facts.** That key set is
  written by the reconcile arm from the operator's declaration and is the sole writer of declared
  host-fact keys (entry 2), and it is long-lived per Resource. This is an observation made against
  a live machine at one adoption, which is what `systems.byo_adopt_facts` was claimed for, and
  keeping it per System dates it to the run that made it.
