"""Hierarchical chunking: Document -> Chapter -> Article -> Clause.

Parses plain text such as::

    Document: Data Processing Agreement
    Chapter I - General
    Article 1 - Definitions
    1. First clause text.
    2. Second clause text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

Level = Literal["document", "chapter", "article", "clause"]

_DOC = re.compile(r"^Document\s*:\s*(.+)$", re.I)
_CHAPTER = re.compile(r"^Chapter\s+([IVXLCDM]+|\d+)\b\s*[-:.–—]?\s*(.*)$", re.I)
_ARTICLE = re.compile(r"^Article\s+(\d+)\b\s*[-:.–—]?\s*(.*)$", re.I)
_CLAUSE = re.compile(r"^\(?(\d+)[.)]\s+(.+)$")


@dataclass
class Chunk:
    id: str
    level: Level
    title: str
    text: str
    path: str
    parent_id: Optional[str] = None
    number: Optional[str] = None
    children: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.title or self.path.rsplit(" > ", 1)[-1]


def parse_document(doc_id: str, raw: str, default_title: str = "Untitled") -> list[Chunk]:
    chunks: dict[str, Chunk] = {}
    order: list[str] = []

    def add(c: Chunk) -> Chunk:
        chunks[c.id] = c
        order.append(c.id)
        if c.parent_id:
            chunks[c.parent_id].children.append(c.id)
        return c

    title = default_title
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if lines and (m := _DOC.match(lines[0])):
        title = m.group(1).strip()
        lines = lines[1:]
    doc = add(Chunk(doc_id, "document", title, title, title))
    chapter: Optional[Chunk] = None
    article: Optional[Chunk] = None
    clause: Optional[Chunk] = None

    for ln in lines:
        if m := _CHAPTER.match(ln):
            num = m.group(1).upper()
            chapter = add(Chunk(f"{doc_id}/ch{num.lower()}", "chapter", ln, ln, f"{title} > Chapter {num}", doc_id, num))
            article = clause = None
        elif m := _ARTICLE.match(ln):
            num = m.group(1)
            parent = chapter or doc
            art_id = f"{parent.id}/art{num}"
            article = add(Chunk(art_id, "article", ln, "", f"{parent.path} > Article {num}", parent.id, num))
            clause = None
        elif article is not None and (m := _CLAUSE.match(ln)):
            num = m.group(1)
            clause = add(
                Chunk(f"{article.id}/cl{num}", "clause", f"Clause {num}", m.group(2), f"{article.path} > Clause {num}", article.id, num)
            )
        elif clause is not None:
            clause.text += " " + ln
        elif article is not None:
            article.text = (article.text + " " + ln).strip()
        else:
            doc.text += " " + ln
    return [chunks[i] for i in order]


def leaf_text(chunk: Chunk, by_id: dict[str, Chunk]) -> str:
    """Text used for embedding/display: clause text prefixed with its article heading."""
    if chunk.level == "clause" and chunk.parent_id:
        parent = by_id[chunk.parent_id]
        return f"{parent.title}. {chunk.text}"
    if chunk.level == "article":
        return f"{chunk.title}. {chunk.text}".strip()
    return chunk.text


def is_leaf(chunk: Chunk) -> bool:
    """Retrievable unit: every clause, plus articles that have no clauses."""
    return chunk.level == "clause" or (chunk.level == "article" and not chunk.children)
