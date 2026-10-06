# Binary xattr recovery (#2852)

## Authority and outcome

Frozen charter: issue #2852, scope token `q2852-b18adf40`,
[scope annotation](https://github.com/randomparity/kdive/issues/2852#issuecomment-5860485737).
The operator approved the empty exclusion set on 2026-09-27 and required applicable x86 live
proofs. Unsupported captured attributes must fail before guest-tree mutation; supported values
must remain byte-exact. Existing valid SELinux context handling remains supported.

## Evidence and choice

The installed guestfs Python binding accepts only `str` for `lsetxattr`. An actual call with
`bytes` raises `TypeError`; embedded NUL in `str` raises `ValueError`; surrogateescaped non-UTF8
raises `UnicodeEncodeError`. UTF-8 text reaches the guestfs operation. Converting arbitrary
bytes through Latin-1 would change their UTF-8 representation. Reject values that cannot pass
this binding exactly, with an instruction that the retained capture requires operator-assisted recovery; retrying
the unchanged capture cannot fix unsupported metadata.
No new binary transport, shell command, dependency, or persisted format is introduced.

## Design

`LibguestfsAuthenticatedGuestTree` owns binding-specific metadata validation. Add
`prepare_restore(entries: Iterator[GuestTreeEntry]) -> None` to the internal tree protocol.
The concrete method verifies mutability, validates every entry's xattr value, then creates its
bound staging root. `RealGuestRecoveryWriter.restore` calls it after archive authenticity,
shape, manifest, count and size validation and before population. Remove the earlier recovery
`publication.create_staging()` call. Installation retains its existing staging creation.
The archive is still authenticated once and replayed from its same private temporary file.
This moves recovery staging creation behind the complete metadata preflight; publication,
renames, state transitions and durable metadata are otherwise unchanged.

One `_xattr_text(name: str, value: bytes) -> str` conversion serves preflight and replay.
For `security.selinux`, keep the current nonempty UTF-8 context with exactly one terminal NUL.
For other attributes, accept strict UTF-8 containing no NUL (including empty and multibyte
text), and reject invalid UTF-8 or any NUL. Replay passes the returned text and the original
byte length. The session's lsetxattr protocols and adapter take `str`, reflecting the binding.
No captured attribute bytes or guest paths are included in unsupported-value errors.

## Failure model

Covered deployment: local-libvirt recovery with the Python guestfs binding. All captured entries,
including an unsupported final member after valid earlier members, are validated before mkdir,
upload, chmod, chown, symlink or xattr writes. Invalid archive identity/digest/manifest still
fails before this preparation. Existing absent-tree recovery removes the tree as before.
Ordinary runtime I/O failures after a successful preflight retain existing staging/publication
behavior, including partial-staging conflicts requiring operator intervention. This change
does not promise atomic filesystem I/O.
Malformed SELinux contexts are rejected in the same preflight. An unsupported legacy capture
remains retained and the current guest tree remains unchanged for operator remediation.

## Validation

- Unit regression: restore archive with a valid first entry and binary final attribute; no
  staging root or entry mutation occurs, and error gives a recovery action.
- Unit cases: non-UTF8, embedded/trailing NUL, empty, ASCII, multibyte UTF-8; valid and malformed
  SELinux contexts; directory, regular and symlink restore paths; read-only capability refusal.
- Existing recovery authenticity and crash-ordering tests continue to pass.
- Real binding on x86-1: disposable guestfs appliance and scratch ext4 image, actual binary
  setter rejection, successful supported values read back byte-exact, archive-wide unsupported
  restore leaves staging absent. Run exact candidate revision and report passed arms/exits.
- Target policy remains x86_64 and ppc64le. This binding contract has no host-width-dependent
  operation; report native ppc64le proof as unrun and let the campaign route a necessary follow-up.
- Focused pytest, lint, whole-tree type checks, records/docs guards, then mandatory pre-push CI.
