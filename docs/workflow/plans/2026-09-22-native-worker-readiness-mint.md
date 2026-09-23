# Plan: native worker readiness before System mint

Design: `docs/workflow/specs/2026-09-22-native-worker-readiness-mint.md`.
Estimate: 45–90 changed implementation and test lines. Fixed design denominator: 250 lines (M),
from #2657's approved scope assessment.

1. Remove the temporary operator-identity manifest probe. After stack bring-up, poll the fixed
   worker's loopback `/readyz` through the existing exact-shape filter for at most 10 attempts
   (2 seconds between attempts; each request capped at 4 seconds). Proceed only on `ready=true`.
   On failure, emit the last filtered readiness line or a fixed unavailable message and name the
   runner correction runbook. Leave the redacted persisted provision boundary step intact.
2. Add focused workflow-shape assertions for readiness-before-mint ordering, bounded polling,
   filter use, and the native no-privilege rule. Cover the filter's ready/unready and malformed
   response behavior with its existing focused tests.
3. Document that package replacement can invalidate a previously installed manifest and that
   the owning runner playbook regenerates and verifies it. Run focused tests, lint/type and the
   full pre-push gate, then dispatch the native KVM workflow at the final branch head. Record
   counts, skips, exit status, source and installed revision, and the run URL.

No deferrals in this issue. The separate `extract-vmlinux` warm-refresh prerequisite remains an
unfiled campaign follow-up candidate owned by native host provisioning.
