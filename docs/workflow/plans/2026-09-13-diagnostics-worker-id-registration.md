# Diagnostics worker-vantage ID registration — implementation plan (#2344)

## Goal

Remove the result codec's duplicated provider worker-vantage allowlist while retaining strict
per-provider validation independent of provider configuration.

## Constraints

- Base branch: `main`; branch: `feat/diagnostics-worker-id-registration-2344`.
- Host architecture: x86_64; targets: none declared; relationship: no-target-declared.
- No provider-package import from `kdive.diagnostics.result_codec`.
- No descriptor fan-out or data-model change.
- Guardrails: `just lint`, `just type`, focused pytest, then `just ci`.

## File map

| Path | Change |
|---|---|
| `src/kdive/diagnostics/provider_contracts.py` | Add static worker-vantage IDs to the existing contribution contract. |
| `src/kdive/diagnostics/contributions/multiarch_gdb.py` | Register local static IDs. |
| `src/kdive/providers/remote_libvirt/diagnostics/contribution.py` | Register remote static IDs without configuration reads. |
| `src/kdive/diagnostics/service.py` | Pass each contribution's static IDs to its dispatcher. |
| `src/kdive/diagnostics/worker_dispatch.py` | Pass dispatcher IDs into codec reconstruction. |
| `src/kdive/diagnostics/result_codec.py` | Accept caller-supplied IDs and remove the duplicated catalog. |
| Diagnostics and job-handler tests | Exercise registration, dispatch validation, and codec behavior. |

## Tasks and contract evidence

1. Add `worker_vantage_ids` to `DiagnosticProviderContribution`, update all construction sites,
   and assert the contract shape. The local and remote contributions use check-ID constants; the
   remote construction does not read configuration. Focused provider-contract/default-factory tests
   prove the shape and static values.
2. Thread the static IDs through dispatch and codec reconstruction. The dispatcher rejects an ID
   outside its provider contribution while retaining the existing per-item error behavior. Focused
   codec/worker-dispatch tests prove this.
3. Replace the hand-curated codec guard with an assertion over production contributions. Perform a
   controlled fault by deleting one static ID, run the guard to red, restore it, then run it green.
4. Run `just lint`, `just type`, focused tests, self-review, and `just ci`; commit the design and
   implementation as one scoped change.

## Success mapping

| Criterion | Proof |
|---|---|
| One static registration authority | Production-contribution guard and controlled missing-ID fault. |
| Config independence | Remote contribution test with no configured instances asserts the full static ID list. |
| No codec provider dependency | Codec imports only diagnostics/domain modules; focused tests exercise injected IDs. |
| No behavior regression | Focused diagnostics/job-handler tests and `just ci`. |
