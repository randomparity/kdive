# Real server-secret presence proof

## Status

Proposed for #3155; implementation waits for whole-set review and scope audit.

## Problem

The operator carrier checks only that `secrets.list` returns allowed labels and no
configured values, then records BLOCKED. An empty answer satisfies its positive
assertions. Four functional cells (default/recovery × direct/gateway) therefore
lack a real server-side positive control.

## Scope and architecture

Extend the existing live carrier, its direct helper and focused tests. Keep the
production registry and APIs unchanged. Exclude diagnostics #3154 and new secret
APIs/backends. The existing local service lane remains Fedora 44 on x86_64; this
proof does not discharge native POWER obligations.

Use an owned catalog System and a verified immutable longterm kernel fixture.
Reuse `on_catalog_system`, `load_fixture`, `build_and_upload_kernel`, and ordinary
HTTP investigation/Run/install/boot calls. A successful HTTP
`debug.start_session(transport="drgn-live")` loads the System bootstrap private
key into the server registry through `_seed_bootstrap_key` and
`load_system_bootstrap_private_key`. Worker-side provisioning alone cannot prove
server registration. Do not run introspection scripts or change debug behavior.

Each functional cell requires an otherwise idle, newly started server whose real
HTTP `secrets.list` answer is exactly empty. A dirty baseline is BLOCKED with a
restart instruction before fixture creation. After the real successful attach,
require exactly `["<process-global>"]` through the cell's own direct or gateway
caller. Reject an empty answer, extra labels, duplicates and unordered labels.
Read the owned System's actual bootstrap key using the existing evidence DSN in
a read-only transaction only to check that its value is absent from the serialized
answer. Also retain the configured-secret leak checks. Persist only boolean or
count results, source kind and opaque owned identifiers; never key material.

A dedicated non-collected `live_stack/secret_presence.py` helper owns this fixture
sequence. The collected operator module dispatches only secrets functional cells
to it; all rejection cells and other operator tools retain their existing paths.
No collected test module becomes an import dependency. Durable terminal history
is expected: the read-only project snapshot wrapper does not enclose fixture
creation. `on_catalog_system` supplies actual capacity/domain/disk cleanup.

## Lifetime and cleanup

End the debug session and close its investigation in `finally` blocks. Release
the allocation through the existing frame and verify owned domain/disk absence,
capacity restoration, terminal session and deletion of its bootstrap-key DB row.
Do not delete shared catalog images or immutable kernel inputs. Failed setup still
runs the frame's owned cleanup and cannot produce a passing effect assertion.

The server registry deliberately retains process-global secrets until process
exit; `release(None)` is a no-op. After resource cleanup, assert the global label
remains and record that lifetime truthfully. The isolated live execution procedure
stops the owned server, verifies its PID has exited, starts a new exact-candidate
server, and verifies an empty HTTP answer between cells and after the final cell.
Keep those process-bound cleanup artifacts alongside the four cell records;
resource cleanup alone must not be described as removal from a running registry.
No registry clear hook, new endpoint or synthetic response is introduced.

## Success

- Each of the four named functional cells passes over real HTTP with successful
  source registration, exact expected labels and zero known-value leaks.
- An empty registry or permanently empty response fails the positive assertion.
- Owned session, System, allocation and bootstrap-key cleanup are observed; global
  registration persists until the isolated server exits, then a fresh process is empty.
- Evidence binds the actual three deployed role SHAs, image digest and immutable
  kernel manifest. A missing fixture or failing debug prerequisite remains a
  recorded blocker/failure; it is not replaced by a fake registry operation.

## Failure model

- Actors and deployments: trusted operators running the native local live stack;
  both supported tool configurations and direct/gateway exposures on x86_64.
- Invariants and assets: real server ownership, exact presence, secret non-disclosure,
  existing authorization, owned guest cleanup and truthful process lifetime.
- Accepted limitations: no POWER qualification, no remote-provider proof, no
  introspection/DWARF qualification, no new APIs/backends or diagnostics fixes.
- Required failures: dirty baseline blocks before mutation; missing image/kernel
  blocks; attach/install/boot failure retains evidence and cleans owned resources;
  leaked value, missing label or failed cleanup fails the cell.

## Threat model

- Boundaries: existing authenticated HTTP calls, existing read-only evidence DB,
  private kernel/upload scratch and operator-owned process lifecycle. No added or
  widened production trust boundary.
- Actors: trust the fixture operator, deployment and immutable inputs; do not trust
  a tool response to attest its own contents or a worker registry to attest the server.
- Controls: existing RBAC and direct/gateway callers; exact label comparison;
  count-only leak diagnostics; read-only DB access; private temporary scratch;
  cleanup of only IDs created by this cell; no secret-containing assertion output.
- Out of scope: compromised operator/host, unrelated tenants, production registry
  redesign and excluded campaign work. Existing auth rejection cells remain intact.

## Validation

Focused tests cover exact positive/negative labels, dirty-baseline refusal,
value leakage without disclosure, successful attach ordering, exception cleanup
and retained-global semantics. Boundary fakes are unit evidence only. Controlled
empty-response and skipped-registration faults must make the focused assertion red.
Run relevant lint, whole-tree type and documentation checks, then the installed
pre-push gate. Before live changes verify Ubuntu host prerequisites and source
identity; stage the existing Fedora lane image and immutable kernel without a
rebuild. Record the current carrier's real BLOCKED result, deploy the candidate,
run the four cells with isolated server lifetimes and retain cleanup evidence.
A live prerequisite defect is routed to its existing owner or returned unfiled;
no speculative new issue or unrelated repair is part of #3155.
