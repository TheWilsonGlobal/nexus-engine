"""Parser for structured legal JSON formats (Civil codes, decrees, statutory schemas)."""
from __future__ import annotations

import json
from typing import Any

from nexus_engine.services.ingestion.parser_models import DocFormat, DocumentParseError, ParsedDocument, ParsedSection


def build_article_section(art: dict[str, Any], parent_id: str, parent_path: list[str], idx: int) -> ParsedSection:
    art_title = art.get("title", f"Article {art.get('number', idx)}")
    art_path = parent_path + [art_title]
    clauses = art.get("clauses", [])
    if isinstance(clauses, str):
        content = clauses
        clause_sections: list[ParsedSection] = []
    else:
        content = "\n".join(clauses)
        clause_sections = [
            ParsedSection(
                section_id=f"{parent_id}:a{idx}:cl{cl_idx}",
                level="clause",
                title=f"Clause {cl_idx}",
                content=cl_text,
                path=art_path + [f"Clause {cl_idx}"],
            )
            for cl_idx, cl_text in enumerate(clauses, 1)
        ]
    return ParsedSection(
        section_id=f"{parent_id}:a{idx}",
        level="article",
        title=art_title,
        content=content,
        path=art_path,
        metadata={"article_number": art.get("number", idx)},
        subsections=clause_sections,
    )


def parse_json_legal(raw_bytes: bytes, doc_id: str, filename: str) -> ParsedDocument:
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise DocumentParseError("Uploaded file is not valid JSON.") from exc
    if not isinstance(data, dict):
        raise DocumentParseError("JSON document must be an object at the top level.")
    try:
        return _parse_json_legal_object(data, doc_id, filename)
    except (AttributeError, TypeError, KeyError, IndexError) as exc:
        raise DocumentParseError("JSON document does not match the expected legal-document structure.") from exc


def _parse_json_legal_object(data: dict[str, Any], doc_id: str, filename: str) -> ParsedDocument:
    doc_meta = data.get("document", data)
    title = doc_meta.get("title", filename)
    parts = doc_meta.get("parts", [])
    sections: list[ParsedSection] = []

    if parts:
        for p_idx, part in enumerate(parts, 1):
            part_title = part.get("title", f"Part {p_idx}")
            part_path = [part_title]
            part_sec = ParsedSection(
                section_id=f"{doc_id}:p{p_idx}",
                level="part",
                title=part_title,
                content="",
                path=part_path,
            )
            for c_idx, chap in enumerate(part.get("chapters", []), 1):
                chap_title = chap.get("title", f"Chapter {c_idx}")
                chap_path = part_path + [chap_title]
                chap_sec = ParsedSection(
                    section_id=f"{part_sec.section_id}:c{c_idx}",
                    level="chapter",
                    title=chap_title,
                    content="",
                    path=chap_path,
                )
                direct_articles = chap.get("articles", [])
                sec_groups = chap.get("sections", [])
                if direct_articles:
                    for a_idx, art in enumerate(direct_articles, 1):
                        chap_sec.subsections.append(
                            build_article_section(art, chap_sec.section_id, chap_path, a_idx)
                        )
                for s_idx, sec in enumerate(sec_groups, 1):
                    sec_title = sec.get("title", f"Section {s_idx}")
                    sec_path = chap_path + [sec_title]
                    sec_node = ParsedSection(
                        section_id=f"{chap_sec.section_id}:s{s_idx}",
                        level="section",
                        title=sec_title,
                        content="",
                        path=sec_path,
                    )
                    for a_idx, art in enumerate(sec.get("articles", []), 1):
                        sec_node.subsections.append(
                            build_article_section(art, sec_node.section_id, sec_path, a_idx)
                        )
                    chap_sec.subsections.append(sec_node)
                part_sec.subsections.append(chap_sec)
            sections.append(part_sec)
    else:
        articles = doc_meta.get("articles", [])
        for a_idx, art in enumerate(articles, 1):
            sections.append(build_article_section(art, doc_id, [title], a_idx))

    return ParsedDocument(
        doc_id=doc_id,
        title=title,
        format=DocFormat.JSON_LEGAL,
        sections=sections,
        metadata={"jurisdiction": doc_meta.get("jurisdiction", "General"), "law_number": doc_meta.get("law_number", "")},
    )
