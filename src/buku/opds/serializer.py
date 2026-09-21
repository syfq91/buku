"""Atom/XML serialization of OPDS 1.2 catalog feeds and OpenSearch documents.

Turns the immutable :mod:`buku.opds.models` presentation models into wire
format. ElementTree is used with registered namespace prefixes so feeds use
the conventional ``<feed xmlns="http://www.w3.org/2005/Atom">`` plus ``dc:``,
``opds:``, ``opensearch:`` and ``thr:`` extensions without any string
building or manual escaping.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from buku.opds.models import (
    ATOM_NS,
    DC_NS,
    OPDS_NS,
    OPENSEARCH_NS,
    SERIES_CATEGORY_SCHEME,
    THR_NS,
    OpdsAuthor,
    OpdsEntry,
    OpdsFeed,
    OpdsLink,
)

for _prefix, _uri in (
    ("", ATOM_NS),
    ("dc", DC_NS),
    ("opds", OPDS_NS),
    ("opensearch", OPENSEARCH_NS),
    ("thr", THR_NS),
):
    ET.register_namespace(_prefix, _uri)


def _rfc3339(value: datetime) -> str:
    """Format a datetime as an RFC 3339 UTC timestamp (second precision)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _append_link(parent: ET.Element, link: OpdsLink) -> None:
    element = ET.SubElement(parent, f"{{{ATOM_NS}}}link")
    element.set("rel", link.rel)
    element.set("href", link.href)
    element.set("type", link.type)
    if link.title:
        element.set("title", link.title)
    if link.count is not None:
        element.set(f"{{{THR_NS}}}count", str(link.count))


def _append_author(parent: ET.Element, author: OpdsAuthor) -> None:
    author_element = ET.SubElement(parent, f"{{{ATOM_NS}}}author")
    name = ET.SubElement(author_element, f"{{{ATOM_NS}}}name")
    name.text = author.name


def _append_entry(parent: ET.Element, entry: OpdsEntry) -> None:
    element = ET.SubElement(parent, f"{{{ATOM_NS}}}entry")

    title = ET.SubElement(element, f"{{{ATOM_NS}}}title")
    title.text = entry.title
    ident = ET.SubElement(element, f"{{{ATOM_NS}}}id")
    ident.text = entry.id
    updated = ET.SubElement(element, f"{{{ATOM_NS}}}updated")
    updated.text = _rfc3339(entry.updated)

    for author in entry.authors:
        _append_author(element, author)

    if entry.language:
        lang = ET.SubElement(element, f"{{{DC_NS}}}language")
        lang.text = entry.language
    if entry.issued:
        issued = ET.SubElement(element, f"{{{DC_NS}}}issued")
        issued.text = entry.issued
    if entry.publisher:
        publisher = ET.SubElement(element, f"{{{DC_NS}}}publisher")
        publisher.text = entry.publisher
    for ident_value in entry.identifiers:
        identifier = ET.SubElement(element, f"{{{DC_NS}}}identifier")
        identifier.text = ident_value

    if entry.series:
        category = ET.SubElement(element, f"{{{ATOM_NS}}}category")
        category.set("scheme", SERIES_CATEGORY_SCHEME)
        category.set("term", entry.series)
        category.set("label", entry.series)

    if entry.summary:
        summary = ET.SubElement(element, f"{{{ATOM_NS}}}summary")
        summary.set("type", "text")
        summary.text = entry.summary
    if entry.content:
        content = ET.SubElement(element, f"{{{ATOM_NS}}}content")
        content.set("type", "text")
        content.text = entry.content

    for link in entry.links:
        _append_link(element, link)


def serialize_feed(feed: OpdsFeed) -> bytes:
    """Serialize an OPDS feed model into an Atom/XML byte string."""
    root = ET.Element(f"{{{ATOM_NS}}}feed")

    ident = ET.SubElement(root, f"{{{ATOM_NS}}}id")
    ident.text = feed.id
    title = ET.SubElement(root, f"{{{ATOM_NS}}}title")
    title.text = feed.title
    updated = ET.SubElement(root, f"{{{ATOM_NS}}}updated")
    updated.text = _rfc3339(feed.updated)
    author = ET.SubElement(root, f"{{{ATOM_NS}}}author")
    author_name = ET.SubElement(author, f"{{{ATOM_NS}}}name")
    author_name.text = "buku"

    for link in feed.links:
        _append_link(root, link)

    if feed.total_results is not None:
        total = ET.SubElement(root, f"{{{OPENSEARCH_NS}}}totalResults")
        total.text = str(feed.total_results)
    if feed.items_per_page is not None:
        per_page = ET.SubElement(root, f"{{{OPENSEARCH_NS}}}itemsPerPage")
        per_page.text = str(feed.items_per_page)

    for entry in feed.entries:
        _append_entry(root, entry)

    output = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    assert isinstance(output, bytes)
    return output


_OPENSEARCH_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">\n'
    "  <ShortName>buku</ShortName>\n"
    "  <Description>Search the buku OPDS catalog.</Description>\n"
    '  <Url type="application/atom+xml;profile=opds-catalog;kind=acquisition"\n'
    '       template="{search_url}" />\n'
    "</OpenSearchDescription>"
)


def serialize_opensearch_description(base_url: str) -> bytes:
    """Serialize an OpenSearch 1.1 description document for ``/opds/search``."""
    search_template = f"{base_url}/opds/search?q={{searchTerms}}"
    return _OPENSEARCH_TEMPLATE.format(search_url=search_template).encode("utf-8")


__all__ = ["serialize_feed", "serialize_opensearch_description"]
