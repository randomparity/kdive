# Worker-owned authority sender construction

## Problem

Worker handler assembly calls factories in provider assembly, which import the concrete worker
sender back from jobs. Construction crosses the same boundary twice even though provider
composition already accepts a sender and sender factory through neutral typed ports.

## Scope

Move `authority_sender_factory` and `local_authority_sender_factory` unchanged from
`providers/assembly/authority.py` into existing `jobs/assembly.py`, then remove the old module.
Worker assembly already owns the incarnation credential and injects these values into provider
composition. Concrete Unix/network transports and binding/TLS validation remain in providers;
`jobs/authority_sender.py` remains the neutral encoding adapter. This aligns construction with
[ADR-0606](../../adr/0606-resource-bound-provider-authority-network-route.md) and
[ADR-0654](../../adr/0654-provider-owned-authority-capability.md), without a new policy decision.

Preserve parameter names/order/kinds/defaults, resolved type hints and executable bodies, including
nested closures, optional local binding/configuration and disabled `None`, lazy per-call TLS and
encode-time credential borrowing. The destination already uses future annotations, so raw internal
annotation representations may differ. Add no annotation machinery. Preserve `build_worker_handler_assembly` and provider composition behavior.
The imported transport/TLS/binding names become worker assembly globals; migrate test patch sites
there. Do not add a forwarding module or move concrete transports into the neutral sender.

Migrate direct imports in worker assembly and its tests, local/remote sender tests, the network
proof helper, and the installed local proof's embedded client and unit-test patch owner. These
internal Python imports are not protected public APIs under ADR-0041 decision1. Preserve all test
assertions; the existing timing and identity tests remain the behavioral proof. Historical dated
TLS-renewal design citations describe their source revision and remain historical evidence.

### Failure model

- Actors: worker assembly creates local/remote Resource-bound authority routes; installed proof
  clients use the same factories. Assets: selected binding, current credential, call-local TLS,
  neutral provider injection and usable cold imports before process assembly.
- Invariants: no credential/TLS copy at construction or rebinding, unchanged fixed destinations,
  unchanged disabled-local and invalid-binding behavior, no worker imports from provider assembly.
- Accepted: internal old module imports are removed after in-tree callers migrate; no supported
  public MCP/wire/schema surface changes. Dispatcher, transport, authz and lifecycle behavior remain
  their existing contracts. No live host/VM or hardware result is claimed by local proof tests.
- Other owners: runtime-builder consolidation, inventory discovery, journal projection and boot
  artifact staging remain independently queued. This move does not depend on changing their logic.

## Success

1. Worker assembly is the sole factory owner; provider assembly has no direct import into jobs.
2. Both factory ASTs, worker registration/credential wiring, neutral sender and provider composition
   behavior remain unchanged. Every production/test/embedded helper caller uses the new owner.
3. Existing sender and assembly tests pass; the actual wheel contains the new owner and no old
   factory module, and cold imports work in both provider/worker import orders without startup.

## Validation

Add one structural AST guard to the existing authority boundary test file. Demonstrate red on the
current provider-to-jobs edge, green after relocation and red under a controlled reintroduced
edge, restoring exact bytes. Retain existing neutral guards and caller assertions. Run real local
TLS sender tests, remote sender timing/materialization tests, worker/provider assembly tests and
the fully mocked embedded-client unit case; execute no live provider or systemd operation.
Compare moved factory ASTs and original caller assertions after import/patch migration. Verify
unchanged worker functions, neutral sender, concrete transport/model code and immutable SQL.
Build/install the actual wheel, verify module origins/bytes and absent legacy module, and cold
import both assembly orders in fresh processes. Run lint, whole-tree types, docs and staged hooks,
then bounded fresh code/security reviews before local commit and exact health attribution.
