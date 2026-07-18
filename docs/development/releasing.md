# Releasing

This project follows [ADR-0041](../adr/0041-versioning-release-process.md): SemVer in the
`0.y.z` phase, milestone→minor, with the **in-tree version always pointing at the next
unreleased version** so a `-dev` build is never ambiguous across a release boundary.

## Version bumps (each via `just set-version`, which runs `uv version` to update `pyproject.toml` + `uv.lock`)

- **At a Milestone's start** — `just set-version <next-minor>` (e.g. `0.2.0` for M1), on a
  branch → PR → merge.
- **Immediately after a release** — open a `chore(release): begin <next>-dev` PR running
  `just set-version <next-patch>`. This is **required** — it is what keeps `X.Y.Z-dev`
  meaning "ahead of the last release." You no longer run `just changelog` by hand: merging
  this PR pushes to `main`, which triggers the changelog-sync workflow (see *Changelog
  automation* below). Because the new `vX.Y.Z` tag now exists, git-cliff rolls the
  `[Unreleased]` section into the dated released section automatically.

Never hand-edit the version: editing `pyproject.toml` alone desyncs `uv.lock` and breaks
`uv sync --locked` in CI. `just lock-check` (and CI) catch a stale lock.

## Cutting a release

1. Ensure `main` is green and `[project].version` already equals the version to release
   (it was bumped at Milestone start or by the previous post-release bump — **the release
   itself does not bump the version**).
2. From an up-to-date, clean `main`: `just release <X.Y.Z>`. This verifies state and pushes
   the annotated `vX.Y.Z` **tag only** (pushing a tag is not a commit to the protected
   branch).
3. `release.yml` triggers on the tag: it verifies tag == version, builds the wheel + sdist
   (commit SHA baked, `RELEASE=true`), generates notes from git-cliff, and creates an
   internal GitHub Release with the artifacts attached.
4. Open the post-release "begin `<next>`-dev" bump PR (above) and **merge it before any
   other PR to `main`**. Until it lands, `main` still reads the just-released version, so a
   commit merged ahead of it would report `X.Y.Z-dev` meaning "after" the release —
   reopening the ambiguity the scheme exists to prevent ([ADR-0041](../adr/0041-versioning-release-process.md)
   decision 3). Treat `main` as frozen for normal merges until the bump is in.

## Container image publishing

`release-image.yml` publishes the app image to `ghcr.io/randomparity/kdive`. It runs on the
**same `vX.Y.Z` tag** that drives `release.yml` (above) — so a single `just release` push
produces both the wheel/sdist GitHub Release and the release image — and also on every push to
`main`:

- **Every push to `main`** → a rolling `:edge` tag and an immutable `:sha-<short>`.
- **Every `vX.Y.Z` tag** → `:X.Y.Z`, `:X.Y`, `:latest`, with an SBOM and max provenance.
- **Every published digest** (main *and* tags) gets a cosign keyless/OIDC signature on the
  immutable `@sha256` digest ([ADR-0088](../adr/0088-deployment-packaging.md) decision 8).
  The SBOM and provenance are release-only; the signature is not.

**Multi-arch.** Each push builds a `linux/amd64,linux/ppc64le` manifest ([ADR-0359](../adr/0359-multiarch-app-image.md)),
so a POWER host pulls the same tag as an x86_64 host. amd64 builds natively; the ppc64le leg
builds under QEMU emulation on the amd64 runner (`docker/setup-qemu-action`), compiling the
sdist-only deps (`grpcio`, `drgn`, `libvirt-python`) from source — this is the slow leg of the
job, and no POWER runner is required. The cosign signature and SBOM cover the multi-arch
manifest digest.

**One-time setup — make the package public.** GHCR packages are created private on first
push and visibility cannot be set from the workflow. After the first `main` push publishes
`:edge`, set the package public once: GitHub → your profile → Packages → `kdive` → Package
settings → Change visibility → Public. Until then `docker pull` returns 404 to anonymous
clients and the chart needs an `imagePullSecret`.

**Verify a release image** (not `:edge`, which floats):
`cosign verify ghcr.io/randomparity/kdive:X.Y.Z --certificate-identity-regexp '^https://github\.com/randomparity/kdive/\.github/workflows/release-image\.yml@' --certificate-oidc-issuer https://token.actions.githubusercontent.com`

## Mock-OIDC mirror publishing

The developer compose stack (`docker-compose.yml`) needs an OpenID Connect issuer to validate
bearer tokens locally. The upstream `mock-oauth2-server` image ships only amd64/arm64, so kdive
publishes an in-repo multi-arch **mirror** — the same `no.nav.security:mock-oauth2-server` jar
on a multi-arch JRE base — to `ghcr.io/randomparity/mock-oauth2-server`
([#1184](https://github.com/randomparity/kdive/issues/1184), [ADR-0358](../adr/0358-publish-mock-oidc-image.md);
build in [ADR-0357](../adr/0357-multi-arch-mock-oidc-image.md)). This is **dev tooling, not a release
artifact** — it is decoupled from the `vX.Y.Z` tag flow.

- **Workflow:** `publish-mock-oidc.yml`, triggered only by a change under `deploy/mock-oidc/**`
  (a jar-version or base bump — exactly when a new digest must be produced) or manual
  `workflow_dispatch`. An unrelated push to `main` does **not** republish, so there is no digest
  thrash. It does not run on release tags.
- **Build:** a `linux/amd64,linux/ppc64le` buildx manifest published as `:<version>` and an
  immutable `:<version>-<short-sha>`. No QEMU is needed — the jars are arch-neutral and only
  copied onto a per-arch JRE base, so the ppc64le layer assembles natively on the amd64 runner
  (contrast the app image above). The workflow asserts the pushed manifest lists both arches and
  prints the `@sha256` digest in its run summary. No SBOM/provenance/signature — it is a
  dev-tooling mirror.
- **Consume:** the compose `oidc` service is `image: ${KDIVE_OIDC_IMAGE:-kdive-mock-oidc:dev}`
  with a `build:` fallback. Unset, it builds the mirror locally (offline, any arch whose bases
  publish); set to the published digest it pulls instead. After a republish, copy the digest
  from the run summary into `KDIVE_OIDC_IMAGE` and pin it in `docker-compose.yml`:

  ```
  export KDIVE_OIDC_IMAGE=ghcr.io/randomparity/mock-oauth2-server@sha256:<digest>
  ```

  This parallels `KDIVE_IMAGE` for the app image. See `deploy/mock-oidc/README.md` for the
  jar-pinning and version-bump procedure. The GHCR package needs the same one-time
  public-visibility flip as the app image for an unauthenticated pull.

## Commit conventions the changelog depends on

git-cliff categorizes from the commit message, so two cases need an explicit marker or they
are mis- or under-reported:

- **Breaking changes** (a renamed/removed MCP tool, a changed `ToolResponse` shape, a
  non-back-compatible migration — the contract in [ADR-0041](../adr/0041-versioning-release-process.md)
  decision 1) **must** carry a `!` (`feat!: …`) or a `BREAKING CHANGE:` footer. Without it
  the change lands only in its normal group and the `⚠ Breaking Changes` heading misses it —
  and a breaking change forces a **minor** bump, so this is load-bearing.
- **Security fixes** use a `(security)` scope, e.g. `fix(security): …`, which routes them to
  the Keep-a-Changelog `Security` group (a plain `fix:` goes to `Fixed`).

## Changelog automation

`CHANGELOG.md` is **generated by git-cliff, never hand-edited.** The `changelog-sync.yml`
workflow regenerates it on every push to `main` (running `just changelog`) and, when the
result changed, commits the delta straight back to `main` as a skipped `chore(changelog)`
commit — so the `[Unreleased]` section stays current with no manual step. The commit carries
`[skip ci]`, and the workflow ignores `CHANGELOG.md`-only pushes, so it never loops or burns a
CI run. `just changelog` remains the manual escape hatch (it is exactly what the workflow runs)
for previewing the changelog locally.

> **Branch protection.** `main` is guarded by the *protect main* ruleset (require-PR + required
> `lint · type · test`, no force-push/deletion, merge/rebase only — squash is blocked to keep
> `git bisect` history intact). A personal repo **cannot** grant the GitHub Actions integration a
> ruleset bypass, and a `GITHUB_TOKEN`-opened PR does not trigger the required CI, so the sync
> can neither push directly nor auto-merge under the default token. Instead the workflow pushes
> over SSH with a write **deploy key** (Actions secret `CHANGELOG_DEPLOY_KEY`) that the ruleset's
> `DeployKey` bypass admits — only that key bypasses; human pushes still go through PRs. The key
> does not expire; to rotate it, generate a new ed25519 keypair, replace the repo deploy key
> (`changelog-sync (auto)`) and the `CHANGELOG_DEPLOY_KEY` secret.

## Version reporting

`python -m kdive --version` and the startup log show `X.Y.Z+g<sha>` for a release build and
`X.Y.Z-dev+g<sha>` otherwise. The SHA/flag come from a baked `_buildinfo.py` in artifacts,
or live git in a checkout.

## Future toggles (not yet enabled)

- **PyPI publish** — add a `uv publish` step to `release.yml` after the GitHub Release step.
- **Signed tags / artifact attestation** — sign `vX.Y.Z` tags and attach provenance.

## Rollback

A release is a tag + a GitHub Release; it changes no `main` history. To withdraw one, delete
the GitHub Release and the tag (`git push origin :vX.Y.Z`), fix forward, and re-tag.
