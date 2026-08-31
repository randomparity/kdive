# Local external-boot operation session implementation plan

Goal: add the narrow operation-scoped host capability defined by ADR-0587 while leaving six-port
wiring and capability advertisement untouched. Python 3.14 is used on x86_64 and ppc64le; no new
dependency or migration is permitted.

## Global constraints

- Require and retain a pin on a live nominal operation lease issued only after System ownership and
  the per-System serialization lane are held; reject missing, released, or foreign leases before
  host resource IO, and prevent lane release until every wrapper is poisoned and all closes have
  been attempted.
- Bind one libvirt domain/connection, exact overlay identity, artifact-root descriptor, and
  reopenable inactive libguestfs access for the operation lifetime.
- Expose no raw path, high-level external-boot operation, or advertised capability.
- Run `just lint`, `just type`, focused `just test-verbose <path>`, `prek run`, and pre-push
  `just ci`.

## Task 1 — Define and prove the session boundary

Files: create `src/kdive/providers/local_libvirt/lifecycle/boot/session.py`; create
`tests/providers/local_libvirt/lifecycle/boot/test_session.py`.

Interfaces: define immutable `ClosedDomainInspection` and `OverlayIdentity`; define
`LocalExternalBootOperationLease` binding System and activation ownership with a retained `pin()`;
define the narrow `LocalExternalBootSession` protocol and concrete session/factory. The factory
accepts `open(lease)` and an injected `open_artifact_root(lease) -> int`; the downstream adapter
will rely on its inspection, inactive fence, descriptor, guest, XML, power, readiness, observation,
cleanup, and close methods.

1. Write tests with fake lease, libvirt, descriptor, and guestfs handles for absent/released/foreign
   lease rejection before any resource open, pin acquisition before resource open, failed lease
   release while the session/guest is live, ownership validation, exact inspection identities,
   inactive fencing before guest mutation, guest reopen, and resource cleanup. Run
   `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session.py`; expect failures
   because the module does not exist.
2. Implement the smallest session and factory using existing domain naming, ownership metadata,
   overlay-path, safe XML, and ADR-0583 identity helpers. Keep external libraries behind injected
   factories. Run the focused test; expect all tests to pass.
3. Add fault tests for partial construction, overlay substitution, guest open/launch/close faults,
   cleanup ordering including poison-before-close and pin-last release, and use after close. For
   guest/domain/connection close faults assert the pin is released only after all close attempts and
   that neither session nor a retained guest wrapper can make another underlying call. Inject a
   controlled early-pin release and inactive-fence fault and confirm the mutation tests fail, then
   restore them. Run the focused test; expect all tests to pass.

Acceptance: every acquired wrapper is poisoned before cleanup, every underlying close is attempted
in dependency-safe order, retained wrappers cannot make another underlying call, and the pin is
released only after all attempts; non-fault paths close every resource. Mutation cannot occur while
active or against a changed overlay; inspection contains the exact closed identities; no high-level
lifecycle method or raw path is exposed.

## Task 2 — Expose a lazy internal builder without root policy

Files: modify `src/kdive/providers/local_libvirt/composition.py`; modify or create matching
composition tests under `tests/providers/local_libvirt/`.

Interfaces: add an internal factory builder accepting the explicit
`open_artifact_root: Callable[[LocalExternalBootOperationLease], int]` callback usable by #2144.
It binds the existing libvirt URI and overlay convention only. Do not add a root setting, attach
external-boot support to `ProviderRuntime`, or change `ProviderSupport`.

1. Write a composition test proving construction is lazy and support remains unadvertised. Run its
   focused path; expect the missing builder assertion to fail.
2. Bind the factory to the configured local URI and injected descriptor callback without opening
   libvirt, the callback, or libguestfs during runtime assembly. Run focused tests; expect all tests
   to pass.

Acceptance: #2144 can construct the internal session factory once it supplies its owned descriptor
policy, no resource opens at assembly, and local support contains no external-boot advertisement.

## Task 3 — Documentation and guardrails

Files: `docs/adr/0587-local-external-boot-uses-an-operation-session.md`, this spec, and this plan.

Run `just adr-status-check`, the focused tests, `just lint`, `just type`, and `prek run`. Re-read the
diff for scope and naming. Commit design and implementation as separate conventional commits.
