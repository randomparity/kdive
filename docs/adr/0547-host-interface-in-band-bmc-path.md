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
the trusted side of that interface. Reaching the BMC means power control, and where the service
exposes them, virtual media and firmware write as well — which is state that outlives the run and
that [ADR-0541](0541-baseline-restore-or-cordon-teardown.md)'s teardown does not restore.
Teardown re-points a bootloader at a declared baseline kernel and verifies the host came back on
it. It has nothing to say about the service processor's own firmware or boot configuration.

Nothing on `main` reads, records, or checks this today: `docs/design/m4-byo-host.md` records the
M4 auth delta as "None" while its carried invariants and non-goals address only the
KDIVE-to-host direction, and no BYO code exists yet to read it.

Two facts constrain what can be decided about it.

**The setting belongs to the machine's owner.** Turning the Host Interface off, or giving it an
authentication mode, is a configuration write to the service processor.
[ADR-0540](0540-adopt-only-provisioning.md) makes `provision` adopt-only — no re-image, no
boot-order change, no firmware write — and the milestone lists firmware management as a non-goal
outright. The remedy is the operator's, made once against their own BMC, not something a run
performs and reverses. It is also a policy choice about the operator's own machine and their own
network, made with facts KDIVE does not hold, in a way "is the declared baseline kernel present"
is not.

**A check keyed on the Redfish resource would not be sound.** `AuthNone` was observed on one BMC.
The mode is permitted by the Redfish specification but is a vendor and configuration choice, so a
check must read `AuthenticationModes` rather than assume it. More than that: a BMC may expose no
`HostInterfaces` collection at all and still carry an unauthenticated in-band channel, because
IPMI over KCS is exactly such a channel and predates Redfish's model of it. A gate that failed a
host reporting `AuthNone` would fail the implementations that answer honestly and pass the ones
that answer nothing, which inverts the signal it is meant to carry.

Note what is **not** a reason here, because it reads like one. "KDIVE cannot remediate it, so
KDIVE should not fail on it" does not hold in this repository, and ADR-0540 is the counterexample
in this same milestone: a host whose declared `baseline_kernel` is absent from its bootloader
**fails to adopt** (`0540:111-117`), and the resolution there is likewise the operator updating a
declaration, not anything a run can perform. Unremediability decides nothing on its own — if it
did, it would equally excuse never failing on an unreachable OOB endpoint or a rejected
credential, which ADR-0539 does fail on. The two facts above are what carry this decision.

## Decision

**The host-OS-to-BMC path is read, reported, and recorded. It does not gate adoption.** KDIVE
stops treating the out-of-band plane as being outside the kernel under test's reach by
assumption, and starts carrying evidence about whether it is.

**Question 1 — do we accept that a kernel under test can drive its own BMC?** Yes. KDIVE will run
on such a host and will not refuse to adopt one. What it refuses is accepting it silently: the
acceptance is the operator's to make, against a value KDIVE read and recorded, rather than one
this project makes on their behalf by not looking.

There is no blanket answer for every host, because the reach is the same everywhere and the
consequence is not. Power is the part that costs least: a kernel that can power-cycle a host it
already holds exclusively (`concurrent_allocation_cap = 1`, ADR-0540) has that machine either way.
What differs is who bears the rest. Where the service exposes virtual media and firmware write,
reaching the BMC leaves persistent out-of-band state that survives the run and that teardown
cannot return to baseline — which an operator may accept on a machine they dedicated to this and
may refuse on one lent to them, or one KDIVE will hand to another project next. That judgement
belongs to whoever owns the machine, and this record does not take it from them. What it removes
is the option of the judgement going unmade: the run records what the service processor reported,
so the question has an answer at incident time rather than a plausible reconstruction.

**Question 2 — is disabling or authenticating the Host Interface an operator prerequisite?** It
becomes a stated term of the BYO environment contract, in the form of a decision the operator has
to have made rather than a check that has to pass. The contract reads: disable the host's in-band
service-processor channels, or give them an authentication mode, or accept that a run can leave
persistent out-of-band state on this machine. `doctor` (#1824) reports which of those holds; it
does not decide it.

**The check's verdict is `pass`, and the finding lives in the result's fields.** This is stated in
[ADR-0091](0091-doctor-diagnostics-model.md)'s vocabulary deliberately, because that record is
Accepted and its verdict is three-state — `pass`, `fail`, `error` (`CheckStatus`,
`src/kdive/diagnostics/checks.py:36-41`) — with no warning member, and `CheckResult.__post_init__`
raises `ValueError` when a non-`fail` result carries a `fix` (`checks.py:89-93`). A clause here
asking for a warning that names a remedy would ask for something that cannot be constructed, and
would amend an Accepted sibling from inside an unrelated record. `error` is not the substitute
either: it means the check could not reach a verdict, and it drives a nonzero exit, so it gates
the CI-style caller this decision declines to gate.

So: verdict `pass`, with the posture and its remedy stated in `detail` — which is what an operator
actually sees, because `kdivectl doctor` renders a fixed column set that does not include `data`
(`_COLUMNS`, `src/kdive/cli/commands/doctor.py:29`, applied at `:80`) — and repeated in
`CheckResult.data` for machine readers, which `ops.diagnostics` serializes into the envelope
(`src/kdive/mcp/tools/ops/diagnostics.py:202`). `data` is a `Mapping[str, str]`, so the mode list
is rendered there as a string. Putting the posture only in `data` would satisfy this record's
letter and defeat its premise: the acceptance is the operator's to make against a value KDIVE
read, and a value absent from the operator's own surface is not one they can make it against.
`detail` on `pass` is documented as "a short confirmation" (`checks.py:58-59`), so it is phrased
as a confirmation naming what was read, not as a bare remedy string.

The posture is one of five
values. Four come from reading `InterfaceEnabled` and `AuthenticationModes` on each Host
Interface, never assuming either: `disabled`, `authenticated`, `unauthenticated`, and
`not_reported` when the resource is absent or unreadable. **`not_reported` is recorded as itself,
never as `disabled` or `authenticated`** — a BMC that exposes no Host Interface resource has told
us nothing about its KCS channel, and flattening the two would be the one way this check could
report a falsehood.

The fifth value is `not_applicable`, and only the driver decides it. A PowerVM LPAR reached
through an HMC has no per-host service processor, so the question does not arise there and the
check does not invent a read for it. An IPMI-only x86 host is the opposite case: the question does
arise, because the in-band KCS channel is real, but it has no Redfish-shaped read — so that host
reports `not_reported`, not `not_applicable`. Keeping those two apart is what stops "we did not
look" from being recorded as "there is nothing to look at".

If a fourth `CheckStatus` member is wanted, that is an amendment to ADR-0091 and belongs in a
record of its own. Nothing here depends on it.

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

**This is the specific use ADR-0539 reserved, so no supersession is owed.** ADR-0539's own
decision text scopes the port to power and console and then says the rest of the surface "stays
outside this port **until a specific use justifies widening it**"
([ADR-0539](0539-out-of-band-control-port.md), Decision, "Two capabilities, both mandatory"). The
M4 contract restates the same reservation as "added only against a named caller". This record
supplies that use and that caller; it does not contradict a decided claim, so ADR-0539 keeps its
status and takes no supersession banner. The widening is exactly one read-only operation: `GET` the
Manager's `HostInterfaces` collection and its members. No write, no other member, and no standing
permission to read the rest of the surface #1816 surveys. The Manager is resolved through the
`Managers` collection rather than by hardcoding the `Self` identifier the observed BMC happens to
use, which the specification does not require. The response passes the ADR-0073 redactor like
every other out-of-band response, and the recorded fact carries the authentication *modes* only —
nothing from a `CredentialBootstrapping` object is read into it.

## Consequences

- The BYO environment contract gains a term an operator has to answer, and `doctor` gains a check
  that reports which answer the machine reflects. Neither refuses a host.
- #1824 owes the `doctor` check: read-only, an ADR-0091 `pass` verdict stating the posture and its
  remedy in `detail` and repeating the posture in `data`, `not_reported` never flattened into
  `disabled` or `authenticated`, `AuthenticationModes` read rather than assumed, and
  `not_applicable` on the HMC driver. It is already `status:blocked` behind #1823's precondition
  module, and this adds to its scope rather than unblocking it. ADR-0091's `CheckStatus` is
  unchanged by this record, and `doctor`'s `_COLUMNS` needs no change either — `detail` is already
  rendered.
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
- **Residual, uncovered:** an IPMI-only host's in-band KCS channel is recorded as `not_reported`
  and nothing compels an operator to look at it. The acknowledgement gate below would have covered
  it; the cost of that coverage is stated there.
- **Residual, uncovered:** the state a kernel can leave through this path persists across
  *successive* leases of the same host, which are KDIVE's own placement rather than the operator's
  BMC. `concurrent_allocation_cap = 1` makes one lease exclusive, and ADR-0541's restore compares
  the declared baseline kernel only, so nothing detects out-of-band state carried from one
  project's lease into the next. The operator's lever is the maintenance cordon (ADR-0541), taken
  by hand. This record accepts that rather than solving it.

## Considered & rejected

- **Accept it as a lab-host residual and record nothing.** The cheapest option, and defensible for
  a dedicated machine. Rejected because it leaves ADR-0539's premise — that the out-of-band plane
  is the recovery path because it sits outside the crashed kernel's reach — conditional on a
  property no record states and no run observes. An incident would then have to reconstruct
  whether the path existed, from a BMC whose state may have changed since.
- **Fail `doctor`, or refuse adoption, when a Host Interface reports `AuthNone`.** Precise about
  the machines that answer and unsound about the rest: it keys on a Redfish resource a BMC may not
  expose while still carrying an unauthenticated KCS channel, so it fails the honest reporters and
  passes the silent ones. Rejecting it says nothing about gating in general — see the next entry,
  which is the version of a gate this objection does not reach.
- **Gate on operator acknowledgement rather than on the read: refuse adoption until the
  declaration records an ack, required whenever the read is `unauthenticated` or `not_reported`,
  and record the ack as a fact beside the observation.** This is the strongest alternative here
  and it is not touched by the soundness objection above — it keys on the operator, not on the
  BMC's answer, so the silent KCS-only host is covered rather than left uncovered. It is also
  strictly more coverage than this record adopts, and rejecting it is a genuine trade rather than
  a free choice. Rejected on three grounds. **It is an admission decision and a declaration key in
  entry 2's inventory schema** (#1817), added by a record whose own deliverable is doc-only, for a
  property the run already reads. **The ack goes stale by construction:** it is given once at
  declaration and the posture it acknowledges is re-read every adopt, so an operator who
  acknowledged `unauthenticated` on a host later hardened, or acknowledged `not_reported` on a
  host whose BMC later answers `AuthNone`, has a live ack for a posture that no longer holds —
  and the ack cannot be re-demanded without an admission failure on a host that has not changed
  in any way KDIVE can attribute to the operator. **And the trigger set makes it a blanket
  key:** `not_reported` is the answer for every IPMI-only host and every BMC without the
  resource, which on a lab of pre-Redfish machines is most of them, so the ack becomes a field
  set once across the inventory and never revisited — the checkbox shape this repository already
  rejects for `[[byo_host]]` declarations below. The residual is stated plainly: an IPMI-only
  host's in-band KCS channel is recorded as `not_reported` and nothing compels an operator to
  look at it.
- **Have KDIVE fail adopt on `unauthenticated` the way ADR-0540 fails on a missing
  `baseline_kernel`.** The closest sibling, and the reason unremediability is not this record's
  argument. Rejected on what distinguishes the two: a declared baseline kernel is a statement
  about *KDIVE's own contract with the host* that KDIVE both requires and can check exactly, and
  its absence makes the run's teardown undeliverable. The Host Interface's mode is a policy
  choice about the operator's machine and network, judged with facts KDIVE does not hold — and
  the check for it cannot see the whole question (the KCS blind spot above), so the failure would
  land on the subset that reports honestly.
- **Have KDIVE disable the Host Interface at adopt and re-enable it at teardown.** It is a
  firmware configuration write on a machine KDIVE was asked to borrow, which is the operation
  ADR-0540 exists to refuse. A failed restore would leave the operator's service processor in a
  state KDIVE changed and cannot prove it changed back — ADR-0541's `restore_incomplete` case
  over firmware instead of a kernel, on a plane with no second path to recover through.
- **Add a `[[byo_host]]` declaration field — `host_interface = "disabled" | "accepted"` — and gate
  admission on it *instead of reading the machine*.** This is the substitution case, distinct from
  the acknowledgement gate above, which keeps the read and adds an ack on top of it. An operator's
  declaration about the host's own configuration is a claim, and preferring the read value over
  the claim is the substance of this decision; a host declared `disabled` whose BMC reports
  `AuthNone` would be admitted on the wrong one of the two, and nothing would ever catch the
  disagreement. It also puts a declaration key into entry 2's inventory schema in place of a
  property the same run can read directly from the machine.
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
