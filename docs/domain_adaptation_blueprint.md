# Domain Adaptation Blueprint: Rapid Solution Development with Nexus Engine SDK

> **Architecture Specification & Field Guide**  
> **Platform Engine:** [`nexus-engine` (v0.2.0)](../README.md)  
> **Target Audiences:** Enterprise AI Architects, Lead Engineers, Technical Evaluation Panels  
> **Target Delivery SLA:** < 48 Hours from domain requirements to functional production MVP

---

## 1. Executive Concept & Value Proposition

Enterprise agent projects often suffer from monolithic stagnation: whenever a new domain arrives (e.g., Banking, Healthcare, Pharmaceutical, Supply Chain), development teams rewrite agent state loops, guardrail regex engines, hybrid retrieval stores, token compactors, and audit loggers from scratch.

`nexus-engine` solves this with a **Strict Inversion of Control & Extension Model**:
- **85%+ Reusable Core Platform**: Orchestration state machine, dual-process safety guardrail (Noisy-OR regex + LLM verifier), SQLite vector-graph CTE retriever, token budget manager, SHA-256 tamper-evident ledger, and OS sandbox executor.
- **<15% Configurable Domain Specialization**: Domain knowledge parsers, domain safety/compliance rules (`rules.json`), domain knowledge graph schemas, and domain persona prompts.

```
                              +---------------------------------------+
                              |        nexus-engine (Core SDK)        |
                              |   - ReAct Orchestrator & State Graph  |
                              |   - Dual-Process Guardrail Framework  |
                              |   - Hybrid Vector-Graph CTE Engine    |
                              |   - Token Compactor & Langfuse Tracer |
                              |   - SHA-256 Tamper-Evident Ledger     |
                              |   - Quota-Bounded OS Sandbox Executor |
                              +-------------------+-------------------+
                                                  |
                                                  | [pip install nexus-engine]
                         +------------------------+------------------------+
                         |                                                 |
                         v                                                 v
        +---------------------------------+               +---------------------------------+
        |   bank-credit-guard (Banking)   |               |    clinical-guard (Healthcare)  |
        |   - SBV Circular 39/06 Parsers  |               |    - Clinical Protocol Parsers  |
        |   - ApexBank QC-TD-CN-2024 Graph|               |    - ICD-10 / Drug Interax Graph|
        |   - Decree 13 PII & CCCD Filter |               |    - HIPAA PII / Dosage Filter  |
        |   - Maker-Checker Directives    |               |    - Attending Physician Prompts|
        |   - 120 Golden Banking Evals    |               |    - 100 Clinical Trial Evals   |
        +---------------------------------+               +---------------------------------+
```

---

## 2. 4-Step Adaptation Workflow (< 48 Hours)

Building any new enterprise solution requires only 4 declarative steps:

### Step 1: Initialize Project & Dependency Declaration (0.5 hour)
Declare `nexus-engine` as your core platform engine in `pyproject.toml`:

```toml
[project]
name = "clinical-guard"
version = "0.1.0"
dependencies = [
    "nexus-engine>=0.2.0",
]
```

### Step 2: Define Domain Knowledge Schema & Ingestion (4–8 hours)
Implement a custom document parser by subclassing or configuring `IngestionPipeline`:

```python
from pathlib import Path
from nexus_engine.services.ingestion.base import BaseDocumentParser, ParsedDocument
from nexus_engine.services.storage.schema import KnowledgeNode, KnowledgeEdge

class ClinicalProtocolParser(BaseDocumentParser):
    def parse(self, file_path: Path) -> ParsedDocument:
        # 1. Parse clinical guidelines / drug monographs
        # 2. Extract entities: [Drug, Contraindication, DosageCondition]
        # 3. Create relational graph edges: [STRICTLY_CONTRAINDICATED, ADVERSE_INTERACTION]
        nodes = [
            KnowledgeNode(
                id="DRUG-WARFARIN",
                type="Drug",
                title="Warfarin Sodium",
                content="Oral anticoagulant. Contraindicated with high-dose NSAIDs...",
            )
        ]
        edges = [
            KnowledgeEdge(
                source_id="DRUG-WARFARIN",
                target_id="DRUG-ASPIRIN",
                type="ADVERSE_INTERACTION",
                properties={"severity": "major", "mechanism": "synergistic bleeding"}
            )
        ]
        return ParsedDocument(nodes=nodes, edges=edges)
```

Ingest domain documents directly into the zero-overhead embedded SQLite database using recursive Common Table Expressions (CTE).

### Step 3: Configure Domain Guardrail Rules (2–4 hours)
Define domain-specific risks and PII patterns in `data/guardrails/rules.json`:

```json
{
  "categories": {
    "healthcare_pii": {
      "patterns": [
        "\\bMRN-\\d{8}\\b",
        "\\b(Medical Record Number|Health Insurance ID)[:\\s]+[A-Z0-9-]+"
      ],
      "weight": 0.95
    },
    "unauthorized_prescription": {
      "patterns": [
        "override (dosage|prescription|contraindication)",
        "ignore maximum dose limit"
      ],
      "weight": 0.98
    }
  }
}
```

The `FastFilter` compiles these regex signals to execute sub-millisecond pre-flight checks ($<1\text{ ms}$).

### Step 4: Inject Domain Prompts & Custom LLM Provider (2–4 hours)
Subclass `Container` to wire domain prompts, response schemas, and human-in-the-loop (HITL) directives:

```python
from nexus_engine import Settings, Container, build_container as nexus_build_container

class ClinicalContainer:
    def __init__(self, settings: Settings):
        self.engine_container = nexus_build_container(settings)
        # Register custom clinical tools or prompt personas
        self.engine_container.agent.system_prompt = (
            "You are ClinicalGuard, an institutional healthcare AI copilot. "
            "You must cross-examine patient history against clinical knowledge graph contraindications. "
            "Always output: VERDICT (APPROVED / CONTRAINDICATED / WARNING), "
            "CLINICAL BASIS, RISK ALERT, and ATTENDING PHYSICIAN DIRECTIVE."
        )

    def run(self, query: str, session_id: str):
        return self.engine_container.agent.run(query=query, session_id=session_id)
```

---

## 3. Comparison Matrix: Three Enterprise Verticals on `nexus-engine`

| Domain Dimension | Banking Solution (`bank-credit-guard`) | Healthcare Solution (`clinical-guard`) | Chemical / Industrial (`hazmat-guard`) |
| :--- | :--- | :--- | :--- |
| **Statutory Knowledge** | SBV Circulars 39 & 06, ApexBank Policy | FDA Protocols, Clinical Guidelines, ICD-10 | OSHA Standards, REACH, GHS MSDS Sheets |
| **Graph Edge Semantics**| `AMENDS`, `SUPERSEDES`, `STRICTLY_PROHIBITS` | `CONTRAINDICATED_WITH`, `SYNERGISTIC_RISK` | `REACTS_VIOLENTLY_WITH`, `STORAGE_INCOMPATIBLE` |
| **Safety Guardrail** | Decree 13/2023/ND-CP (CCCD, Bank Accounts) | HIPAA / PHI (MRN, Diagnostic records) | Controlled Precursor / Bio-hazard exfiltration |
| **Output Mandate** | Maker-Checker Credit Memorandum | Attending Physician Review Order | Hazmat Safety Handling Ticket |
| **Development Time** | Reference implementation | **< 36 Hours** | **< 48 Hours** |

---

## 4. Key Architectural Takeaways for Stakeholders & Reviewers

1. **Zero Reinvention**:
   Core reliability engineering (SSRF defense, sandboxed code execution, memory pruning, SHA-256 hash chaining) is already hardened and tested in `nexus-engine`.
2. **Deterministic & Inspectable**:
   Every domain extension inherits the same tamper-evident audit ledger and quantitative evaluation harness.
3. **True Agility for Enterprise AI Platforms**:
   Squads can launch production-ready enterprise vertical agents within days instead of months.
