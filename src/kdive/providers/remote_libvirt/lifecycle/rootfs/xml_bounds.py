"""Provider-private bounds for libvirt-controlled XML documents."""

import xml.etree.ElementTree as ET

from defusedxml.ElementTree import fromstring as safe_fromstring

MAX_LIBVIRT_XML_BYTES = 1024 * 1024
MAX_LIBVIRT_XML_DOCUMENTS = 4096
MAX_LIBVIRT_XML_AGGREGATE_BYTES = 16 * 1024 * 1024


def parse_libvirt_xml(document: str) -> ET.Element:
    if len(document) > MAX_LIBVIRT_XML_BYTES:
        raise ValueError("libvirt XML exceeds provider limit")
    encoded = document.encode()
    if len(encoded) > MAX_LIBVIRT_XML_BYTES:
        raise ValueError("libvirt XML exceeds provider limit")
    return safe_fromstring(encoded)


class XmlEnumerationBudget:
    def __init__(self) -> None:
        self.documents = 0
        self.bytes = 0

    def parse(self, document: str) -> ET.Element:
        if len(document) > MAX_LIBVIRT_XML_BYTES:
            raise ValueError("libvirt XML exceeds provider limit")
        encoded = document.encode()
        if len(encoded) > MAX_LIBVIRT_XML_BYTES:
            raise ValueError("libvirt XML exceeds provider limit")
        self.documents += 1
        self.bytes += len(encoded)
        if (
            self.documents > MAX_LIBVIRT_XML_DOCUMENTS
            or self.bytes > MAX_LIBVIRT_XML_AGGREGATE_BYTES
        ):
            raise ValueError("libvirt XML enumeration exceeds provider limit")
        return safe_fromstring(encoded)
