# Remove the unused changelog credential (#2624)

## Problem and authority

PR #2361 closed #2337 and deleted the changelog-sync workflow. Debt 0012 tracks
the remaining credential and branch-protection bypass. The operator authorized
their removal on 2026-09-21; the frozen scope is issue #2624's `q2624-9a672efd` annotation.

## Scope and success

Remove repository Actions secret `CHANGELOG_DEPLOY_KEY`, the write-enabled deploy
key exactly titled `changelog-sync (auto)`, and the sole `DeployKey` bypass actor
from active ruleset 19143912, `protect main`. Recheck identities immediately before
each mutation. Stop on a different identity, changed configuration or ambiguous response.
Verify both credential inventories exclude the named objects and the ruleset equals
its captured configuration with only that actor removed. Preserve its pull-request
requirement, merge/rebase methods, deletion/force-push prohibitions, required checks,
conditions, enforcement and other controls.

Then resolve Debt 0012 with a dated banner and evidence, preserving its historical
body. Update the pending-cleanup paragraph and the stale claim that merging the
post-release bump triggers changelog synchronization in the release guide.
No ownership transition is needed: repository administrators own the settings,
and the workflow consumer was already removed. No replacement credential is created.

## Global constraints

Change only the three authorized GitHub settings, Debt 0012, release documentation
and this required design artifact. Keep the implementation plan private and uncommitted.
Do not alter other repository settings (owner: repository administrators) or release
workflow behavior (owner: separately authorized issue). Publish no secret material,
private identifiers or raw administrator responses. Use existing GitHub CLI and jq.

## Approach

Use the existing administrative API to remove the two exact credential objects and
update only `bypass_actors` in the captured ruleset. Compare the full configurable
ruleset before and after, excluding server-owned metadata. A partial PATCH-style
update is preferable to reconstructing rule objects: reconstruction risks omitting
controls ([GitHub update API](https://docs.github.com/en/rest/repos/rules#update-a-repository-ruleset)).
Keeping the unused credential does not meet #2624. No architecture changes
or new ADR are needed; ADR-0633 already settled consumer removal.

Capture private before/after metadata without key material. After each operation,
read back its target; after the final operation, verify the complete three-object
postcondition and unchanged remaining ruleset. Deletions cannot be rolled back from
Git: on failure report the observed partial state and stop for operator reconciliation.
Do not recreate keys, restore a bypass, or repeat an ambiguous mutation automatically.

## Failure model

- Actors and deployments: an authorized repository administrator using GitHub API;
  ordinary PR authors and GitHub Actions on this repository.
- Invariants and assets at stake: exact credential identities; the controls and
  conditions of ruleset 19143912; truthful documentary resolution after live proof.
- Accepted failure classes: an API failure may leave a partially completed cleanup;
  it is reported and held for reconciliation, never documented as complete.
- Covered elsewhere: future repository administrator changes are owned by repository
  administrators; workflow source regressions are bounded by the existing no-push guard.

## Threat model

- Boundary inventory: administrator commands cross into GitHub configuration; private
  metadata becomes public documentary evidence. No boundary is added or widened.
- Actor model: trust the expressly authorized administrator; a holder of the unused
  key loses repository write capability. Concurrent administrator edits are not assumed safe.
- Controls: exact identity checks, fresh readback, minimal update payload, semantic
  preservation comparison, and redacted public summaries. A changed pre-state stops work.
- Out of scope: other credentials and repository settings remain administrator-owned.

## Validation

Live JSON assertions first fail because the three targets exist, then pass after
removal; compare the ruleset against the captured baseline minus the authorized actor.
Run the existing no-default-branch-push guard and document/record guards. Verify prose
against the live result and #2361; do not manufacture tests that snapshot its wording.
The installed pre-push hook owns the required full `just ci` run. CI and a fresh
mergeability readback establish PR handoff; no VM or release execution is relevant.
