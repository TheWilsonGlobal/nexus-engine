"""Structural hierarchical parser for legal codes, corporate SOPs, and compliance documents."""
from __future__ import annotations

import re
from typing import Optional

from nexus_engine.services.ingestion.json_legal_parser import parse_json_legal
from nexus_engine.services.ingestion.markdown_parser import parse_markdown
from nexus_engine.services.ingestion.parser_models import DocFormat, ParsedDocument, ParsedSection
from nexus_engine.services.ingestion.text_hierarchy_parser import parse_pdf, parse_plain_text

__all__ = ["DocFormat", "ParsedSection", "ParsedDocument", "HierarchicalParser"]


class HierarchicalParser:
    def parse(
        self,
        raw_bytes: bytes,
        filename: str,
        format_hint: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> ParsedDocument:
        fmt = self._detect_format(filename, format_hint)
        base_id = doc_id or self._clean_id(filename)

        if fmt == DocFormat.JSON_LEGAL:
            return parse_json_legal(raw_bytes, base_id, filename)
        elif fmt == DocFormat.MARKDOWN:
            return parse_markdown(raw_bytes, base_id, filename)
        elif fmt == DocFormat.PDF:
            return parse_pdf(raw_bytes, base_id, filename)
        else:
            return parse_plain_text(raw_bytes, base_id, filename)

    @staticmethod
    def _clean_id(name: str) -> str:
        s = re.sub(r"\.[^.]+$", "", name.lower())
        return re.sub(r"[^a-z0-9_-]+", "_", s).strip("_") or "doc"

    @staticmethod
    def _detect_format(filename: str, hint: Optional[str]) -> DocFormat:
        if hint:
            try:
                return DocFormat(hint.lower())
            except ValueError:
                pass
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        if ext == "json":
            return DocFormat.JSON_LEGAL
        elif ext in ("md", "markdown"):
            return DocFormat.MARKDOWN
        elif ext == "pdf":
            return DocFormat.PDF
        return DocFormat.PLAIN_TEXT
