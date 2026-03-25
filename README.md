# SAP O2C Graph-Based Data Modeling and Query System

> **Live Demo:** [https://o2c-graph-query-system.vercel.app/]
> **GitHub:** [https://github.com/DivyanshJain04/o2c-graph-query-system]
> **Built with:** FastAPI · SQLite · Groq LLaMA 3.3 70B · React · Cytoscape.js

A full-stack system that unifies fragmented SAP Order-to-Cash data into an interactive knowledge graph and allows users to explore entity relationships and query the data using natural language.

---

## What The System Does

- Ingests 19 SAP O2C entities from JSONL files into a typed SQLite database (REAL/INTEGER for numeric fields)
- Builds a knowledge graph of connected business entities via their real FK relationships
- Lets users explore nodes, inspect all properties, and follow connections interactively
- Accepts natural language questions, generates SQL dynamically via Groq LLaMA 3.3 70B, executes it, and returns data-grounded answers
- Highlights graph nodes referenced in chat responses in real time
- Enforces strict domain guardrails — off-topic queries are rejected at the prompt level

---

## The O2C Flow This System Models

```
Business Partner (Customer)
        │
        ▼
Sales Order Header  ──────────► Sales Order Item ──► Product
        │
        ▼
Outbound Delivery Header ◄────── Outbound Delivery Item
        │
        ▼
Billing Document Header ◄──────── Billing Document Item
        │
        ▼
Journal Entry (Accounts Receivable)
        │
        ▼
Payment (Accounts Receivable)
```

---

## Repository Structure

```
o2c-graph-query-system/
├── backend/
│   ├── ingest.py          # JSONL → SQLite with type inference + FK indexes
│   ├── main.py            # FastAPI: graph, node, chat, schema, health APIs
│   ├── requirements.txt
│   ├── .env.example
│   └── data/              # SAP O2C JSONL dataset
├── frontend/
│   ├── src/
│   │   ├── App.jsx                    # Main layout: graph + chat
│   │   ├── components/
│   │   │   └── MessageCard.jsx        # Chat message rendering component
│   │   └── styles.css
│   ├── .env.example
│   └── package.json
├── sessions/
│   ├── cursor_graph_based_data_modeling_and_fa.md   # Initial build session
│   ├── cursor_production_quality_sap_o2c_graph.md   # Production upgrade session
│   └── cursor_system_upgrade_for_production_qu.md   # Final hardening session
├── .gitignore
└── README.md
```

---

## Architecture Decisions

### SQLite over Neo4j

The dataset has clear relational structure — entities link via FK columns (`salesOrder`, `billingDocument`, `accountingDocument`). Storing in SQLite means:

- The LLM generates standard SQL, which it does far more reliably than Cypher or SPARQL
- Zero server setup — ingestion runs in seconds and works identically locally and on Render
- Full analytical queries run on the complete dataset, not just the visualization sample
- FK indexes on all join columns keep query latency low

A graph database would add operational complexity (hosting, auth, query language) without improving query quality for this dataset size.

### Groq + LLaMA 3.3 70B over Gemini or OpenAI

Initial integration used Gemini 1.5 Flash. During development it returned 404 errors — `gemini-1.5-flash` had been deprecated. This is documented in the session logs.

Groq was chosen as the replacement:
- **Speed** — sub-second inference on Groq's custom chips
- **Reliability** — `llama-3.3-70b-versatile` is a stable production model
- **SQL quality** — LLaMA 3.3 70B generates accurate multi-JOIN SQL reliably

Model name is stored in `GROQ_MODEL` env var — future deprecation requires only a one-line `.env` change.

### Two-Stage LLM Pipeline

```
User question
      │
      ▼
Stage 1 — Groq: schema + join paths + guardrails → { sql }
      │
      ▼
Execute SQL against full SQLite database
      │
      ▼
Stage 2 — Groq: question + SQL + real results → natural language answer
      │
      ▼
Response: { answer, sql, results, request_id }
```

Separating SQL generation from answer generation reduces hallucination — Stage 2 only interprets data that was actually retrieved.

### SQL Retry on Error

If Stage 1 SQL fails execution, the system makes a repair call to Groq with the failed SQL and the exact SQLite error. Handles column name mismatches and alias errors automatically.

### Type-Inferred Ingestion

`ingest.py` applies heuristic type inference at ingestion time:
- Columns containing `amount`, `price`, `quantity` → **REAL**
- Integer-style ID columns (digits, no leading zeros, ≤18 chars) → **INTEGER**
- SAP ERP IDs with leading zeros (e.g., `0000740506`) stay as **TEXT** to preserve identity
- Everything else → **TEXT**

This ensures `SUM(totalNetAmount)`, `ORDER BY netAmount DESC`, and numeric comparisons behave correctly.

### Multi-Layer SQL Safety

1. **Prompt-level** — LLM instructed to generate only SELECT statements
2. **Keyword deny-list** — DROP, DELETE, UPDATE, INSERT, ALTER, PRAGMA, ATTACH, DETACH blocked
3. **Schema validation** — referenced tables and qualified column references (`alias.column`) validated against live schema before execution
4. **Timeout** — SQLite progress handler aborts queries running longer than 2.5 seconds
5. **Row cap** — maximum 500 rows returned per query

### In-Memory Graph Cache + Connected-Only Rendering

Graph construction runs once and is cached in memory. Only nodes that appear in at least one edge are returned in the visualization — isolated nodes are excluded from the graph view but remain available for direct lookup via the node detail API.

---

## Graph Entity Model

| Entity | Role in O2C |
|--------|-------------|
| `business_partners` | Customers who place orders |
| `sales_order_headers` | The order itself |
| `sales_order_items` | Line items within an order |
| `products` | Materials/products ordered |
| `outbound_delivery_headers` | Shipment record |
| `outbound_delivery_items` | Items within a delivery |
| `billing_document_headers` | Invoice for the delivery |
| `billing_document_items` | Line items on the invoice |
| `journal_entry_items_accounts_receivable` | Accounting entry for billing |
| `payments_accounts_receivable` | Payment clearing record |

9 FK relationships define the edges. Only edges where both endpoints exist in the sampled node set are rendered.

---

## LLM Prompting Strategy

System prompt includes:

1. **Role** — strict SQL planner, SAP O2C domain only
2. **Live schema** — exact table/column names from `PRAGMA table_info` at startup
3. **Sample values** — 2-3 distinct values per key column for grounding
4. **Join paths** — 9 explicit FK paths covering the full O2C chain
5. **Billing trace example** — a complete 6-table LEFT JOIN for end-to-end document tracing
6. **SQL rules** — no CTEs, no recursive queries, no subqueries as table sources
7. **Output contract** — strict JSON `{ sql, answer }`, no extra text
8. **Off-topic rejection** — exact rejection string with concrete examples

---

## Guardrails

**Layer 1 — Prompt:** LLM instructed to return `null` SQL and the exact rejection string for non-dataset queries. Concrete examples embedded in the prompt.

**Layer 2 — SQL enforcement:** Multi-layer safety (keyword deny-list + schema validation + timeout + row cap).

**Layer 3 — UI:** Rejection responses render in a distinct amber card, visually distinct from normal answers.

---

## Bonus Features

- **Node highlighting** — nodes referenced in chat answers glow yellow; cleared on next query
- **Conversation memory** — full history sent with every request
- **SQL retry** — automatic one-shot repair with exact SQLite error context
- **Request tracing** — every request logs with a UUID; `/api/chat` returns `request_id` on unhandled errors
- **Cold start handling** — frontend retries once on 502/503/504 with a "waking up" message

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check — `{ status: "ok" }` |
| GET | `/api/graph` | Connected graph `{ nodes, edges }` |
| GET | `/api/graph/node/{type}/{id}` | Node details + connected nodes/edges |
| GET | `/api/schema` | All tables and columns |
| POST | `/api/chat` | `{ message, history }` → `{ answer, sql, results }` |

---

## Demo Queries

| Question | Expected Answer |
|----------|----------------|
| How many sales orders are in the dataset? | 100 |
| Which product has the most billing documents? | S8907367039280 — 22 billing documents |
| Trace billing document 90504259 | Sales order 740560, delivery 80738080 |
| Show sales orders delivered but not billed | LEFT JOIN query — orders with delivery but no billing document |
| Show top 10 customers by total net amount billed | Ranked list with INR amounts |

---

## Local Setup

### Prerequisites
- Python 3.9+
- Node.js 18+
- Free [Groq API key](https://console.groq.com)

### 1. Backend

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\Activate.ps1
# Mac/Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# Set GROQ_API_KEY in .env
```

### 2. Ingest

```bash
cd backend
python ingest.py
# Creates database.db with 19 typed tables + 18 FK indexes
```

### 3. Start API

```bash
uvicorn main:app --reload --port 8000
```

### 4. Start Frontend

```bash
cd frontend
npm install
npm run dev
# Open http://localhost:3000
```

### Environment Variables

**backend/.env**
```
GROQ_API_KEY=your_key_here
GROQ_MODEL=llama-3.3-70b-versatile
```

**frontend/.env** (production only)
```
VITE_API_BASE_URL=https://your-backend.onrender.com
```

---

## Deployment (Render + Vercel)

**Backend — Render Web Service:**
- Root Directory: `backend`
- Build: `pip install -r requirements.txt && python ingest.py`
- Start: `uvicorn main:app --host 0.0.0.0 --port 10000`
- Env vars: `GROQ_API_KEY`, `GROQ_MODEL`

**Frontend — Vercel:**
- Root Directory: `frontend`
- Build: `npm run build`
- Output: `dist`
- Env var: `VITE_API_BASE_URL=https://your-backend.onrender.com`

---

## Observability

The backend logs every request with structured output:
- **Chat requests** — user question, generated SQL, row count, execution time (ms)
- **SQL failures** — failed SQL + SQLite error, retry attempts
- **LLM failures** — Groq errors, JSON parse failures
- **Request tracing** — UUID in `x-request-id` response header and in error payloads

---

## Limitations

- **Graph is for exploration** — sampled and visualizes connected nodes only. Authoritative analytics use the full SQLite database via chat.
- **SQL validation is best-effort** — table and qualified column references are validated; unusual patterns (subqueries, CTEs) are rejected conservatively.
- **Free tier cold starts** — Render free tier spins down after inactivity. First request may take 30–60 seconds. Frontend shows a retry message.

---

## What Broke During Development

- **Gemini deprecation** — `gemini-1.5-flash` returned 404 mid-build. Switched to Groq. Model now configurable via env var so future deprecations need only a one-line change.
- **SQL column guessing** — initial prompts let the LLM guess column names, causing execution failures. Fixed by injecting exact PRAGMA-derived column names at startup.
- **Graph performance** — initial 50-node sampling produced a cluttered unusable graph. Reduced to 20 connected nodes per entity; isolated nodes now excluded from visualization.
- **Numeric aggregations** — all-TEXT storage caused `SUM()` and `ORDER BY` on amount fields to sort lexicographically. Fixed by type inference in `ingest.py`.

---

## What I Would Improve With More Time

- **Full date typing** — key numeric fields (amount, price, quantity) are typed as REAL/INTEGER at ingestion. ISO date strings like `creationDate` could be stored with DATE affinity for correct chronological filtering.
- **Streaming responses** — chat waits for the full Groq response before rendering. Streaming tokens would feel significantly faster.
- **Real graph database** — Neo4j would enable native path traversal queries ("find all broken O2C flows") without multi-JOIN SQL. SQLite is the right tradeoff for this dataset size and demo timeframe.
- **Auth + multi-tenancy** — CORS is open and there is no auth. Production deployment for a real enterprise customer would need both.
