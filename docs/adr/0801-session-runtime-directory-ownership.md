# 0801 — Session-runtime directory ownership

## Status

Accepted (2026-10-18)

Implements the operator-approved #3245 helper ownership amendment.

## Context

The shared session helper adopts an existing UUID-named directory with exist_ok=True
and suppresses deletion failures. Private proof receipts are written after preparation,
so a receipt cannot establish that the helper created the directory.

## Decision

The existing helper exclusively creates its short directory and captures a no-follow
UID/device/inode receipt before changing XDG_CONFIG_HOME. Restore returns the prior
environment first, removes only its still-matching empty directory, and reports incomplete
cleanup. Existing callers retain both primary and cleanup error context. The private
consumer uses that creation receipt and accounts for failure to persist its own receipt.

## Consequences

Collisions fail without retry or adoption. Uncertain capture or foreign/replaced/nonempty
paths remain for diagnosis. Non-session behavior and the short-path contract are retained.
This receipt is not atomic protection against arbitrary concurrent same-UID renames.
No guest execution, domain or disk ownership semantics change.

## Considered & rejected

- Keep exist_ok adoption/post-hoc receipt. verified:
  `git show f30fc1967ac9b0cec9f1b3a30ebaf4e016a5232b:src/kdive/testing/live_vm.py`
  shows exist_ok=True and unconditional suppressed rmdir. The private proof consumer records
  only after prepare returns. Neither establishes creation ownership.
- Add a separate runtime manager or nested-session protocol. judgment: unnecessary machinery
  for the existing one-session-boot-per-process contract.
