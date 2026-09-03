# 0009 — The libvirt target renderer NFC-gates its source but not its kernel and cmdline

## Status

Open
review-by: 2027-03-03

## Concern

ADR-0583 makes NFC a precondition of the preserved digest: "other character data is unchanged
and must already be NFC" in the paragraph defining the versioned two-part libvirt definition
identity. Both libvirt target renderers enforce that on one input only.

`render_target_xml` in `src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py`
normalization-checks `source` and raises `ValueError("domain XML must be NFC")` on a
decomposed document. It applies no such check to its `kernel`, `initrd`, or `cmdline`
arguments, and those are exactly the values it then writes into the document as character
data. Passing NFD text through succeeds silently: with the ADR's own third golden vector
decomposed to NFD, the renderer accepts it and the resulting boot projection digest is
`sha256:08e0a95327a4b36e98cbce130b9673a4c954eaef6f2e417c736416cfee4e9d7f` rather than the
published `sha256:06bf5b2aceb13f19b7debd17181ada54041d883f926c9c5f4c0acae4336f58fb`. The
remote copy, `preserved_definition_identity` and `render_target_xml` in
`src/kdive/providers/remote_libvirt/lifecycle/external_boot.py`, has the same shape.

The consequence is a rendered definition whose identity is stable and reproducible but is not
the identity the record publishes for that logical value. Two callers supplying the same
command line in different normal forms produce different state identities, and the
compare-and-set that activation performs against a recovery point reads those as different
states.

The scope is narrower than "the renderer violates ADR-0583", and the difference matters:

- `cmdline` is **explicitly exempt**. ADR-0583 states that `debug_cmdline` "and the derived
  `cmdline` are exempt from the serializer's NFC-input rule so the accepted scalar sequence is
  not rewritten". Rewriting a caller's command line is the thing that exemption forbids, so the
  gap for `cmdline` is a question of whether to *reject* non-NFC input, never whether to
  normalize it.
- `kernel` and `initrd` are host paths and carry no such exemption. They are the clearer half.

Whether the right correction is rejection at the renderer, rejection further upstream where
the plan is composed, or an ADR amendment recording the exemption's true extent is not settled
by the record as written.

## Why deferred

The change belongs to code this issue does not own. #2159 is a test-only change under an
explicit operator instruction to escalate rather than take any `src/` surface, and its charter
excludes the providers' implementation files. Adding a rejection to either renderer would also
be a behavior change on a path with no failing test to justify it, decided by the worker that
found it rather than by the decision that governs it.

It also lands more cheaply as part of work already scheduled. #2159's own second step
converges the local-libvirt and remote-libvirt copies of this algorithm into one shared module.
An input gate added now would be written twice and then merged; added at convergence it is
written once, in the module that owns the contract, and one set of tests covers both providers.

The alternative of tightening only local-libvirt now was rejected rather than deferred: it
would leave the two copies disagreeing about which inputs they accept, which is precisely the
silent divergence #2159 exists to stop.

## Non-regression boundary

- The three ADR-0583 golden vectors must keep reproducing against both copies while this record
  is open. `tests/providers/local_libvirt/lifecycle/boot/test_adr_0583_golden_vectors.py` and
  `tests/providers/remote_libvirt/lifecycle/test_external_boot.py` assert them independently,
  each against the published literals rather than against the other implementation.
- Neither renderer may start silently NFC-*normalizing* `cmdline`. ADR-0583's exemption exists
  so the accepted scalar sequence reaches the guest unrewritten, and the fresh-boot check that
  compares `/proc/cmdline` against the plan's bytes fails if anything rewrites them. Rejection
  is open; normalization is not.
- `render_target_xml`'s existing NFC check on `source` must stay. It is the half that is
  present and correct.
- The comment beside `_GOLDEN_KERNEL` in the local golden-vector module names this record, so a
  reader who notices the non-ASCII literals is not left to infer that the renderer validates
  them.

## What would resolve it

Decide where non-NFC `kernel` and `initrd` input is refused, apply it once in the module
#2159's second step creates, and cover it with a test that passes decomposed text and expects
the refusal. Settle `cmdline` explicitly in the same change — either record that non-NFC
command lines are accepted by design, which is the reading ADR-0583's exemption supports, or
amend the ADR if they are not.

Done when a decomposed `kernel` argument cannot reach a rendered definition unnoticed, the
`cmdline` exemption's extent is stated somewhere durable rather than inferred, and this record
carries its resolution banner.

## Provenance

target: src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py
target: src/kdive/providers/remote_libvirt/lifecycle/external_boot.py
Found by the `$gauntlet` adversarial review of the #2159 branch on 2026-09-03, as a
non-blocking note on the golden-vector module's comment. The comment half was dispositioned
`accepted-fixed` — it had credited the renderer with a gate it does not have, and now says so
— and the code half is deferred here. Reproduced independently before recording: the NFD
digest above was computed against the shipped renderer.
tracker: #2159
