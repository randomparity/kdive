# 0703 — Preflight guestfs recovery xattrs

## Status

Accepted (2026-09-27)

## Context

Issue #2852 permits byte-exact restoration or refusal before mutation. The guestfs Python
`lsetxattr` binding takes text, while recovery archives contain arbitrary bytes. Direct calls
with bytes, embedded-NUL strings and surrogateescaped non-UTF8 fail before reaching the daemon.
The source tree must not be partially restored before discovering this known incompatibility.

## Decision

Validate every authenticated archive entry through the concrete tree's binding-specific
`prepare_restore` operation before that operation creates the staging root. Then populate the
same validated archive. Accept non-SELinux values only when strict UTF-8 without NUL; pass text
and the original byte length. Preserve the existing terminal-NUL SELinux-context conversion.
Unsupported values raise an actionable error without echoing guest metadata. Keep capture bytes
and persisted archive formats unchanged. Installation keeps its current preparation path.

## Considered & rejected

- **Keep passing bytes.** measured: a direct installed-binding `lsetxattr` call with
  `b"a\0\xff"` raises `TypeError`, reproducing the issue.
- **Decode arbitrary bytes with Latin-1 or surrogateescape.** measured: surrogateescape raises
  `UnicodeEncodeError`; judgment: Latin-1 followed by the binding's UTF-8 encoding cannot
  preserve arbitrary bytes.
- **Add a separate binary transport.** judgment: a guest command or tar transport expands this
  privileged seam and its prerequisites; the issue explicitly permits an early refusal.

## Consequences

Some archives require operator remediation rather than automatic restoration. Rejection happens
before staging creation, leaving both the archive and guest module layout unchanged. A runtime
write failure after preflight keeps existing conflict behavior; partial staging can require
operator intervention. Retrying an unchanged unsupported capture cannot repair its metadata.
