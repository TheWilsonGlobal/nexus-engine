"""Parser for PDF and plain text documents with pattern-based chapter/article hierarchies."""
from __future__ import annotations

import io
import logging
import re
from typing import Optional

from nexus_engine.services.ingestion.parser_models import DocFormat, DocumentParseError, ParsedDocument, ParsedSection

log = logging.getLogger(__name__)

_RX_CHAP = re.compile(r"^(?:Chapter|Chương)\s+([IVXLCDM]+|\d+)\s*[-:–—]?\s*(.*)$", re.I)
_RX_ART = re.compile(r"^(?:Article|Điều)\s+(\d+)\s*[-:–—.]?\s*(.*)$", re.I)


def parse_pdf(raw_bytes: bytes, doc_id: str, filename: str) -> ParsedDocument:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        pages_text: list[str] = []
        for p in reader.pages:
            txt = p.extract_text()
            if txt:
                pages_text.append(txt)
        full_text = "\n\n".join(pages_text)
    except Exception as exc:
        log.warning("PDF extraction failed for %s: %s", filename, exc)
        raise DocumentParseError("Uploaded file is not a readable PDF.") from exc

    return parse_text_hierarchy(full_text, doc_id, filename, DocFormat.PDF)


def parse_plain_text(raw_bytes: bytes, doc_id: str, filename: str) -> ParsedDocument:
    full_text = raw_bytes.decode("utf-8", errors="replace")
    return parse_text_hierarchy(full_text, doc_id, filename, DocFormat.PLAIN_TEXT)


def parse_text_hierarchy(text: str, doc_id: str, filename: str, fmt: DocFormat) -> ParsedDocument:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = filename
    if lines and lines[0].lower().startswith("document:"):
        title = lines[0].split(":", 1)[1].strip()
        lines = lines[1:]

    sections: list[ParsedSection] = []
    current_chap: Optional[ParsedSection] = None
    current_art: Optional[ParsedSection] = None
    c_idx = 0
    a_idx = 0

    for ln in lines:
        if _RX_CHAP.match(ln):
            c_idx += 1
            a_idx = 0
            c_title = ln
            current_chap = ParsedSection(
                section_id=f"{doc_id}:c{c_idx}",
                level="chapter",
                title=c_title,
                content="",
                path=[title, c_title],
            )
            current_art = None
            sections.append(current_chap)
        elif _RX_ART.match(ln):
            a_idx += 1
            a_title = ln
            parent_path = current_chap.path if current_chap else [title]
            parent_id = current_chap.section_id if current_chap else doc_id
            current_art = ParsedSection(
                section_id=f"{parent_id}:a{a_idx}",
                level="article",
                title=a_title,
                content="",
                path=parent_path + [a_title],
            )
            if current_chap:
                current_chap.subsections.append(current_art)
            else:
                sections.append(current_art)
        else:
            target = current_art or current_chap
            if target:
                target.content = (target.content + "\n" + ln).strip()
            elif sections:
                sections[-1].content = (sections[-1].content + "\n" + ln).strip()
            else:
                sections.append(
                    ParsedSection(
                        section_id=f"{doc_id}:intro",
                        level="section",
                        title=title,
                        content=ln,
                        path=[title],
                    )
                )

    if not sections and text.strip():
        sections.append(
            ParsedSection(
                section_id=f"{doc_id}:main",
                level="document",
                title=title,
                content=text.strip(),
                path=[title],
            )
        )

    return ParsedDocument(
        doc_id=doc_id,
        title=title,
        format=fmt,
        sections=sections,
    )
