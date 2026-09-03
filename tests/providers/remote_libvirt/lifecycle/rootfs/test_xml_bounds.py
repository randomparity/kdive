import pytest

from kdive.providers.remote_libvirt.lifecycle.rootfs.xml_bounds import (
    MAX_LIBVIRT_XML_AGGREGATE_BYTES,
    MAX_LIBVIRT_XML_BYTES,
    MAX_LIBVIRT_XML_DOCUMENTS,
    XmlEnumerationBudget,
    parse_libvirt_xml,
)


def xml_document(size: int) -> str:
    return "<x>" + "a" * (size - 7) + "</x>"


def test_single_xml_accepts_exact_byte_limit_and_rejects_one_over() -> None:
    assert parse_libvirt_xml(xml_document(MAX_LIBVIRT_XML_BYTES)).tag == "x"
    with pytest.raises(ValueError, match="provider limit"):
        parse_libvirt_xml(xml_document(MAX_LIBVIRT_XML_BYTES + 1))


def test_character_limit_rejects_before_encode() -> None:
    class Oversized(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            raise AssertionError("encode must not be called")

    with pytest.raises(ValueError, match="provider limit"):
        parse_libvirt_xml(Oversized("x" * (MAX_LIBVIRT_XML_BYTES + 1)))


def test_enumeration_accepts_many_bounded_documents() -> None:
    budget = XmlEnumerationBudget()
    for _ in range(MAX_LIBVIRT_XML_DOCUMENTS):
        assert budget.parse("<x/>").tag == "x"


def test_enumeration_rejects_count_and_aggregate_overflow() -> None:
    count_budget = XmlEnumerationBudget()
    for _ in range(MAX_LIBVIRT_XML_DOCUMENTS):
        count_budget.parse("<x/>")
    with pytest.raises(ValueError, match="enumeration"):
        count_budget.parse("<x/>")

    byte_budget = XmlEnumerationBudget()
    byte_budget.bytes = MAX_LIBVIRT_XML_AGGREGATE_BYTES
    with pytest.raises(ValueError, match="enumeration"):
        byte_budget.parse("<x/>")
