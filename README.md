<div align="center">

# 🌿 VANA

### AI-Powered Ayurvedic Knowledge Graph & Research Platform

*From a plant name to its chemistry, bioactivity, targets, and evidence, all in one connected graph.*

<br/>

![React](https://img.shields.io/badge/React-20232A?style=for-the-badge&logo=react&logoColor=61DAFB)
![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?style=for-the-badge&logo=typescript&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)
![Neo4j](https://img.shields.io/badge/Neo4j-008CC1?style=for-the-badge&logo=neo4j&logoColor=white)
![Gemini](https://img.shields.io/badge/Gemini-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![AWS](https://img.shields.io/badge/AWS_EC2-FF9900?style=for-the-badge&logo=amazonec2&logoColor=white)

<br/>

**118** Plants &nbsp;·&nbsp; **2,446** Phytochemicals &nbsp;·&nbsp; **3,306** Bioactivities &nbsp;·&nbsp; **821** Research Documents

</div>

---

## 📖 Overview

Ayurvedic knowledge is scattered across botanical records, traditional-use texts, phytochemical databases, and scientific papers. **VANA** connects all of it into one graph so you can explore it end to end.

VANA combines four things:

| | Capability | What it does |
|---|---|---|
| 🧠 | **Semantic plant resolution** | Understands names, aliases, and typos, and resolves them against the existing graph first |
| 🕸️ | **Knowledge graph** | Models plants, parts, chemicals, uses, bioactivities, targets, and documents in Neo4j |
| ⚙️ | **Automated ingestion** | Pulls in new plants through a validated, cacheable, repeatable pipeline |
| 🔬 | **Evidence linking** | Ties each biological observation back to the research document that reports it |

---

## ✨ Key Features

### 📚 Evidence-Backed Data, Curated in One Place
VANA integrates nine trusted scientific and botanical sources into a single graph, so each fact stays traceable to where it came from.

| Source | What it contributes |
|---|---|
| **IMPPAT** | Phytochemicals, plant-part associations, therapeutic-use mappings |
| **ChEMBL** | Bioactivities, assays, targets, publications |
| **UniProt** | Protein / target identity resolution |
| **MeSH / NLM** | Therapeutic-use normalization and medical subject headings |
| **PubMed** | Supporting literature |
| **GBIF** | Botanical taxonomy and species resolution |
| **NCBI Taxonomy** | Taxonomy IDs and common names |
| **IUCN Red List** | Global conservation status |
| **IISc Digital Flora** | Karnataka regional status and plant / common-name information |

### 🧠 Graph-Constrained AI Resolution
Type `Hibicus` and get **Hibiscus rosa-sinensis**. Gemini can only choose from candidates that already exist in the graph, so it can't invent an identity or create a duplicate plant from a typo.

- Scientific names, common names, aliases, and spelling variations
- Deterministic fuzzy guard as a second layer of typo protection
- External resolution and ingestion only when no defensible match exists

### 🔗 Multi-Hop Graph Exploration
Move from a plant to its molecular evidence in one connected path:

`Plant → Plant Part → Phytochemical → Bioactivity → Target → Research Document`

### 🌱 Automated Plant Ingestion
A new plant goes through collection, extraction, canonicalization, and validation before it ever reaches the graph.

### 🛡️ Canonical-First Data Integrity
Canonical data, the Neo4j projection, lookup caches, and temporary outputs are kept separate. The graph can be validated, restored, and re-synced independently.

---

## 🧬 Knowledge Graph Model

```mermaid
graph LR
    P(["🌿 Plant"]):::plant
    PP(["🍃 Plant Part"]):::part
    PC(["⚗️ Phytochemical"]):::chem
    TU(["💊 Therapeutic Use"]):::use
    BA(["🧪 Bioactivity"]):::bio
    T(["🎯 Target"]):::target
    D(["📄 Document"]):::doc

    P -- HAS_PART --> PP
    PP -- CONTAINS --> PC
    PP -- USED_FOR --> TU
    PC -- HAS_BIOACTIVITY --> BA
    BA -- MEASURED_ON --> T
    BA -- REPORTED_IN --> D

    classDef plant fill:#2e7d32,stroke:#1b5e20,color:#fff,stroke-width:2px
    classDef part fill:#66bb6a,stroke:#388e3c,color:#fff,stroke-width:2px
    classDef chem fill:#f9a825,stroke:#f57f17,color:#000,stroke-width:2px
    classDef use fill:#ec407a,stroke:#ad1457,color:#fff,stroke-width:2px
    classDef bio fill:#ab47bc,stroke:#6a1b9a,color:#fff,stroke-width:2px
    classDef target fill:#1e88e5,stroke:#0d47a1,color:#fff,stroke-width:2px
    classDef doc fill:#546e7a,stroke:#263238,color:#fff,stroke-width:2px
```

---

## 🏗️ System Architecture

```mermaid
flowchart LR
    U(["👤 User"]):::user --> F["⚛️ React + TypeScript<br/>Frontend"]:::fe
    F --> N["🔀 Nginx"]:::fe
    N --> A["⚡ FastAPI Backend"]:::be

    A --> R["🧠 Plant & Entity<br/>Resolver"]:::be
    A --> Q["🔍 Graph Query<br/>Engine"]:::be
    A --> I["⚙️ Ingestion<br/>Pipeline"]:::be

    R --> G["✨ Gemini API"]:::ai
    Q --> DB[("🕸️ Neo4j<br/>Knowledge Graph")]:::db
    I --> C[("📁 Canonical<br/>Data Store")]:::db
    I --> S[("💾 Persistent<br/>Shared Caches")]:::cache
    C --> DB

    classDef user fill:#37474f,stroke:#102027,color:#fff,stroke-width:2px
    classDef fe fill:#0288d1,stroke:#01579b,color:#fff,stroke-width:2px
    classDef be fill:#2e7d32,stroke:#1b5e20,color:#fff,stroke-width:2px
    classDef ai fill:#7e57c2,stroke:#4527a0,color:#fff,stroke-width:2px
    classDef db fill:#00838f,stroke:#004d40,color:#fff,stroke-width:2px
    classDef cache fill:#5c6bc0,stroke:#283593,color:#fff,stroke-width:2px
```

---

## 🔄 Data Pipeline

```mermaid
flowchart LR
    A(["🌿 Plant<br/>request"]):::a --> B["🔎 Resolution"]:::b
    B --> C["📥 Data<br/>collection"]:::b
    C --> D["🧩 Extraction<br/>& matching"]:::b
    D --> E["📐 Canonicalization"]:::c
    E --> F["✅ Validation"]:::c
    F --> G[("📁 Canonical<br/>CSV store")]:::d
    G --> H[("🕸️ Neo4j<br/>sync")]:::d

    classDef a fill:#37474f,stroke:#102027,color:#fff,stroke-width:2px
    classDef b fill:#1e88e5,stroke:#0d47a1,color:#fff,stroke-width:2px
    classDef c fill:#8e24aa,stroke:#4a148c,color:#fff,stroke-width:2px
    classDef d fill:#2e7d32,stroke:#1b5e20,color:#fff,stroke-width:2px
```

**Separation of concerns**

| Layer | Role |
|---|---|
| 📁 **Canonical store** | Authoritative structured dataset the graph is built from |
| 💾 **Persistent caches** | Cached external lookups for fewer requests and repeatable ingestion |
| 🕸️ **Neo4j** | Queryable graph projection used by the application |

---

## 📊 Production Graph

<table>
<tr>
<td valign="top">

**Nodes**

| Entity | Count |
|---|---:|
| 🌿 Plants | **118** |
| 🍃 Plant Parts | **612** |
| ⚗️ Phytochemicals | **2,446** |
| 💊 Therapeutic Uses | **552** |
| 🧪 Bioactivities | **3,306** |
| 🎯 Targets | **437** |
| 📄 Documents | **821** |

</td>
<td valign="top">

**Relationships**

| Type | Count |
|---|---:|
| `HAS_PART` | **612** |
| `CONTAINS` | **8,975** |
| `USED_FOR` | **7,528** |
| `HAS_BIOACTIVITY` | **3,818** |
| `MEASURED_ON` | **3,306** |
| `REPORTED_IN` | **3,306** |

</td>
</tr>
</table>

---

## ☁️ Deployment

```mermaid
flowchart LR
    U(["👤 User<br/>Browser"]):::user -- "HTTPS" --> NG

    subgraph EC2["☁️ AWS EC2 · Ubuntu"]
        direction LR
        subgraph DC["🐳 Docker Compose"]
            direction LR
            subgraph FC["Frontend container"]
                NG["🔀 Nginx<br/>serves React build"]:::fe
            end
            subgraph BC["Backend container"]
                API["⚡ FastAPI + Uvicorn"]:::be
                RES["🧠 Resolver"]:::be
                ING["⚙️ Ingestion"]:::be
                DATA[("📁 Canonical data<br/>+ shared caches")]:::store
                API --> RES
                API --> ING
                ING --> DATA
            end
            NG -- "/api proxy" --> API
        end
    end

    RES -- "semantic resolution" --> GEM["✨ Google Gemini API"]:::ai
    API -- "Cypher queries" --> NEO[("🕸️ Neo4j Cloud")]:::db
    ING -- "sync" --> NEO
    ING -- "fetch & cache" --> EXT["🌐 Public data sources<br/>IMPPAT · ChEMBL · UniProt · PubMed<br/>GBIF · NCBI · MeSH · IUCN · IISc"]:::ext

    style EC2 fill:#0f172a,stroke:#38bdf8,stroke-width:2px,color:#e0f2fe
    style DC fill:#1e293b,stroke:#6366f1,stroke-width:2px,color:#e0e7ff
    style FC fill:#0c4a6e,stroke:#38bdf8,stroke-width:1px,color:#e0f2fe
    style BC fill:#064e3b,stroke:#34d399,stroke-width:1px,color:#d1fae5

    classDef user fill:#475569,stroke:#cbd5e1,color:#fff,stroke-width:2px
    classDef fe fill:#0ea5e9,stroke:#bae6fd,color:#fff,stroke-width:2px
    classDef be fill:#10b981,stroke:#a7f3d0,color:#fff,stroke-width:2px
    classDef store fill:#6366f1,stroke:#c7d2fe,color:#fff,stroke-width:2px
    classDef ai fill:#a855f7,stroke:#e9d5ff,color:#fff,stroke-width:2px
    classDef db fill:#0d9488,stroke:#99f6e4,color:#fff,stroke-width:2px
    classDef ext fill:#475569,stroke:#94a3b8,color:#fff,stroke-width:2px
```

The whole stack runs from one `docker compose up`. Only the frontend container is exposed publicly, and it proxies `/api` to the backend.

---

## 🧰 Tech Stack

| Layer | Technologies |
|---|---|
| **Frontend** | React · TypeScript · Vite · Nginx |
| **Backend** | Python · FastAPI · Uvicorn · Pydantic |
| **AI** | Google Gemini API |
| **Graph database** | Neo4j |
| **Data** | Pandas · OpenPyXL · CSV |
| **Data sources** | IMPPAT · ChEMBL · UniProt · MeSH / NLM · PubMed · GBIF · NCBI Taxonomy · IUCN Red List · IISc Digital Flora |
| **Caching** | Persistent JSON caches |
| **Infrastructure** | Docker · Docker Compose · AWS EC2 · Ubuntu |

---

## 🖼️ Screenshots

> Screenshots coming soon. Thank you for your patience.

<!--
<p align="center">
  <img src="docs/assets/plant-search.png" width="49%" />
  <img src="docs/assets/knowledge-graph.png" width="49%" />
</p>
-->

---

## 🚀 Getting Started

```bash
# 1. Clone
git clone https://github.com/parthjain21108/VANA-Ayurvedic-Knowledge-Graph.git
cd VANA-Ayurvedic-Knowledge-Graph

# 2. Configure (see backend/.env.example)
cp backend/.env.example backend/.env

# 3. Run
docker compose up -d --build

# 4. Verify
docker compose ps
```

<details>
<summary><b>🔐 Required environment variables</b></summary>

<br/>

```text
NEO4J_URI
NEO4J_USERNAME
NEO4J_PASSWORD
NEO4J_DATABASE
GEMINI_API_KEY
```

Secrets are never committed. `.env.example` documents the configuration without exposing credentials.

</details>

<details>
<summary><b>🗂️ Project structure</b></summary>

<br/>

```text
VANA/
├── backend/
│   ├── api.py
│   ├── master.py
│   ├── query/
│   ├── ingestion/
│   ├── data_manager/
│   ├── phase1_release/
│   │   └── data/
│   │       ├── canonical/
│   │       └── shared_cache/
│   └── requirements*.txt
├── frontend/
│   ├── src/
│   ├── public/
│   ├── package.json
│   └── vite.config.ts
├── docs/assets/
├── docker-compose.yml
└── README.md
```

</details>

---

## 🧭 Design Principles

- **Graph-first resolution:** prefer existing knowledge over unnecessary external inference.
- **Canonical-first data:** keep structured source data separate from graph projections.
- **Evidence-linked relationships:** connect biological findings to supporting documents.
- **Controlled AI:** use AI for semantic reasoning, constrained by graph-backed candidates.
- **Reproducible ingestion:** persistent caches and canonical snapshots make runs repeatable.

---

## ⚠️ Disclaimer

VANA is a research and knowledge-exploration platform. Its output is not medical advice, diagnosis, or a substitute for consultation with a qualified healthcare professional.

---

<div align="center">

**Connecting traditional knowledge, modern biological data, and AI through a unified knowledge graph.**

Built by **[Parth Jain](https://github.com/parthjain21108)**

</div>
