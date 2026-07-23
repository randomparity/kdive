"""Live-stack proof that the *generated* kdivectl verb surface drives a real lifecycle (#1453).

The break-glass boundary test (``test_kdivectl_boundary.py``) drives ``ops force-release`` — a
*curated* :class:`kdive.cli.commands.registry.Verb` whose hand-written handler runs directly. Its
canonical path never reaches the generic generated-verb seam
(:func:`kdive.cli.dispatch.invoke_generated_verb`): :func:`~kdive.cli.commands.registry.run_verb`
resolves a curated verb first and only falls through to the seam for a *non-curated* path. So a
green boundary test says nothing about whether a schema-generated verb assembles a correct payload,
whether the server accepts it, or whether its response renders.

This test closes that gap. It drives a multi-step create -> read -> list lifecycle end to end
through **generated (non-curated) verbs only** — ``session whoami``, ``investigations open``,
``investigations get``, ``investigations list`` — via the real entry point (``python -m kdive.cli``
as a subprocess, so the asserted exit code and rendered stdout are exactly what a script or CI
sees), each routing through ``invoke_generated_verb``. It asserts the three things a
parser-construction test cannot: payload assembly (the ``--project`` / ``--title`` /
``--investigation-id`` flag values round-trip through the tool call and come back on the entity),
server acceptance (exit 0), and rendered response (the default table render carries the claims; the
``--json`` render is a parseable envelope).

It closes with a generated-verb authorization boundary that reaches the same exit ``3`` as the
curated boundary test, by the seam's own fail-closed gate: ``reports generate-all-projects`` (the
``reports.generate_all_projects`` tool, non-curated) is ``platform_auditor``-scoped, so the server
does not expose it to a project-only token. The generic seam classifies the absent tool ``UNKNOWN``
and refuses it — exit ``3`` (:mod:`kdive.cli.errors`) with a plain "unreachable" message — rather
than blindly dispatching a tool the caller cannot see (the ``UNKNOWN`` branch of
:func:`~kdive.cli.dispatch.invoke_generated_verb`'s ceremony gate). Project-scoped generated verbs
instead *raise* on a role denial, so this exposure-driven refusal is the seam's cleanest exit-3
boundary.

Gated ``live_stack`` (ADR-0035 §4): it needs a running kdive stack plus a reachable OIDC issuer, so
it skips cleanly in normal CI and runs only against a brought-up stack (``just stack-up`` +
``just test-live-stack``). See ``docs/operating/runbooks/kdivectl.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest

from kdive.mcp.dev_harness import OidcIssuer, mint_token
from tests.integration.live_stack.conftest import require_issuer, require_stack

_OPERATOR_ROLE = "operator"
_AUTHORIZATION_DENIED_EXIT = 3


def _cli_token(issuer: OidcIssuer, *, project: str) -> str:
    """Mint an operator token scoped to exactly ``project`` (no platform role).

    Operator (rank 2) satisfies the ``contributor`` floor ``investigations.open`` requires, and
    the missing platform role is what the ``reports.generate_all_projects`` boundary check denies.
    ``client_id='kdivectl'`` marks the token as the operator CLI's, mirroring the boundary test.
    """
    return mint_token(
        issuer,
        subject="gen-lifecycle-cli",
        projects=[project],
        roles={project: _OPERATOR_ROLE},
        client_id="kdivectl",
    )


async def _run_kdivectl(argv: list[str], *, token: str, server_url: str) -> tuple[int, str]:
    """Run the real ``kdivectl`` entry point as a subprocess; return ``(exit_code, stdout)``.

    Driving the actual entry point (not the in-process handler) is the boundary #1453 prescribes:
    it exercises argparse -> ``run_verb`` -> ``invoke_generated_verb`` -> transport exactly as an
    operator would, with the token and server URL supplied via the environment. stdout is captured
    (not discarded) so the caller can assert the rendered response, and stderr is surfaced so a
    non-zero exit self-diagnoses.
    """
    env = {**os.environ, "KDIVE_TOKEN": token, "KDIVE_SERVER_URL": server_url}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kdive.cli",
        *argv,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode is not None
    if proc.returncode != 0:
        # Surface stderr on failure so a red run names the cause rather than an opaque exit code.
        sys.stderr.write(err.decode(errors="replace"))
    return proc.returncode, out.decode()


async def _run_json(
    argv: list[str], *, token: str, server_url: str
) -> tuple[int, dict[str, object]]:
    """Run a verb with ``--json`` and parse its rendered envelope; return ``(exit_code, envelope)``.

    ``--json`` makes the generated-verb render path (:func:`kdive.cli.render.render_envelope`) emit
    the whole response envelope verbatim, so a structured assertion on ``object_id`` / ``status`` /
    ``error_category`` is exact rather than a brittle scrape of the default table.
    """
    code, out = await _run_kdivectl([*argv, "--json"], token=token, server_url=server_url)
    try:
        envelope = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"{argv} did not render a JSON envelope (exit {code}): {out!r}"
        ) from exc
    assert isinstance(envelope, dict), f"{argv} rendered a non-object envelope: {envelope!r}"
    return code, envelope


async def _drive_generated_lifecycle(issuer: OidcIssuer, server_url: str) -> None:
    """Drive session -> open -> get -> list through generated verbs, then the denial boundary."""
    project = f"kdivectl-gen-{uuid4().hex[:8]}"
    title = f"generated-lifecycle-{uuid4().hex[:8]}"
    token = _cli_token(issuer, project=project)

    # 1. session whoami (read-only generated), DEFAULT render: proves the token is accepted, the
    #    seam reaches the server, and the table render path emits the caller's own claims.
    code, out = await _run_kdivectl(["session", "whoami"], token=token, server_url=server_url)
    assert code == 0, "session whoami (a generated read verb) should succeed (exit 0)"
    assert "gen-lifecycle-cli" in out, f"whoami table did not render the principal: {out!r}"
    assert project in out, f"whoami table did not render the granted project: {out!r}"

    # 2. investigations open (mutating generated verb): naming the verb is the acknowledgement (no
    #    opt-in flag); the --project/--title flags must assemble into the tool payload, the server
    #    must accept the write, and the new object_id must render.
    code, env = await _run_json(
        ["investigations", "open", "--project", project, "--title", title],
        token=token,
        server_url=server_url,
    )
    assert code == 0, "investigations open (a generated mutating verb) should succeed (exit 0)"
    # Success is the absence of an ``error_category`` (what the CLI exit code derives from); the
    # ``status`` field carries the domain state (``open``), not a generic ``ok``.
    assert env.get("error_category") is None, f"open did not succeed: {env!r}"
    investigation_id = env.get("object_id")
    assert isinstance(investigation_id, str) and investigation_id, f"open returned no id: {env!r}"

    # 3. investigations get --investigation-id <id> (read-only generated, REQUIRED scalar): the
    #    strongest payload-assembly proof — the id must route into the payload so the server returns
    #    exactly that entity, and the round-tripped title confirms step 2's write reached storage.
    code, env = await _run_json(
        ["investigations", "get", "--investigation-id", investigation_id],
        token=token,
        server_url=server_url,
    )
    assert code == 0, "investigations get should succeed (exit 0)"
    assert env.get("object_id") == investigation_id, f"get returned the wrong entity: {env!r}"
    data = env.get("data")
    assert isinstance(data, dict), f"get returned no data mapping: {env!r}"
    assert data.get("title") == title, f"title did not round-trip: {env!r}"

    # 4. investigations list --project <p> (read-only generated, unwrap_request): the collection
    #    render path must surface the just-opened investigation among its items.
    code, env = await _run_json(
        ["investigations", "list", "--project", project],
        token=token,
        server_url=server_url,
    )
    assert code == 0, "investigations list should succeed (exit 0)"
    items = env.get("items")
    assert isinstance(items, list), f"list returned no items collection: {env!r}"
    listed_ids = {item.get("object_id") for item in items if isinstance(item, dict)}
    assert investigation_id in listed_ids, f"opened investigation not listed: {listed_ids!r}"

    # 5. authorization boundary (fail-closed on an unexposed tool): reports.generate_all_projects
    #    is platform_auditor-scoped, so the server does not expose it to this project-only token.
    #    The generated-verb seam classifies the absent tool UNKNOWN and refuses it with exit 3 and a
    #    plain "unreachable" message — the generated analogue of the boundary test's exit-3 denial,
    #    proving RBAC surfaces as a distinct nonzero exit rather than a blind dispatch.
    code, out = await _run_kdivectl(
        ["reports", "generate-all-projects"], token=token, server_url=server_url
    )
    assert code == _AUTHORIZATION_DENIED_EXIT, (
        "an unexposed platform-scoped generated verb should be refused with exit 3"
    )
    assert "not positively classified" in out, f"the refusal did not name the reason: {out!r}"


@pytest.mark.live_stack
def test_generated_verb_lifecycle_over_the_real_entry_point() -> None:
    """Generated verbs drive open->get->list end to end via python -m kdive.cli; denial exits 3."""
    issuer = require_issuer()
    server_url = require_stack()
    asyncio.run(_drive_generated_lifecycle(issuer, server_url))
