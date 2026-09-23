"""Guard that both apt-using Dockerfile stages fetch over HTTPS (issue #2668).

The ``python:3.14.6-slim-bookworm`` base ships a deb822 ``/etc/apt/sources.list.d/
debian.sources`` whose default ``URIs:`` is ``http://deb.debian.org/debian``. A network
that permits only HTTPS egress cannot reach that mirror, so ``apt-get update`` fails and
the image never builds there. Each apt-using ``RUN`` must switch the sources to
``https://`` before its own ``apt-get update`` call — package authenticity is unaffected
(apt still verifies signatures), only the transport changes.

Stdlib + pytest only: ``tests/image/`` is collected with ``--noconftest`` in CI, without
the project installed.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"

#: Each apt-using stage's `RUN` instruction, captured up to (but not including) the next
#: top-level Dockerfile instruction, so an https switch anywhere in the block counts.
_APT_UPDATE_RUN = re.compile(
    r"^RUN\b(?P<block>(?:.*\\\n)*.*apt-get update.*(?:\\\n.*)*)$",
    re.MULTILINE,
)
#: The sed/equivalent switch from the http mirror to https, scoped to the sources file: the
#: rewrite and the path must appear together, or a switch aimed at the wrong file would count.
_HTTPS_SWITCH = re.compile(
    r"http://deb\.debian\.org.*https://deb\.debian\.org.*/etc/apt/sources\.list\.d/debian\.sources"
)


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def test_apt_update_blocks_are_discoverable() -> None:
    # A rename or refactor that stops matching would make the https assertion below pass
    # vacuously, so pin the expected shape of the Dockerfile first.
    blocks = _APT_UPDATE_RUN.findall(_dockerfile())
    assert len(blocks) == 2, (
        f"expected 2 apt-get update RUN blocks in {_DOCKERFILE.name}, found {len(blocks)}"
    )


def test_every_apt_update_block_switches_to_https_first() -> None:
    text = _dockerfile()
    blocks = _APT_UPDATE_RUN.findall(text)
    for block in blocks:
        update_pos = block.find("apt-get update")
        switch_match = _HTTPS_SWITCH.search(block)
        assert switch_match is not None, (
            f"apt-get update block has no http->https sources switch before it:\n{block}"
        )
        assert switch_match.start() < update_pos, (
            f"the https switch must run before apt-get update in the same RUN block:\n{block}"
        )
