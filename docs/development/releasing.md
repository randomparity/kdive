# Releasing

This guide owns version changes, release publication and published-image verification.
[ADR-0041](../adr/0041-versioning-release-process.md) defines the versioned public contract:
MCP tools and responses, error categories, and durable schema/state machines. In the `0.y.z`
phase, Milestones and incompatible contract changes require a minor bump; compatible fixes
and additions can use a patch bump.

## Version bumps

- **At a Milestone's start** — `just set-version <next-minor>` (e.g. `0.2.0` for M1), on a
  branch → PR → merge.
- **Immediately after a release** — open a `chore(release): begin <next>-dev` PR that runs
  `just set-version <next-patch>`, then `git fetch --tags origin` and `just changelog`. Both are
  **required**: the bump keeps `X.Y.Z-dev` meaning "ahead of the last release", and the regen is
  the one place the committed changelog is refreshed (see *Changelog generation* below).
  **Fetch the tags first** — git-cliff renders from the tags in *your* clone, and with the new
  `vX.Y.Z` tag missing it exits 0 and silently files the whole release under `[Unreleased]`.
  Check the diff shows a dated `## [X.Y.Z] - <date>` heading and an `[X.Y.Z]:` compare link in
  the footer. If neither appeared, confirm `git tag --list vX.Y.Z` lists the tag **and**
  `git merge-base --is-ancestor vX.Y.Z HEAD` succeeds — git-cliff walks HEAD's history, so a bump
  branch cut from an older `main` cannot see the tag and refetching will not help; rebase onto the
  tagged commit. If both already hold, the release had no changelog-visible commits (`cliff.toml`
  skips `chore`, `ci` and `test`) and the render is correct as it stands.

Merge the post-release version PR before any other normal PR to `main`.

The recipe updates `pyproject.toml`, `uv.lock` and the Helm chart's `appVersion` together,
without syncing the environment. Check with `just lock-check` and `just chart-version-check`.
The chart's own `version` is maintained separately when chart packaging changes; see
[ADR-0365](../adr/0365-helm-chart-version-independent-of-appversion.md).

The `release` and `chart-version-check` recipes currently require a checkout path without
spaces: their version-helper command expands the path without shell quoting.

## Cutting a release

Prerequisites: the repository tools (`just`, `uv`, Git), permission to push release tags,
and a clean, current `main` checkout at the intended release version.

1. Confirm the release commit's CI checks are green. `just release` checks branch, cleanliness,
   equality with freshly fetched `origin/main`, and version equality; it does not query CI.
2. Run `just release X.Y.Z`. It creates and pushes an annotated `vX.Y.Z` tag. It does not bump
   the version or run the test suite.
3. Check both independent workflows for that tag:
   [Release](../../.github/workflows/release.yml) builds the wheel and sdist with release/SHA
   metadata, generates notes, and creates or updates the GitHub Release and its assets.
   [release-image](../../.github/workflows/release-image.yml) publishes the container image.
   Both check the tag against the project version; neither replaces the pre-release CI check.
4. Merge the post-release version bump before other normal PRs. Its push triggers changelog
   synchronization, which uses the new tag to create the dated release section.

Check both publication results before treating the release as available: success in one workflow
says nothing about the other. A rerun can overwrite GitHub Release assets or move image tags.
Deleting a Git tag or GitHub Release does not withdraw the GHCR image or change a deployment.
For deployment changes, follow the relevant [operating guide](../operating/index.md).
The workflows do not publish to PyPI or a Helm chart registry.

## Container image publishing

The [app-image workflow](../../.github/workflows/release-image.yml) publishes to
`ghcr.io/randomparity/kdive` with these configured outputs:

| Trigger | Image tags | Platforms | Attestations |
|---|---|---|---|
| Push to `main` | `edge`, `sha-<short>` | `linux/amd64` | None |
| Release tag `vX.Y.Z` | `X.Y.Z`, `X.Y`, `latest` | `linux/amd64`, `linux/ppc64le` | SBOM and max provenance |

All these image tags can move, including commit-based tags on a rerun. `latest` follows the
most recently published release, even a backport; it is not a highest-version selector.
Select a digest for an exact build. POWER consumers need a multi-platform release image;
`edge` has no ppc64le image. Release builds use QEMU for the ppc64le build leg on an amd64
runner. The publishing job does not run the resulting image to prove runtime compatibility.

For both triggers, the job pushes by digest, signs that digest with keyless cosign/OIDC, then
applies tags ([ADR-0573](../adr/0573-publish-by-digest-sign-then-tag.md)). An interruption before
tag application can leave an untagged digest; the workflow never deliberately tags an unsigned
build. The signature covers the image digest; release SBOM/provenance are BuildKit attestations
attached to the index.

Consumers need registry access. If anonymous pulls are intended, the package administrator must
allow public access; otherwise authenticate with access to the package. Publication by this
workflow does not establish the package's visibility.

### Verify a release image

Use Docker Buildx and [Cosign](https://docs.sigstore.dev/cosign/verifying/verify/) with registry
and verification-service access. Set `RELEASE_VERSION` to the intended version without `v`,
then inspect its tag:

```bash
docker buildx imagetools inspect "ghcr.io/randomparity/kdive:${RELEASE_VERSION:?set release version}"
```

Set `IMAGE_DIGEST` to the top-level `sha256:...` digest shown above. Verify that exact image
against the workflow identity for the intended release tag, and deploy the same digest:

```bash
IMAGE="ghcr.io/randomparity/kdive@${IMAGE_DIGEST:?set image digest}"
cosign verify "$IMAGE" \
  --certificate-identity "https://github.com/randomparity/kdive/.github/workflows/release-image.yml@refs/tags/v${RELEASE_VERSION:?set release version}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Inspect the release attestations on that same digest with
[Buildx](https://docs.docker.com/reference/cli/docker/buildx/imagetools/inspect/):

```bash
docker buildx imagetools inspect "$IMAGE" --format '{{ json .SBOM }}'
docker buildx imagetools inspect "$IMAGE" --format '{{ json .Provenance }}'
```

Signature verification establishes signer identity and digest integrity. Attestation inspection
shows build metadata; neither check establishes that the image works in your deployment.

## Mock-OIDC mirror publishing

The [mock-OIDC image](../../deploy/mock-oidc/README.md) is development tooling with its own
publishing workflow, separate from KDIVE release tags. The
[`publish-mock-oidc` workflow](../../.github/workflows/publish-mock-oidc.yml) runs on pushes to
`main` that change `deploy/mock-oidc/**`, or on manual dispatch.

It publishes `linux/amd64,linux/ppc64le` to `ghcr.io/randomparity/mock-oauth2-server`, tagged
`<issuer-version>` and `<issuer-version>-<12-character-commit>`. Both tags can be republished;
select the `@sha256` digest from the workflow summary when pinning a particular build.
The workflow checks that the published index contains both architectures. It does not execute
the target runtime, sign the image, or attach SBOM/provenance attestations.

The build resolves JVM dependencies on the builder architecture and copies them onto the target
JRE; no QEMU build emulation is needed. The
[image-maintenance guide](../../deploy/mock-oidc/README.md#updating-the-image) owns the version,
checksum and base-index updates. Its [image-selection section](../../deploy/mock-oidc/README.md#using-the-image)
explains `KDIVE_OIDC_IMAGE`, cached local builds and the emulated-POWER default. The consumer
needs registry access for a published override. Authenticate as required by the package’s access policy.

## Commit conventions the changelog depends on

[cliff.toml](../../cliff.toml) groups conventional commits. Mark breaking changes with `!`
(such as `feat!: …`) or a `BREAKING CHANGE:` footer so they appear under Breaking Changes.
Use `fix(security): …` or `feat(security): …` for the Security group. Chore, test and CI
commits are skipped, including ones with a security scope.

## Changelog generation

`CHANGELOG.md` is **generated by git-cliff, never hand-edited.** It is regenerated **once per
release**, by `just changelog` in the post-release `chore(release): begin <next>-dev` PR
(above). The new `vX.Y.Z` tag exists by then, so git-cliff rolls `[Unreleased]` into a dated
`[X.Y.Z]` section.

Between releases the committed `[Unreleased]` section is **deliberately absent or stale** — it
reflects whenever it was last regenerated, not the tip of `main`. Straight after a release
regeneration there is no `[Unreleased]` section at all: the only commit past the tag is the
`chore(release)` bump, which `cliff.toml` skips. Run `just changelog` locally for the current view.
No automated consumer reads it: `release.yml` builds the GitHub Release notes with
`git-cliff --latest` straight from git history and never opens the file, so a release never
depends on the committed copy being current
([ADR-0041](../adr/0041-versioning-release-process.md)).

CI does not regenerate it. A workflow used to, pushing the regenerated changelog to `main` after
every merge; that moved the default branch a second time per merge and forced every other open PR
through a base refresh and a full CI cycle, which cost more than the freshness was worth
([ADR-0633](../adr/0633-regenerate-the-committed-changelog-at-release-time.md), #2337).

> **Branch protection.** `main` is guarded by the *protect main* ruleset (require-PR + required
> `lint · type · test`, no force-push/deletion, merge/rebase only — squash is blocked to keep
> `git bisect` history intact). Nothing in CI writes to `main`: every change arrives as a reviewed
> PR. The `CHANGELOG_DEPLOY_KEY` Actions secret, the `changelog-sync (auto)` repository deploy
> key, and the ruleset's `DeployKey` bypass that admitted it are left with **no consumer** —
> removing all three is a repository-owner step in GitHub settings, which no pull request can
> perform. It is tracked by
> [deferral record 0012](../debt/0012-changelog-write-deploy-key-and-bypass-have-no-consumer.md),
> not by #2337, which closes when this change merges.

## Version reporting

`python -m kdive --version` and the startup log report `X.Y.Z+g<sha>` for a release build and
`X.Y.Z-dev+g<sha>` for a development build. Runtime resolution uses baked `_buildinfo.py`
first, then live Git; without either it omits the SHA and reports a development build.
A checkout counts as a release only when clean at the exact tag matching its installed version.
`just build` bakes development metadata; the release workflow uses `just build true` and removes
the temporary stamp afterwards.
