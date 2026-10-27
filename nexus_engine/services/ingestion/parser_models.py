"""Data models and enums for hierarchical document ingestion."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DocumentParseError(ValueError):
    """Uploaded bytes could not be parsed as the detected format.

    The message is deliberately generic and safe to return to API clients; the underlying
    exception is chained (``__cause__``) for server-side logs only.
    """


class DocFormat(str, Enum):
    JSON_LEGAL = "json_legal"
    MARKDOWN = "markdown"
    PDF = "pdf"
    PLAIN_TEXT = "plain_text"


@dataclass
class ParsedSection:
    section_id: str
    level: str  # "part" | "chapter" | "section" | "article" | "clause" | "h1" | "h2" | "h3" | "paragraph"
    title: str
    content: str
    path: list[str]  # e.g. ["Part 1", "Chapter II", "Article 16"]
    metadata: dict[str, Any] = field(default_factory=dict)
    subsections: list[ParsedSection] = field(default_factory=list)


@dataclass
class ParsedDocument:
    doc_id: str
    title: str
    format: DocFormat
    sections: list[ParsedSection]
    metadata: dict[str, Any] = field(default_factory=dict)
