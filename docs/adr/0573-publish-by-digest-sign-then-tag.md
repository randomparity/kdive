# 0573 — Publish by digest; sign; then tag

## Status

Accepted (2026-08-21)

## Context

`release-image.yml` pushed and signed in the wrong order: `Build and push` ran
`docker/build-push-action` with `push: true` and the full tag set, so on a `v*` tag the
digest and its `:X.Y.Z`, `:X.Y`, and `:latest` tags were live in GHCR the moment that step
returned — while `Install cosign` and `Sign the published digest` ran strictly later. Any
interruption in between (job timeout, runner loss, a Fulcio/Rekor outage, a manual cancel)
left a fully tagged release digest with no signature. Nothing untags it, nothing warns, and
`concurrency.cancel-in-progress: false` means a re-run does not supersede the run that left
it: on a tag build, `:latest` has already moved onto the unsigned digest (#1991).

The obvious cheap remedy — a failure/cancellation handler naming the stranded digest —
cannot cover the dominant interruption mode. When a job-level `timeout-minutes` fires,
GitHub terminates the runner; no later step runs, `if: always()` or otherwise. For a job
bounded at up to 300 minutes (ADR-0571), timeout is exactly the interruption to defend
against, so the ordering itself has to change.

## Decision

We restructure `release-image.yml` into **push by digest → sign → apply tags**:

1. **Push by digest only.** The build step runs `docker/build-push-action` with the
   documented push-by-digest export —
   `outputs: type=image,name=<image>,push-by-digest=true,name-canonical=true,push=true` —
   so buildx publishes an untagged manifest at an immutable `@sha256` digest and reports
   that digest in `steps.build.outputs.digest`. (A bare `push: true` with no tag does not
   work: buildx refuses it with "tag is needed when pushing to registry".) SBOM and
   provenance attestations attach here exactly as before (they are bound to the digest via
   referrers, not to tags).
2. **Sign the digest.** cosign keyless/OIDC signs `ghcr.io/randomparity/kdive@<digest>` —
   the same signing surface as [ADR-0088](0088-deployment-packaging.md) decision 8, which
   this record extends with an ordering it did not speak to.
3. **Then apply the human-facing tags.** One `docker buildx imagetools create` step copies
   every tag from `docker/metadata-action` onto the signed digest. Single-source create is a
   carbon copy of an index/manifest-list source, and `--prefer-index=false` preserves the
   single-platform manifest format on main pushes, so the tag lands on *exactly* the signed
   digest — no rebuild, no new manifest, referrers intact.

This supersedes nothing: [ADR-0088](0088-deployment-packaging.md) decision 8's surface
(what gets signed, keyless/OIDC, on GHCR via tagged-release CI) is implemented unchanged;
[ADR-0359](0359-multiarch-app-image.md)'s multi-arch releases and
[ADR-0572](0572-edge-builds-amd64-only-releases-stay-multiarch.md)'s amd64-only `:edge`
are untouched; the timeout bound remains [ADR-0571](0571-every-job-in-every-workflow-declares-a-timeout.md)'s.

## Consequences

- The invariant a consumer cares about now holds structurally: **a digest is reachable under
  any human-facing tag only after its signature exists.** An interrupted run leaves at most
  an unsigned, untagged digest that no release tag references — invisible to every
  `docker pull` / `cosign verify` path, which go through tags.
- The stranded artifact is still there (GHCR keeps untagged manifests), but it is inert:
  deleting it is optional hygiene on the package page, not incident response, and a re-run
  republishes under a fresh digest without colliding.
- `docs/development/releasing.md` states the actual guarantee instead of the unqualified
  "every published digest gets a signature" it claimed before.
- Tag application moves from the registry's implicit push-time tagging to one explicit step;
  a bug there fails loudly after signing rather than publishing silently unsigned tags.
- Verification instructions (`cosign verify … :X.Y.Z`) are unchanged: resolving the tag
  yields the same signed digest.

## Considered & rejected

- **Failure/cancel handler naming the stranded digest.** Cannot run after a job-level
  `timeout-minutes` fires — the runner is terminated, so no handler executes — and for the
  non-timeout cases it would only *report* a live unsigned `:latest` rather than prevent it.
- **Retry the sign step.** Covers transient Fulcio/Rekor blips only; a runner loss or
  timeout still strands a tagged unsigned digest.
- **Untag-on-failure cleanup step.** Same blindness to job-level timeouts as the handler
  above, plus it races the next queued run in the concurrency group.
- **Per-platform jobs feeding a manifest-merge job with signing at merge.** Already weighed
  and rejected in [ADR-0572](0572-edge-builds-amd64-only-releases-stay-multiarch.md) for
  cost reasons; this issue is about ordering, not shape.
