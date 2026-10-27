"""Entity-Relation extraction pipeline generating knowledge graph triples (Head-Relation-Tail)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from nexus_engine.services.ingestion.chunker import IngestionChunk


@dataclass
class KnowledgeTriple:
    head: str
    head_type: str
    relation: str
    tail: str
    tail_type: str
    source_chunk_id: str
    confidence: float = 1.0


class GraphBuilder:
    def __init__(self) -> None:
        # Regex heuristics for fast deterministic triplet extraction
        self._rx_obligation_vi = re.compile(r"(cá nhân|pháp nhân|bên vi phạm|người đại diện|người có nghĩa vụ)\s+(phải|có nghĩa vụ)\s+([^.,;]+)", re.I)
        self._rx_prohibition_vi = re.compile(r"(cá nhân|pháp nhân|mọi người|nghiêm cấm)\s+(không được|bị cấm)\s+([^.,;]+)", re.I)
        self._rx_obligation_en = re.compile(r"([A-Za-z\s]+)\s+(?:shall|must|is required to)\s+([^.,;]+)", re.I)
        self._rx_prohibition_en = re.compile(r"([A-Za-z\s]+)\s+(?:shall not|is prohibited from|must not)\s+([^.,;]+)", re.I)
        self._rx_cross_ref_vi = re.compile(r"(?:Điều|quy định tại Điều)\s+(\d+)", re.I)
        self._rx_cross_ref_en = re.compile(r"(?:Article|under Article|pursuant to Article)\s+(\d+)", re.I)
        self._rx_approval_sop = re.compile(r"\$([0-9,]+(?:\s+to\s+\$[0-9,]+)?)\s*.*?([A-Za-z\s]+?(?:Manager|Head|Director|VP|CFO|CEO|Board))", re.I)
        self._rx_eccn = re.compile(r"\b(ECCN|dual-use|encryption|sanctioned)\b.{0,30}\b([A-Za-z0-9_-]+)", re.I)

    def extract_triples(self, chunks: Sequence[IngestionChunk]) -> list[KnowledgeTriple]:
        triples: list[KnowledgeTriple] = []

        for chunk in chunks:
            # 1. Structural hierarchy relation: Parent -> CONTAINS -> Child
            if chunk.parent_id:
                triples.append(
                    KnowledgeTriple(
                        head=chunk.parent_id,
                        head_type="Section",
                        relation="CONTAINS",
                        tail=chunk.id,
                        tail_type="Section",
                        source_chunk_id=chunk.id,
                        confidence=1.0,
                    )
                )

            # 2. Extract Cross-references (e.g. Điều 16 -> Điều 83, Article 4 -> Article 5)
            for m in self._rx_cross_ref_vi.finditer(chunk.text):
                art_num = m.group(1)
                triples.append(
                    KnowledgeTriple(
                        head=chunk.title or chunk.id,
                        head_type="Statute",
                        relation="REFERENCES_STATUTE",
                        tail=f"Điều {art_num}",
                        tail_type="Statute",
                        source_chunk_id=chunk.id,
                        confidence=0.9,
                    )
                )
            for m in self._rx_cross_ref_en.finditer(chunk.text):
                art_num = m.group(1)
                triples.append(
                    KnowledgeTriple(
                        head=chunk.title or chunk.id,
                        head_type="Statute",
                        relation="REFERENCES_STATUTE",
                        tail=f"Article {art_num}",
                        tail_type="Statute",
                        source_chunk_id=chunk.id,
                        confidence=0.9,
                    )
                )

            # 3. Legal Obligations & Prohibitions (Vietnamese & English)
            for m in self._rx_obligation_vi.finditer(chunk.text):
                subj = m.group(1).strip()
                action = m.group(3).strip()
                if 3 < len(action) < 60:
                    triples.append(
                        KnowledgeTriple(
                            head=subj,
                            head_type="LegalEntity",
                            relation="OBLIGATED_TO",
                            tail=action,
                            tail_type="Obligation",
                            source_chunk_id=chunk.id,
                            confidence=0.85,
                        )
                    )
            for m in self._rx_prohibition_vi.finditer(chunk.text):
                subj = m.group(1).strip()
                action = m.group(3).strip()
                if 3 < len(action) < 60:
                    triples.append(
                        KnowledgeTriple(
                            head=subj,
                            head_type="LegalEntity",
                            relation="PROHIBITED_FROM",
                            tail=action,
                            tail_type="Prohibition",
                            source_chunk_id=chunk.id,
                            confidence=0.85,
                        )
                    )
            for m in self._rx_obligation_en.finditer(chunk.text):
                subj = m.group(1).strip()
                action = m.group(2).strip()
                if 3 < len(subj) < 40 and 3 < len(action) < 60:
                    triples.append(
                        KnowledgeTriple(
                            head=subj,
                            head_type="Role",
                            relation="MANDATES",
                            tail=action,
                            tail_type="Obligation",
                            source_chunk_id=chunk.id,
                            confidence=0.85,
                        )
                    )

            # 4. Procurement Approval Thresholds (SOP)
            if "procurement" in chunk.doc_id or "sop" in chunk.doc_id:
                for m in self._rx_approval_sop.finditer(chunk.text):
                    threshold = "$" + m.group(1).strip()
                    approver = m.group(2).strip()
                    triples.append(
                        KnowledgeTriple(
                            head=f"Spend {threshold}",
                            head_type="Threshold",
                            relation="REQUIRES_APPROVAL_FROM",
                            tail=approver,
                            tail_type="Authority",
                            source_chunk_id=chunk.id,
                            confidence=0.95,
                        )
                    )

            # 5. Dual-use & Export Controls
            if "export" in chunk.doc_id or "control" in chunk.doc_id:
                if "MOIT" in chunk.text or "license" in chunk.text or "giấy phép" in chunk.text:
                    triples.append(
                        KnowledgeTriple(
                            head="Dual-Use Export",
                            head_type="ControlledActivity",
                            relation="REQUIRES_LICENSE_FROM",
                            tail="Ministry of Industry and Trade (MOIT)",
                            tail_type="RegulatoryBody",
                            source_chunk_id=chunk.id,
                            confidence=0.9,
                        )
                    )
                if "OFAC SDN" in chunk.text or "sanctions" in chunk.text:
                    triples.append(
                        KnowledgeTriple(
                            head="Foreign Buyer",
                            head_type="CommercialParty",
                            relation="SUBJECT_TO_SCREENING",
                            tail="Global Sanctions Registries (OFAC SDN / UN)",
                            tail_type="ComplianceRegistry",
                            source_chunk_id=chunk.id,
                            confidence=0.95,
                        )
                    )

        unique_triples: list[KnowledgeTriple] = []
        seen = set()
        for t in triples:
            key = (t.head.strip().lower(), t.relation.strip().lower(), t.tail.strip().lower(), t.source_chunk_id)
            if key not in seen:
                seen.add(key)
                unique_triples.append(t)
        return unique_triples
