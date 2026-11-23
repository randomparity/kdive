# App-local build finalization

## Problem and authority

A UUID-only process map can return another app's finalize result after authorizing the
caller in its own database. The two-database reproduction returned `succeeded` while
that caller's Run remained `created` with no build row. The user authorizes internal
architecture corrections while preserving public APIs; the current queue selects this
ownership seam. [ADR-0837](../../adr/0837-build-finalization-belongs-to-the-app-registration.md)
replaces only ADR-0675's internal task namespace, retaining its retry/cancellation behavior.

## Scope and ownership

Current owner: module `_IN_FLIGHT`, with fresh handlers created inside each request.
Intended owner: one `CompleteBuildHandlers` captured per complete-build registration.
Add its private dictionary with `field(init=False, default_factory=dict, repr=False,
compare=False)`. Lookup and insertion stay synchronous on the app loop. Move `_forget`
onto the instance, retaining the identity check and exception retrieval/logging.
Bind the callback with `partial(self._forget, uid)`; remove the obsolete globals.

Construct the handler before the tool decorator and capture it in the existing runtime
callback. Preserve runtime selection, authorization, caller cancellation and the task's
own pool connection. No compatibility path or new public field is needed.
The wrapper describes retries to the same server instance, independent validation by a
different instance and recorded replay; regenerate its tool reference and required mirrors.

The service's validation slot, post-slot recorded check, publication locks, upload-window
fences, result/error construction and measurement remain unchanged.

## Failure model

- Actors/deployments: HTTP apps on the normal single server event loop; tests/embeddings
  may assemble two logical apps with shared or independent pools/databases on that loop.
- Assets/invariants: app dependency isolation; same-app retry coalescing; validation work
  survives caller cancellation; completed/failed tasks clear their own entries; failure
  reporting stays singular; each database publishes one build for a Run.
- Accepted: process death loses in-memory work; retry can validate again. Different app
  registrations may validate independently, as replicas already can. Existing process-wide
  validation capacity and its one-loop hosting assumption are retained, not expanded.
- Covered elsewhere: service validation/publication transactions, artifact authenticity,
  window replacement/expiry and request authorization retain their existing owners/tests.

## Success

Two registered apps using the same Run UUID enter their own injected validators; a retry
on the first app joins its original task. Shared-database publication records one build;
separate databases each record their own build. Rejected/unauthorized requests, replay,
expiry/replacement and cancelled-waiter behavior retain their response contracts.

## Validation

Promote the real wrong-database reproduction into registered-app tests parameterized over
shared and separate databases. Use actual FastMCP wrappers and runtime authorization,
injecting only the current request context and constructor dependencies. Temporarily use
two validation slots in the test so blocked scans can overlap; restore the production value.
Release both validators and drain all request tasks in `finally`, including failure paths.
Create the second database with existing fixture-owned provisioning and role-lock helpers,
and drop only that fresh owned database in fixture teardown. Extend existing seed helpers
with an optional Run UUID rather than mock persistence or copy their model construction.

Run complete-build single-flight/tool/effective-config, service/measurement, adversarial
publication and tool-documentation suites with Docker required. Controlled faults must
catch shared task state, request-local factory construction and unguarded stale cleanup.
Rerun the original two-database reproduction after the fix. Verify service bytes and
unaffected handler ASTs; compare tool schemas, allowing only the scoped wrapper description.
Run lint, whole-tree types, generation/doc/resource/record guards and hooks. Review the
scoped code and security boundaries independently before closing this finding.
