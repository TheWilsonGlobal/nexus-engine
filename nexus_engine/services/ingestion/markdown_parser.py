"""Parser for markdown documents with heading-based hierarchy (H1, H2, H3)."""
from __future__ import annotations

from typing import Optional

from nexus_engine.services.ingestion.parser_models import DocFormat, ParsedDocument, ParsedSection


def parse_markdown(raw_bytes: bytes, doc_id: str, filename: str) -> ParsedDocument:
    text = raw_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    doc_title = filename

    # Find top H1 as document title
    for ln in lines:
        if ln.startswith("# "):
            doc_title = ln[2:].strip()
            break

    sections: list[ParsedSection] = []
    current_h2: Optional[ParsedSection] = None
    current_h3: Optional[ParsedSection] = None
    current_buffer: list[str] = []

    def flush_buffer() -> None:
        nonlocal current_buffer
        if not current_buffer:
            return
        body = "\n".join(current_buffer).strip()
        current_buffer = []
        if current_h3 is not None:
            current_h3.content = (current_h3.content + "\n" + body).strip()
        elif current_h2 is not None:
            current_h2.content = (current_h2.content + "\n" + body).strip()

    h2_idx = 0
    h3_idx = 0

    for line in lines:
        if line.startswith("## "):
            flush_buffer()
            h2_idx += 1
            h3_idx = 0
            h2_title = line[3:].strip()
            current_h2 = ParsedSection(
                section_id=f"{doc_id}:s{h2_idx}",
                level="h2",
                title=h2_title,
                content="",
                path=[doc_title, h2_title],
            )
            current_h3 = None
            sections.append(current_h2)
        elif line.startswith("### "):
            flush_buffer()
            h3_idx += 1
            h3_title = line[4:].strip()
            parent_path = current_h2.path if current_h2 else [doc_title]
            parent_id = current_h2.section_id if current_h2 else doc_id
            current_h3 = ParsedSection(
                section_id=f"{parent_id}:sub{h3_idx}",
                level="h3",
                title=h3_title,
                content="",
                path=parent_path + [h3_title],
            )
            if current_h2:
                current_h2.subsections.append(current_h3)
            else:
                sections.append(current_h3)
        elif not line.startswith("# "):
            current_buffer.append(line)

    flush_buffer()

    if not sections and lines:
        sections.append(
            ParsedSection(
                section_id=f"{doc_id}:main",
                level="document",
                title=doc_title,
                content=text.strip(),
                path=[doc_title],
            )
        )

    return ParsedDocument(
        doc_id=doc_id,
        title=doc_title,
        format=DocFormat.MARKDOWN,
        sections=sections,
    )
