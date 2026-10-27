"""Knowledge graph store: hierarchical structure, entity mentions, and typed knowledge triples."""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any, Iterable, Optional, Union

from nexus_engine.services.ingestion.chunker import IngestionChunk
from nexus_engine.services.ingestion.graph_builder import KnowledgeTriple
from nexus_engine.services.retrieval.chunker import Chunk

ENTITY_LEXICON = [
    "data controller", "data processor", "sub-processor", "personal data", "data subject",
    "personal data breach", "encryption", "audit log", "access control", "incident response",
    "multi-factor authentication", "retention", "liability", "confidentiality", "termination",
    "supplier", "customer", "backup", "vulnerability", "cá nhân", "pháp nhân", "hợp đồng",
    "năng lực pháp luật", "năng lực hành vi", "giao dịch dân sự", "phạt vi phạm", "bồi thường thiệt hại",
    "procurement", "purchase order", "three-way matching", "finance director", "cfo", "due diligence",
    "export control", "dual-use", "eccn", "sanctions", "moit", "ofac sdn",
]
_ENTITY_RX = {t: re.compile(r"\b" + re.escape(t) + r"s?\b", re.I) for t in ENTITY_LEXICON}
_ARTICLE_REF = re.compile(r"\b(?:article|điều)\s+(\d+)\b", re.I)


def entity_id(term: str) -> str:
    return "entity:" + re.sub(r"[^a-z0-9_-]+", "-", term.lower()).strip("-")


ALIASES = {
    "processor": "data processor",
    "controller": "data controller",
    "breach": "personal data breach",
    "breach notification": "personal data breach",
    "mfa": "multi-factor authentication",
    "po": "purchase order",
}
_ALIAS_RX = {a: re.compile(r"\b" + re.escape(a) + r"s?\b", re.I) for a in ALIASES}


def find_entities(text: str) -> list[str]:
    found = [t for t, rx in _ENTITY_RX.items() if rx.search(text)]
    for alias, canon in ALIASES.items():
        if canon not in found and _ALIAS_RX[alias].search(text):
            found.append(canon)
    return found


class GraphStore:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, str]] = {}
        self._edges: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._adj: dict[str, set[tuple[str, str]]] = defaultdict(set)  # id -> {(other, relation)}
        self.chunks: dict[str, Union[Chunk, IngestionChunk]] = {}
        self._doc_chunks: dict[str, set[str]] = defaultdict(set)

    # ---- building -----------------------------------------------------
    def add_node(self, node_id: str, label: str, type_: str) -> None:
        self.nodes.setdefault(node_id, {"label": label, "type": type_})

    def add_edge(self, source: str, target: str, relation: str, metadata: Optional[dict[str, Any]] = None) -> None:
        key = (source, target, relation)
        if key in self._edges:
            return
        self._edges[key] = metadata or {}
        self._adj[source].add((target, relation))
        self._adj[target].add((source, relation))

    def clear(self) -> None:
        self.nodes.clear()
        self._edges.clear()
        self._adj.clear()
        self.chunks.clear()
        self._doc_chunks.clear()

    def load_precomputed(self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        """Hydrate graph nodes and edges directly from persistent SQLite storage."""
        self.clear()
        for n in nodes:
            self.nodes[n["id"]] = {"label": n["label"], "type": n["entity_type"]}
        for e in edges:
            self.add_edge(e["source_id"], e["target_id"], e["relation"], {"weight": e.get("weight", 1.0)})

    def ingest(self, chunks: Iterable[Union[Chunk, IngestionChunk]]) -> None:
        article_entities: dict[str, set[str]] = defaultdict(set)
        for c in chunks:
            self.chunks[c.id] = c
            doc_id = getattr(c, "doc_id", c.id.split(":")[0])
            self._doc_chunks[doc_id].add(c.id)

            label = getattr(c, "label", c.id)
            self.add_node(c.id, label, c.level)
            if c.parent_id:
                self.add_edge(c.parent_id, c.id, "contains")

            text_to_scan = c.title + " " + c.text if getattr(c, "level", "") in ("article", "h2") else c.text
            for term in find_entities(text_to_scan):
                eid = entity_id(term)
                self.add_node(eid, term.title(), "entity")
                self.add_edge(c.id, eid, "mentions")
                art = c.id if c.level in ("article", "h2") else (c.parent_id or c.id)
                article_entities[art].add(eid)

        for ents in article_entities.values():
            ordered = sorted(ents)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1 :]:
                    self.add_edge(a, b, "related_to")

    def ingest_triples(self, triples: Iterable[KnowledgeTriple]) -> None:
        for t in triples:
            h_id = entity_id(t.head) if not t.head.startswith("entity:") and ":" not in t.head else t.head
            t_id = entity_id(t.tail) if not t.tail.startswith("entity:") and ":" not in t.tail else t.tail

            self.add_node(h_id, t.head, t.head_type.lower())
            self.add_node(t_id, t.tail, t.tail_type.lower())
            self.add_edge(h_id, t_id, t.relation.lower(), {"confidence": t.confidence, "source": t.source_chunk_id})

            if t.source_chunk_id in self.chunks:
                self.add_edge(t.source_chunk_id, h_id, "mentions")

    def register_chunks(self, chunks: Iterable[Union[Chunk, IngestionChunk]], doc_map: dict[str, list[str]]) -> None:
        """Make chunks rankable after load_precomputed (which restores only nodes and edges)."""
        for c in chunks:
            self.chunks[c.id] = c
        for doc_id, cids in doc_map.items():
            self._doc_chunks[doc_id].update(cids)

    def remove_document(self, doc_id: str) -> None:
        cids = self._doc_chunks.pop(doc_id, set())
        for cid in cids:
            self.chunks.pop(cid, None)
            self.nodes.pop(cid, None)

        # Clean edges involving removed chunks
        to_del = [k for k in self._edges if k[0] in cids or k[1] in cids]
        for k in to_del:
            self._edges.pop(k, None)
            self._adj[k[0]].discard((k[1], k[2]))
            self._adj[k[1]].discard((k[0], k[2]))

    # ---- queries ------------------------------------------------------
    def neighbors(self, node_id: str, relation: Optional[str] = None) -> list[str]:
        return sorted(o for o, r in self._adj.get(node_id, ()) if relation is None or r == relation)

    def traverse(self, start: str, depth: int = 2) -> list[str]:
        seen = {start}
        q: deque[tuple[str, int]] = deque([(start, 0)])
        out: list[str] = []
        while q:
            n, d = q.popleft()
            if d == depth:
                continue
            for o in self.neighbors(n):
                if o not in seen:
                    seen.add(o)
                    out.append(o)
                    q.append((o, d + 1))
        return out

    def rank_chunks(self, query: str, limit: int = 20) -> list[tuple[str, float]]:
        scores: dict[str, float] = defaultdict(float)
        for term in find_entities(query):
            eid = entity_id(term)
            for c in self.neighbors(eid, "mentions"):
                scores[c] += 2.0
            for rel in self.neighbors(eid, "related_to"):
                for c in self.neighbors(rel, "mentions"):
                    scores[c] += 1.0

        for num in _ARTICLE_REF.findall(query):
            for nid, c in self.chunks.items():
                c_num = getattr(c, "number", None) or (c.metadata.get("article_number") if hasattr(c, "metadata") else None)
                if getattr(c, "level", "") == "article" and str(c_num) == str(num):
                    children = getattr(c, "children", [])
                    for leaf in children or [nid]:
                        scores[leaf] += 3.0

        expanded: dict[str, float] = defaultdict(float)
        for cid, s in scores.items():
            c = self.chunks.get(cid)
            if c is None:
                continue
            children = getattr(c, "children", [])
            if getattr(c, "level", "") == "article" and children:
                for ch in children:
                    expanded[ch] += s * 0.5
            else:
                expanded[cid] += s

        return sorted(expanded.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    def snapshot(self) -> dict[str, list[dict[str, str]]]:
        return {
            "nodes": [{"id": i, "label": n["label"], "type": n["type"]} for i, n in self.nodes.items()],
            "edges": [{"source": s, "target": t, "relation": r} for (s, t, r) in self._edges],
        }
