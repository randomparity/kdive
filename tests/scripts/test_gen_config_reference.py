"""The config-reference generator renders, groups, and redacts (ADR-0087)."""

from __future__ import annotations

from collections.abc import Mapping

from kdive.config.registry import Setting
from scripts.gen_config_reference import render


def _str(raw: str) -> str:
    return raw


def _always(env: Mapping[str, str]) -> bool:
    return True


def test_render_groups_and_redacts_secrets() -> None:
    settings = [
        Setting(
            name="KDIVE_DATABASE_URL",
            parse=_str,
            group="database",
            processes=frozenset({"server"}),
            required_when=_always,
            help="DSN.",
        ),
        Setting(
            name="KDIVE_REMOTE_LIBVIRT_CA_CERT_REF",
            parse=_str,
            secret=True,
            group="remote-libvirt",
            processes=frozenset({"worker"}),
            help="CA ref.",
        ),
    ]
    out = render(settings)
    assert "## database" in out
    assert "## remote-libvirt" in out
    assert "KDIVE_DATABASE_URL" in out
    assert "secret (ref only)" in out  # the secret marker replaces help
    assert "do not edit" in out  # generated-file header


def test_render_is_deterministic_and_groups_sorted() -> None:
    settings = [
        Setting(name="KDIVE_B", parse=_str, group="zeta", processes=frozenset({"server"})),
        Setting(name="KDIVE_A", parse=_str, group="alpha", processes=frozenset({"server"})),
    ]
    out = render(settings)
    assert out == render(settings)
    assert out.index("## alpha") < out.index("## zeta")


def test_render_marks_required_conditional_and_optional() -> None:
    def _uri_set(env: Mapping[str, str]) -> bool:
        return bool(env.get("KDIVE_URI"))

    settings = [
        Setting(
            name="KDIVE_REQ",
            parse=_str,
            group="g",
            processes=frozenset({"server"}),
            required_when=_always,
        ),
        Setting(
            name="KDIVE_COND",
            parse=_str,
            group="g",
            processes=frozenset({"server"}),
            required_when=_uri_set,
        ),
        Setting(name="KDIVE_OPT", parse=_str, group="g", processes=frozenset({"server"})),
    ]
    out = render(settings)
    lines = {n: next(ln for ln in out.splitlines() if n in ln) for n in ("REQ", "COND", "OPT")}
    assert "yes" in lines["REQ"]
    assert "conditional" in lines["COND"]
    assert "| no |" in lines["OPT"]
