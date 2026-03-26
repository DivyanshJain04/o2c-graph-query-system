import json
import logging
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.db"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_NODES_PER_ENTITY = int(os.getenv("MAX_NODES_PER_ENTITY", "40"))
SQL_TIMEOUT_SECONDS = float(os.getenv("SQL_TIMEOUT_SECONDS", "2.5"))
SQL_MAX_ROWS = int(os.getenv("SQL_MAX_ROWS", "500"))

logger = logging.getLogger("o2c_api")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
GRAPH_INCLUDED_TABLES = {
    "sales_order_headers",
    "sales_order_items",
    "outbound_delivery_headers",
    "outbound_delivery_items",
    "billing_document_headers",
    "billing_document_items",
    "journal_entry_items_accounts_receivable",
    "payments_accounts_receivable",
    "business_partners",
    "products",
}

ENTITY_ID_COLUMNS = {
    "sales_order_headers": ["salesOrder", "soldToParty"],
    "sales_order_items": ["salesOrder", "material"],
    "outbound_delivery_headers": ["deliveryDocument"],
    "outbound_delivery_items": ["deliveryDocument", "referenceSdDocument"],
    "billing_document_headers": ["billingDocument", "accountingDocument", "soldToParty"],
    "billing_document_items": ["billingDocument", "referenceSdDocument"],
    "billing_document_cancellations": ["billingDocument", "accountingDocument", "soldToParty"],
    "journal_entry_items_accounts_receivable": ["accountingDocument", "referenceDocument"],
    "payments_accounts_receivable": ["accountingDocument", "customer"],
    "business_partners": ["businessPartner"],
    "products": ["product"],
    "plants": ["shippingPoint"],
    "product_descriptions": ["product"],
    "product_plants": ["product", "plant"],
    "product_storage_locations": ["product", "plant", "storageLocation"],
    "sales_order_schedule_lines": ["salesOrder"],
    "customer_company_assignments": ["customer"],
    "customer_sales_area_assignments": ["customer"],
    "business_partner_addresses": ["businessPartner"],
}

RELATIONSHIPS = [
    ("sales_order_items", "salesOrder", "sales_order_headers", "salesOrder", "belongs_to"),
    (
        "outbound_delivery_items",
        "referenceSdDocument",
        "sales_order_headers",
        "salesOrder",
        "references_sales_order",
    ),
    (
        "outbound_delivery_items",
        "deliveryDocument",
        "outbound_delivery_headers",
        "deliveryDocument",
        "belongs_to_delivery",
    ),
    (
        "billing_document_items",
        "referenceSdDocument",
        "outbound_delivery_headers",
        "deliveryDocument",
        "references_delivery",
    ),
    (
        "billing_document_items",
        "billingDocument",
        "billing_document_headers",
        "billingDocument",
        "belongs_to_billing_header",
    ),
    (
        "billing_document_headers",
        "accountingDocument",
        "journal_entry_items_accounts_receivable",
        "accountingDocument",
        "posted_to_journal_entry",
    ),
    (
        "payments_accounts_receivable",
        "accountingDocument",
        "journal_entry_items_accounts_receivable",
        "accountingDocument",
        "pays_journal_entry",
    ),
    (
        "sales_order_headers",
        "soldToParty",
        "business_partners",
        "businessPartner",
        "sold_to_business_partner",
    ),
    ("sales_order_items", "material", "products", "product", "references_product"),
]

JOIN_PATHS = [
    "sales_order_items.salesOrder -> sales_order_headers.salesOrder",
    "outbound_delivery_items.referenceSdDocument -> sales_order_headers.salesOrder",
    "outbound_delivery_items.deliveryDocument -> outbound_delivery_headers.deliveryDocument",
    "billing_document_items.referenceSdDocument -> outbound_delivery_headers.deliveryDocument",
    "billing_document_items.billingDocument -> billing_document_headers.billingDocument",
    "billing_document_headers.accountingDocument -> journal_entry_items_accounts_receivable.accountingDocument",
    "payments_accounts_receivable.accountingDocument -> journal_entry_items_accounts_receivable.accountingDocument",
    "sales_order_headers.soldToParty -> business_partners.businessPartner",
    "sales_order_items.material -> products.product",
]

app = FastAPI(title="SAP O2C Graph & Query API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GRAPH_CACHE: Optional[Dict[str, Any]] = None
SCHEMA_CACHE: Dict[str, List[str]] = {}


@app.middleware("http")
async def request_logger(request: Request, call_next):
    start = time.perf_counter()
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    try:
        response = await call_next(request)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": getattr(response, "status_code", None),
                "elapsed_ms": elapsed_ms,
            },
        )
        response.headers["x-request-id"] = request_id
        return response
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        logger.exception(
            "request_error",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "elapsed_ms": elapsed_ms,
                "error": str(exc),
            },
        )
        raise


@app.get("/")
def health() -> Dict[str, str]:
    return {"status": "ok"}


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_tables(conn: sqlite3.Connection) -> List[str]:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return [row["name"] for row in cursor.fetchall() if not row["name"].startswith("sqlite_")]


def get_table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    cursor = conn.execute(f'PRAGMA table_info("{table}")')
    return [row["name"] for row in cursor.fetchall()]


def get_schema_map(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    schema: Dict[str, List[str]] = {}
    for table in get_tables(conn):
        schema[table] = get_table_columns(conn, table)
    return schema


def load_schema_cache() -> None:
    global SCHEMA_CACHE
    conn = get_connection()
    SCHEMA_CACHE = get_schema_map(conn)
    conn.close()


def safe_value_for_id(value: Any) -> str:
    if value is None:
        return "null"
    text = str(value)
    return text.strip() if text.strip() else "empty"


def make_node_id(node_type: str, row: sqlite3.Row) -> str:
    id_columns = ENTITY_ID_COLUMNS.get(node_type)
    if not id_columns:
        # Fallback: stable, deterministic serialization of full row.
        return f"{node_type}:{json.dumps(dict(row), sort_keys=True, ensure_ascii=True)}"
    parts = []
    for col in id_columns:
        parts.append(f"{col}={safe_value_for_id(row[col]) if col in row.keys() else 'missing'}")
    return f"{node_type}|" + "|".join(parts)


def row_to_node(table: str, row: sqlite3.Row) -> Dict[str, Any]:
    row_dict = dict(row)
    node_id = make_node_id(table, row)
    label_parts = []
    for c in ENTITY_ID_COLUMNS.get(table, [])[:2]:
        if c in row_dict and row_dict[c] not in (None, ""):
            label_parts.append(str(row_dict[c]))
    label = " | ".join(label_parts) if label_parts else node_id
    return {"id": node_id, "label": label, "type": table, "properties": row_dict}


def fetch_sample_nodes(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    tables = [t for t in get_tables(conn) if t in GRAPH_INCLUDED_TABLES]

    for table in tables:
        cursor = conn.execute(
            f'SELECT * FROM "{table}" ORDER BY RANDOM() LIMIT ?', (MAX_NODES_PER_ENTITY,)
        )
        rows = cursor.fetchall()
        for row in rows:
            node = row_to_node(table, row)
            nodes.append(node)
    return nodes


def fetch_edges(conn: sqlite3.Connection, sampled_nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    edges: List[Dict[str, str]] = []
    seen = set()
    node_ids = {n["id"] for n in sampled_nodes}
    node_ids_by_table: Dict[str, Set[str]] = {}
    for node in sampled_nodes:
        node_ids_by_table.setdefault(node["type"], set()).add(node["id"])

    sampled_rows_by_table: Dict[str, List[sqlite3.Row]] = {}
    for table in GRAPH_INCLUDED_TABLES:
        sampled_rows_by_table[table] = conn.execute(
            f'SELECT * FROM "{table}" ORDER BY RANDOM() LIMIT ?', (MAX_NODES_PER_ENTITY,)
        ).fetchall()

    for source_table, source_col, target_table, target_col, rel_name in RELATIONSHIPS:
        if source_table not in GRAPH_INCLUDED_TABLES or target_table not in GRAPH_INCLUDED_TABLES:
            continue
        source_rows = sampled_rows_by_table.get(source_table, [])
        target_rows = sampled_rows_by_table.get(target_table, [])
        target_lookup: Dict[str, str] = {}
        for t_row in target_rows:
            t_node_id = make_node_id(target_table, t_row)
            if t_node_id not in node_ids_by_table.get(target_table, set()):
                continue
            target_value = t_row[target_col] if target_col in t_row.keys() else None
            if target_value not in (None, ""):
                target_lookup[str(target_value)] = t_node_id
        for row in source_rows:
            source_node_id = make_node_id(source_table, row)
            if source_node_id not in node_ids_by_table.get(source_table, set()):
                continue
            fk_value = row[source_col] if source_col in row.keys() else None
            if fk_value in (None, ""):
                continue
            target_node_id = target_lookup.get(str(fk_value))
            if not target_node_id:
                continue
            if source_node_id not in node_ids or target_node_id not in node_ids:
                continue
            key = (source_node_id, target_node_id, rel_name)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                {"source": source_node_id, "target": target_node_id, "relationship": rel_name}
            )
    return edges


def call_groq(messages: List[Dict[str, str]]) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set.")
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.1,
    }
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    response = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices", [])
    if not choices:
        return ""
    return str(choices[0].get("message", {}).get("content", "")).strip()


def extract_json_object(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Model response does not contain valid JSON object.")
    obj_text = raw[start : end + 1]
    parsed = json.loads(obj_text)
    if not isinstance(parsed, dict):
        raise ValueError("JSON payload must be an object.")
    return parsed


def parse_model_json_with_retry(system_prompt: str, user_prompt: str) -> Tuple[Dict[str, Any], str]:
    """
    Calls the model and parses a JSON object. If parsing fails, retries once with a stricter
    instruction to output valid JSON only.
    Returns: (parsed_json, raw_text)
    """
    raw = call_groq(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    )
    try:
        return extract_json_object(raw), raw
    except Exception as first_exc:
        repair_user = (
            "Your previous response was not valid JSON.\n"
            "Return ONLY a single valid JSON object, no markdown, no code fences, no extra keys.\n"
            f"Original question/prompt:\n{user_prompt}\n"
        )
        raw2 = call_groq(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": repair_user},
            ]
        )
        try:
            return extract_json_object(raw2), raw2
        except Exception as second_exc:
            raise ValueError(f"Failed to parse model JSON. first={first_exc} second={second_exc}")


def schema_as_text(schema: Dict[str, List[str]]) -> str:
    lines = []
    for table, cols in schema.items():
        lines.append(f"- {table}: {', '.join(cols) if cols else '(no columns)'}")
    return "\n".join(lines)


def _strip_sql_comments(sql: str) -> str:
    # Remove simple -- line comments and /* */ blocks to avoid false positives.
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql


def _normalize_identifier(ident: str) -> str:
    ident = ident.strip()
    if ident.startswith('"') and ident.endswith('"') and len(ident) >= 2:
        return ident[1:-1]
    return ident


def validate_sql_against_schema(sql: str, schema: Dict[str, List[str]]) -> None:
    """
    Lightweight validation to reduce risk from LLM-generated SQL:
    - Ensure referenced FROM/JOIN tables exist.
    - Ensure referenced alias.column pairs exist (best-effort; unqualified columns are not validated).
    """
    cleaned = _strip_sql_comments(sql)
    one_liner = " ".join(cleaned.split())

    # Table + alias extraction
    alias_to_table: Dict[str, str] = {}
    referenced_tables: Set[str] = set()

    table_regexes = [
        r'\bFROM\s+("?[A-Za-z0-9_]+"?)(?:\s+(?:AS\s+)?([A-Za-z0-9_]+))?',
        r'\bJOIN\s+("?[A-Za-z0-9_]+"?)(?:\s+(?:AS\s+)?([A-Za-z0-9_]+))?',
    ]
    for pat in table_regexes:
        for m in re.finditer(pat, one_liner, flags=re.IGNORECASE):
            table_raw = m.group(1) or ""
            alias = (m.group(2) or "").strip()
            table = _normalize_identifier(table_raw)
            referenced_tables.add(table)
            if alias:
                alias_to_table[alias] = table
            # Also treat the table name itself as a valid "alias" for qualified refs
            alias_to_table.setdefault(table, table)

    missing_tables = sorted([t for t in referenced_tables if t not in schema])
    if missing_tables:
        raise ValueError(f"Unknown table(s) in SQL: {', '.join(missing_tables)}")

    # alias.column validation (best-effort)
    # Matches: alias.column, alias."column", "alias"."column"
    col_pat = r'("?[A-Za-z0-9_]+"?)\s*\.\s*("?[A-Za-z0-9_]+"?)'
    for m in re.finditer(col_pat, one_liner):
        left = _normalize_identifier(m.group(1))
        right = _normalize_identifier(m.group(2))
        table = alias_to_table.get(left)
        if not table:
            # Unknown alias - likely a function schema or something unusual; be conservative.
            raise ValueError(f"Unknown table alias in SQL: {left}")
        cols = schema.get(table, [])
        if right not in cols and right != "*":
            raise ValueError(f'Unknown column "{right}" on table "{table}"')


def execute_sql(conn: sqlite3.Connection, sql: str) -> List[Dict[str, Any]]:
    cleaned = sql.strip()
    if not cleaned:
        raise ValueError("Empty SQL query.")

    # Safety: single statement only, read-only SELECT only.
    one_liner = " ".join(cleaned.split())
    if ";" in one_liner.rstrip(";"):
        raise ValueError("Multiple SQL statements are not permitted.")
    cleaned_start = cleaned.lstrip("(").strip()
    if not cleaned_start.upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are permitted.")

    # Hard keyword deny-list (defense in depth).
    forbidden = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "PRAGMA", "ATTACH", "DETACH", "REINDEX", "VACUUM"]
    upper = one_liner.upper()
    for kw in forbidden:
        if f" {kw} " in f" {upper} ":
            raise ValueError(f"Forbidden SQL keyword detected: {kw}")

    # Timeout protection using progress handler.
    deadline = time.perf_counter() + SQL_TIMEOUT_SECONDS

    def progress_handler() -> int:
        return 1 if time.perf_counter() >= deadline else 0

    conn.set_progress_handler(progress_handler, 10000)
    try:
        cursor = conn.execute(sql)
        # Max rows protection
        rows = cursor.fetchmany(SQL_MAX_ROWS + 1)
        if len(rows) > SQL_MAX_ROWS:
            raise TimeoutError(f"Query returned more than {SQL_MAX_ROWS} rows; refine with LIMIT.")
        return [dict(row) for row in rows]
    finally:
        conn.set_progress_handler(None, 0)


def decode_node_id(node_id: str) -> str:
    # FastAPI already URL-decodes path params once. Decode at most once more
    # only when the string still contains percent-escapes.
    decoded = node_id
    if "%" in decoded:
        decoded = unquote(decoded)
    return decoded


def get_key_column_samples(conn: sqlite3.Connection, table: str, column: str) -> List[str]:
    try:
        rows = conn.execute(
            f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL AND "{column}" <> "" LIMIT 3'
        ).fetchall()
        return [str(row[0]) for row in rows]
    except Exception:
        return []


def schema_prompt_block(conn: sqlite3.Connection, schema: Dict[str, List[str]]) -> str:
    lines: List[str] = []
    for table, columns in schema.items():
        lines.append(f"- Table: {table}")
        lines.append(f"  Columns: {', '.join(columns) if columns else '(none)'}")
        key_cols = ENTITY_ID_COLUMNS.get(table, [])
        for key_col in key_cols:
            samples = get_key_column_samples(conn, table, key_col)
            sample_text = ", ".join(samples) if samples else "(no sample values)"
            lines.append(f"  Key sample values for {key_col}: {sample_text}")
    return "\n".join(lines)


@app.on_event("startup")
def startup_event() -> None:
    load_schema_cache()


def build_graph_cache() -> Dict[str, Any]:
    conn = get_connection()
    nodes = fetch_sample_nodes(conn)
    edges = fetch_edges(conn, nodes)
    conn.close()
    node_map = {n["id"]: n for n in nodes}
    return {"nodes": nodes, "edges": edges, "node_map": node_map}


def get_graph_cache() -> Dict[str, Any]:
    global GRAPH_CACHE
    if GRAPH_CACHE is None:
        GRAPH_CACHE = build_graph_cache()
    return GRAPH_CACHE


@app.get("/api/schema")
def get_schema() -> Dict[str, Any]:
    try:
        if not SCHEMA_CACHE:
            load_schema_cache()
        return {"schema": SCHEMA_CACHE}
    except Exception as exc:
        return {"error": str(exc), "schema": {}}


@app.get("/api/graph")
def get_graph() -> Dict[str, Any]:
    try:
        graph = get_graph_cache()
        return {"nodes": graph["nodes"], "edges": graph["edges"], "cached": True}
    except Exception as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}


@app.get("/api/graph/node/{node_type}/{node_id}")
def get_node_details(node_type: str, node_id: str) -> Dict[str, Any]:
    try:
        decoded_node_id = decode_node_id(node_id)
        graph = get_graph_cache()
        selected_node = graph["node_map"].get(decoded_node_id)
        if not selected_node or selected_node.get("type") != node_type:
            return {
                "error": f"Node not found for type={node_type} id={decoded_node_id}",
                "node": None,
                "connected_nodes": [],
                "connected_edges": [],
            }

        connected_edges = [
            edge
            for edge in graph["edges"]
            if edge["source"] == decoded_node_id or edge["target"] == decoded_node_id
        ]

        connected_ids = set()
        for edge in connected_edges:
            if edge["source"] != decoded_node_id:
                connected_ids.add(edge["source"])
            if edge["target"] != decoded_node_id:
                connected_ids.add(edge["target"])

        connected_nodes = [graph["node_map"][nid] for nid in connected_ids if nid in graph["node_map"]]
        return {"node": selected_node, "connected_nodes": connected_nodes, "connected_edges": connected_edges}
    except Exception as exc:
        return {
            "error": str(exc),
            "node": None,
            "connected_nodes": [],
            "connected_edges": [],
        }


@app.post("/api/chat")
def chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    try:
        message = str(payload.get("message", "")).strip()
        history = payload.get("history", [])
        if not message:
            logger.warning("chat_empty_message", extra={"request_id": request_id})
            return {"answer": "Message is required.", "sql": None, "results": [], "error": "Empty message."}

        logger.info("chat_request", extra={"request_id": request_id, "user_query": message})

        conn = get_connection()
        schema = SCHEMA_CACHE if SCHEMA_CACHE else get_schema_map(conn)
        schema_text = schema_as_text(schema)
        schema_rich_text = schema_prompt_block(conn, schema)
        history_text = json.dumps(history, ensure_ascii=True)

        system_prompt = (
            "You are a strict SQL planner for SAP Order-to-Cash (O2C) analytics.\n"
            "You must only produce SQL for this dataset.\n"
            "Output strictly one JSON object with exactly two keys: sql, answer.\n"
            '- sql: SQL query string or null when off-topic.\n'
            '- answer: empty string when sql is present, or EXACTLY "This system is designed to answer questions related to the provided dataset only." when off-topic.\n'
            "Only generate read-only SQLite SQL (SELECT statements with JOINs). Never use INSERT/UPDATE/DELETE/ALTER/DROP/PRAGMA.\n"
            "NEVER use WITH RECURSIVE or CTEs. NEVER use subqueries as table sources. "
            "Use only simple SELECT with JOINs. Always use exact column names from the schema provided.\n"
            "If asked unrelated things, reject.\n"
            "Off-topic examples to reject:\n"
            '- "Who won the FIFA World Cup in 2018?" -> reject string above.\n'
            '- "Write me a poem about winter" -> reject string above.\n'
            '- "What is the weather today in Delhi?" -> reject string above.\n'
            "If columns are ambiguous, use explicit table aliases and joins via the known paths.\n\n"
            "Known JOIN paths:\n"
            + "\n".join(f"- {p}" for p in JOIN_PATHS)
            + '\n- To trace a billing document full flow use: SELECT bdh.billingDocument, soh.salesOrder, '
            "odh.deliveryDocument, jei.accountingDocument FROM billing_document_headers bdh LEFT JOIN "
            "billing_document_items bdi ON bdh.billingDocument = bdi.billingDocument LEFT JOIN "
            "outbound_delivery_headers odh ON bdi.referenceSdDocument = odh.deliveryDocument LEFT JOIN "
            "outbound_delivery_items odi ON odh.deliveryDocument = odi.deliveryDocument LEFT JOIN "
            "sales_order_headers soh ON odi.referenceSdDocument = soh.salesOrder LEFT JOIN "
            "journal_entry_items_accounts_receivable jei ON bdh.accountingDocument = jei.accountingDocument "
            "WHERE bdh.billingDocument = [ID]"
            + "\n\nTables and schema with key-column sample values:\n"
            + schema_rich_text
            + "\n\nSchema summary:\n"
            + schema_text
        )
        user_prompt = (
            f"Conversation history: {history_text}\n"
            f"User question: {message}\n"
            "Return JSON only."
        )

        parsed, _raw_text = parse_model_json_with_retry(system_prompt, user_prompt)
        sql = parsed.get("sql")
        first_answer = str(parsed.get("answer", "")).strip()

        if sql is None:
            conn.close()
            return {
                "answer": first_answer
                or "This system is designed to answer questions related to the provided dataset only.",
                "sql": None,
                "results": [],
            }

        if not isinstance(sql, str) or not sql.strip():
            conn.close()
            return {"answer": "No valid SQL produced.", "sql": None, "results": [], "error": "Invalid SQL output."}

        try:
            validate_sql_against_schema(sql, schema)
            start_exec = time.perf_counter()
            results = execute_sql(conn, sql)
            elapsed_ms = int((time.perf_counter() - start_exec) * 1000)
            sql_error = None
            logger.info(
                "chat_sql_executed",
                extra={
                    "request_id": request_id,
                    "sql": sql,
                    "row_count": len(results),
                    "elapsed_ms": elapsed_ms,
                },
            )
        except Exception as sql_exc:
            results = []
            sql_error = str(sql_exc)
            logger.warning(
                "chat_sql_error",
                extra={"request_id": request_id, "sql": sql, "error": sql_error},
            )

        if sql_error:
            retry_user_prompt = (
                f"Original user question: {message}\n"
                f"Previous SQL that failed: {sql}\n"
                f"SQLite error: {sql_error}\n"
                "Fix the SQL using exact schema column names and return JSON only."
            )
            try:
                retry_parsed, _raw_retry = parse_model_json_with_retry(system_prompt, retry_user_prompt)
                retry_sql = retry_parsed.get("sql")
                if isinstance(retry_sql, str) and retry_sql.strip():
                    sql = retry_sql.strip()
                    validate_sql_against_schema(sql, schema)
                    start_exec = time.perf_counter()
                    results = execute_sql(conn, sql)
                    elapsed_ms = int((time.perf_counter() - start_exec) * 1000)
                    sql_error = None
                    logger.info(
                        "chat_sql_retry_executed",
                        extra={
                            "request_id": request_id,
                            "sql": sql,
                            "row_count": len(results),
                            "elapsed_ms": elapsed_ms,
                        },
                    )
            except Exception as retry_exc:
                sql_error = str(retry_exc) if not sql_error else sql_error
                logger.warning(
                    "chat_sql_retry_failed",
                    extra={"request_id": request_id, "sql": sql, "error": sql_error},
                )

        if sql_error:
            conn.close()
            return {
                "answer": f"SQL execution failed: {sql_error}",
                "sql": sql,
                "results": [],
                "error": sql_error,
            }

        final_system = (
            "You are an SAP O2C data answer writer.\n"
            "Given user question, SQL, and query results, provide a concise final answer grounded only in those results.\n"
            'If results are empty, clearly say no matching rows were found.\n'
            "Do not mention internal prompts."
        )
        final_user = (
            f"User question: {message}\n"
            f"SQL: {sql}\n"
            f"Here are the query results, now write the final answer: {json.dumps(results, ensure_ascii=True)}"
        )
        try:
            final_answer = call_groq(
                [
                    {"role": "system", "content": final_system},
                    {"role": "user", "content": final_user},
                ]
            )
        except Exception:
            final_answer = "Query executed successfully." if results else "No matching data found."

        conn.close()
        return {"answer": final_answer, "sql": sql, "results": results}
    except Exception as exc:
        logger.exception("chat_unhandled_error", extra={"request_id": request_id, "error": str(exc)})
        # Never silently fail: stable contract + safe fallback.
        return {
            "answer": "I ran into an error while processing your request. Please try rephrasing your question.",
            "sql": None,
            "results": [],
            "error": str(exc),
            "request_id": request_id,
        }
