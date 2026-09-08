# 0012 — The changelog write deploy key and its protect-main bypass have no consumer

## Status

Open
review-by: 2026-11-08

## Concern

Deleting `.github/workflows/changelog-sync.yml` (ADR-0633, #2337) removes the only thing that ever
used three repository settings:

- the `CHANGELOG_DEPLOY_KEY` Actions secret, which held an SSH private key with write access;
- the `changelog-sync (auto)` repository deploy key, its public half; and
- the `DeployKey` bypass on the *protect main* ruleset, which is what let that key push straight
  to the default branch past require-PR and the required `lint · type · test` check.

After this change nothing reads the secret and nothing exercises the bypass, but all three remain
configured. A write credential that can push to a protected branch, and a standing rule exemption
that admits it, are now unattended: no workflow run will ever surface them again, and nobody is
watching a path nothing takes. That invisibility is the same property ADR-0633's Context names as
the reason the per-merge cost went unnoticed for as long as it did, applied this time to a
credential rather than to CI minutes.

The exposure is bounded and is not made worse by #2337 — the key and the bypass exist today and
are exercised on every merge, so removing their consumer strictly reduces use. What #2337 changes
is that the remaining configuration stops being self-evident.

## Why deferred

All three are GitHub repository settings, not files. A secret, a deploy key, and a ruleset bypass
can only be removed through the repository's settings by an account with admin rights; no change
to this repository's contents can do it, and no pull request can prove it was done. #2337 is a
code and documentation change and closes when it merges, so it cannot be the owner of a step that
must happen afterwards in a different system.

## Non-regression boundary

- No workflow or workflow-invoked script pushes to the default branch. Held mechanically by
  `tests/guards/test_no_workflow_pushes_to_default_branch.py`, which fails the `test` check on a
  reintroduction, and by ADR-0633's decision 1.
- No new consumer of `CHANGELOG_DEPLOY_KEY`, of the `changelog-sync (auto)` deploy key, or of the
  `DeployKey` bypass is added while this record is open. A change that needs one supersedes
  ADR-0633 rather than quietly re-using the credential.
- The protect-main ruleset keeps require-PR, the required `lint · type · test` check, no
  force-push, no deletion, and merge/rebase-only. Removing the bypass must not touch those.

## What would resolve it

The repository owner, in GitHub settings:

1. deletes the `CHANGELOG_DEPLOY_KEY` Actions secret (Settings → Secrets and variables → Actions);
2. deletes the `changelog-sync (auto)` deploy key (Settings → Deploy keys) — that string is the
   key's name in the settings UI, which is why `docs/development/releasing.md` still carries it;
3. removes the `DeployKey` entry from the *protect main* ruleset's bypass list (Settings → Rules →
   Rulesets → protect main), leaving the rest of the ruleset unchanged.

Done when the Actions secret list, the deploy-key list, and the ruleset's bypass list each show
none of the three, and a merge to the default branch still requires a pull request. Resolve this
record with a `> **Resolved by …**` banner naming the date the owner confirmed it, and drop the
"left with **no consumer**" paragraph from `docs/development/releasing.md`.

## Provenance

target: docs/development/releasing.md
target: docs/adr/0633-regenerate-the-committed-changelog-at-release-time.md
Raised by the #2337 `$trial-loop` / `$gauntlet` branch review on 2026-09-08 (pass 1, medium
finding: the branch shipped three "tracked separately" promises with no artifact behind any of
them). ADR-0633's Consequences states the same fact and explicitly disclaims ownership of it; this
record is the owner it names.
