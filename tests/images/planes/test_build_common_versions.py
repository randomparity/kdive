"""parse_virt_inspector_versions: pure XML -> {name: version}."""

from __future__ import annotations

import pytest

from kdive.images.planes._build_common import parse_virt_inspector_versions

_XML = """<?xml version="1.0"?>
<operatingsystems>
  <operatingsystem>
    <name>linux</name>
    <applications>
      <application><name>makedumpfile</name><version>1.7.9</version></application>
      <application><name>drgn</name><version>0.0.28</version><release>1.fc44</release></application>
      <application><name>nameless</name></application>
    </applications>
  </operatingsystem>
</operatingsystems>"""


def test_parse_maps_name_to_version() -> None:
    assert parse_virt_inspector_versions(_XML) == {
        "makedumpfile": "1.7.9",
        "drgn": "0.0.28",
    }


def test_parse_skips_application_without_version() -> None:
    assert "nameless" not in parse_virt_inspector_versions(_XML)


def test_parse_empty_or_no_applications_is_empty() -> None:
    assert parse_virt_inspector_versions("<operatingsystems/>") == {}


def test_parse_rejects_doctype_entities() -> None:
    # Defensive: a DOCTYPE must be refused so a crafted package name cannot trigger entity
    # expansion (stdlib ElementTree expands internal entities only when a DTD is present).
    hostile = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]>'
        "<operatingsystems><operatingsystem><applications>"
        "<application><name>&e;</name><version>1</version></application>"
        "</applications></operatingsystem></operatingsystems>"
    )
    with pytest.raises(ValueError):
        parse_virt_inspector_versions(hostile)
