# One checked authority request decoder

## Problem

Authority transport decodes a request once, then independently checks operation/model pairing
in its encoder and receiver. The same fifteen guards must change together. The generic decoder
itself can return a valid model paired with the wrong operation; envelope callers currently
reject that mismatch correctly. This is duplicated validation ownership, not a proven wire bypass.

## Scope

Move pairing validation into the existing `_decode_operation_request` in
`providers/external_boot_authority/transport.py`. Its eight specialized branches already use
exact typed decoders and stay unchanged. Decode generic requests with `decode_authority_request`,
then return only for the seven existing model families and their nine supported operations:
takeover; mutation/observe-authority/observe-running; conflict resolution; preparation; teardown;
recovery-orphan disposition; and health. Otherwise raise `ValueError("invalid-request")`.
Use the existing explicit checks, without a new registry, dispatcher or codec framework.

Both envelope paths call this checked decoder. The encoder discards the result; the receiver
returns it. Delete their duplicate fifteen type guards. Keep receiver operation allowlisting,
canonical JSON, credential bounds, nesting/size checks and exception translation unchanged.
Keep dispatch type narrowing, service errors, authentication order, listeners and TLS unchanged.
Public `Operation` literals, callable signatures and valid wire bytes do not change. Unknown
operations remain rejected by the receiver and are rejected by the checked decoder; unsupported
values outside the typed encoder contract have no compatibility promise.

### Failure model

- Actors: trusted typed worker encoders and authenticated local/remote authority clients, including
  malformed frames. Assets: closed request compatibility, canonical wire bytes, worker credentials,
  pre-dispatch rejection and existing provider mutation boundaries.
- Preserve rejection before authentication/provider calls for malformed requests; authentication
  failure and provider-side errors retain existing behavior. No permission or credential lifetime
  changes, new operation or schema is introduced.
- Accepted: existing dispatcher type guards remain for local narrowing; eliminating them is not
  required to remove the two envelope ladders. Existing transport availability/timeouts and
  cooperative cancellation remain their established contracts, with no new convergence promise.
- Other owners: sender assembly, journal binding and provider staging retain their queued tasks.
  No host configuration, remote deployment or external write is needed for this local refactor.

## Success

1. One shared checked decoder rejects generic operation/model mismatches and unknown operations;
   eight specialized parsers retain their validation, with no parallel outer model ladders.
2. Encoder/receiver public shape and all valid canonical bytes remain unchanged. Malformed generic
   and specialized requests get the same bounded receiver response before auth/service calls.
3. Existing TLS, dispatch, credential, framing and service-error behavior remains unchanged.

## Validation

Direct checked-decoder mismatch tests establish the new ownership red before source changes;
existing valid bytes and envelope rejections remain regression proofs. Send canonical malformed
frames directly through the existing real mutual-TLS Unix fixture, bypassing the encoder, and
assert bounded invalid-request bytes with no authentication or service calls. Exercise both
encoder and receiver on generic mismatches and malformed specialized sweep requests.
Run the transport suite and related local/remote sender, protocol and worker assembly regressions.
Controlled missing-guard and skipped-decoder faults must fail new tests; restore exact bytes.
Compare unchanged transport AST outside the three functions, specialized branches and canonical
credential/receiver validation subtrees with the baseline. Build/install the actual wheel and run
focused cases with transport module origin/bytes verified. Run lint, whole-tree types, doc guards,
staged hooks, and fresh bounded code/security review before the local commit and two-ID closure.
