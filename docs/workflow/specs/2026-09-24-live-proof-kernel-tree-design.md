# Live proof kernel tree report

## Problem

Live tests can inherit the local build convenience tree through `KDIVE_KERNEL_SRC`.
Their output does not identify the tree used, so a passing proof can be attributed to
the wrong checkout (issue #2749).

## Scope

The shared pytest header reports the absolute resolved path of `KDIVE_KERNEL_SRC`
when it is set, following the proof's literal path semantics for `~`. The existing
live proof entry points continue to consume that environment variable. The header
already owns live stack revision reporting;
extending it needs no ownership transition. In quiet mode, session startup prints
the same header lines that pytest would otherwise suppress. The local build
convenience default remains intact. Kernel content checks belong to #2750.

### Failure model

- Detection: An unset kernel variable produces no kernel header line; existing
  live proof prerequisite checks report the missing tree.
- Containment: Header rendering does not inspect kernel contents or change the tree.
- Recovery: Set `KDIVE_KERNEL_SRC` to the intended tree and rerun the proof.
- Accepted risk: This identifies the path, not the tree's config, source state,
  or provenance; #2750 owns those checks.

## Success

- A live pytest run with `KDIVE_KERNEL_SRC` set shows the resolved absolute path
  in its header, including when the value came from the local default.
- The path appears in quiet pytest output.
- The current local demo default and live proof tree consumption are preserved.

## Validation

- `focused-test`: Header unit test with a relative tree path confirms absolute
  resolved output and no path when unset.
- `focused-test`: Quiet session startup test confirms the kernel header line is
  printed through the terminal reporter.
- `task-test-not-applicable`: No live VM run; this change affects only pytest
  reporting, which the focused tests exercise without provisioning a guest.
