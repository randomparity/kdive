"""Guard: release-image pushes by digest, signs, then applies tags (ADR-0573, #1991).

`release-image.yml` used to push the fully tagged image first and sign later, so any
interruption between the two steps left a `:latest`-tagged unsigned release digest in GHCR
that nothing cleaned up and no re-run superseded (`cancel-in-progress: false`). No failure
handler can fix that: a fired job-level `timeout-minutes` terminates the runner before any
later step runs. The remedy is structural, so the guard is too.

These tests pin the ordering contract of the one job that publishes the app image:

1. the push step carries **no** `tags:` input — it publishes an untagged digest only;
2. cosign signs exactly the digest the push step reported;
3. tag application comes last and points the metadata-action tags at that signed digest
   via a single-source `imagetools create`.

A reorder that puts tags back on the push step, or a new signing path that signs something
other than `steps.build.outputs.digest`, lands here red. Stdlib, `pyyaml` and pytest: this
reads the tree, not the project (`pyyaml` is how test_apt_install_is_bounded.py reads these
same workflow files).
"""

from __future__ import annotations

from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "release-image.yml"

#: Step names the contract is stated in terms of. Asserting their presence first keeps every
#: check below from passing vacuously over a renamed or restructured job.
_PUSH_STEP = "Build and push by digest"
_SIGN_STEP = "Sign the pushed digest"
_TAG_STEP = "Apply tags to the signed digest"
_IMAGE = "ghcr.io/randomparity/kdive"


def _publish_steps() -> list[dict]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert workflow is not None, f"{_WORKFLOW.name} did not parse"
    steps = workflow["jobs"]["publish"]["steps"]
    names = [s.get("name") for s in steps]
    for required in (_PUSH_STEP, _SIGN_STEP, _TAG_STEP):
        assert required in names, (
            f"step {required!r} missing from {_WORKFLOW.name} ({names}) — the "
            f"push→sign→tag contract cannot be checked over this shape"
        )
    return steps


def _step(steps: list[dict], name: str) -> dict:
    return next(s for s in steps if s.get("name") == name)


def test_push_step_publishes_by_digest_without_tags() -> None:
    push = _step(_publish_steps(), _PUSH_STEP)
    with_block = push.get("with") or {}
    # A bare `push: true` with no tag errors out in buildx ("tag is needed when pushing to
    # registry"), so the only working digest-only form is the documented `outputs:` export.
    outputs = with_block.get("outputs", "")
    for required in (
        "type=image",
        f"name={_IMAGE}",
        "push-by-digest=true",
        "push=true",
    ):
        assert required in outputs, (
            f"the push step's `outputs:` must carry {required!r} — without it the step "
            f"either cannot push at all or publishes under a tag (ADR-0573); got {outputs!r}"
        )
    assert "tags" not in with_block, (
        "the push step must not carry a `tags:` input — tagging here republishes the "
        "pre-sign ordering where an interruption strands a tagged unsigned digest (ADR-0573)"
    )


def test_sign_step_signs_the_reported_digest() -> None:
    sign = _step(_publish_steps(), _SIGN_STEP)
    digest_expr = "${{ steps.build.outputs.digest }}"
    env = sign.get("env") or {}
    values = str(env)
    assert digest_expr in values, (
        f"the sign step must consume {digest_expr}, not a tag or another source "
        f"(ADR-0088 decision 8 signs the immutable digest); got env {values}"
    )
    run = sign.get("run", "")
    assert f'cosign sign --yes "{_IMAGE}@${{DIGEST}}"' in run, (
        "the sign step must sign the immutable @sha256 digest subject"
    )


def test_tag_application_comes_after_signing_and_targets_the_signed_digest() -> None:
    steps = _publish_steps()
    order = [s.get("name") for s in steps]
    assert order.index(_SIGN_STEP) < order.index(_TAG_STEP), (
        "tags must be applied only after the digest is signed (ADR-0573) — a tag step "
        "before or between the push and sign steps reintroduces #1991"
    )
    tag = _step(steps, _TAG_STEP)
    env = str(tag.get("env") or {})
    assert "${{ steps.build.outputs.digest }}" in env, (
        "the tag step must retarget the exact digest the sign step signed"
    )
    assert "${{ steps.meta.outputs.tags }}" in env, (
        "the tag step must apply the metadata-action tag set, not an ad-hoc list"
    )
    run = tag.get("run", "")
    assert "imagetools create" in run, (
        "tags are applied registry-side with `docker buildx imagetools create` — a rebuild "
        "or a second push would mint a different, unsigned digest"
    )
    assert "--prefer-index=false" in run, (
        "`--prefer-index=false` preserves the single-platform manifest format so the tag "
        "lands on exactly the signed digest instead of a wrapped copy with a new one"
    )
