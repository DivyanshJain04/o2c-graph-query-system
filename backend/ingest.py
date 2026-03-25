import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


DEFAULT_DATASET_CANDIDATES = [
    Path("data/sap-o2c-data"),
    Path("data/sap-order-to-cash-dataset/sap-o2c-data"),
]
DB_PATH = Path("database.db")

# Indexes to create after ingestion for join performance
FK_INDEXES = [
    ("sales_order_headers", "salesOrder"),
    ("sales_order_headers", "soldToParty"),
    ("sales_order_items", "salesOrder"),
    ("sales_order_items", "material"),
    ("outbound_delivery_headers", "deliveryDocument"),
    ("outbound_delivery_items", "deliveryDocument"),
    ("outbound_delivery_items", "referenceSdDocument"),
    ("billing_document_headers", "billingDocument"),
    ("billing_document_headers", "accountingDocument"),
    ("billing_document_headers", "soldToParty"),
    ("billing_document_items", "billingDocument"),
    ("billing_document_items", "referenceSdDocument"),
    ("journal_entry_items_accounts_receivable", "accountingDocument"),
    ("journal_entry_items_accounts_receivable", "referenceDocument"),
    ("payments_accounts_receivable", "accountingDocument"),
    ("payments_accounts_receivable", "customer"),
    ("business_partners", "businessPartner"),
    ("products", "product"),
]


def to_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    # Preserve complex structures as JSON strings after flattening.
    return json.dumps(value, ensure_ascii=True)


def flatten_dict(data: Dict[str, Any], parent_key: str = "", sep: str = "_") -> Dict[str, Any]:
    flattened: Dict[str, Any] = {}
    for key, value in data.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else key
        if isinstance(value, dict):
            nested = flatten_dict(value, new_key, sep=sep)
            flattened.update(nested)
        elif isinstance(value, list):
            flattened[new_key] = json.dumps(value, ensure_ascii=True)
        else:
            flattened[new_key] = to_string(value)
    return flattened


def detect_dataset_root() -> Path:
    for candidate in DEFAULT_DATASET_CANDIDATES:
        if candidate.exists() and candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Could not find dataset folder. Checked: "
        + ", ".join(str(p) for p in DEFAULT_DATASET_CANDIDATES)
    )


def jsonl_files_for_entity(entity_folder: Path) -> List[Path]:
    return sorted([p for p in entity_folder.glob("*.jsonl") if p.is_file()])


def load_rows(file_paths: Iterable[Path]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for file_path in file_paths:
        print(f"  - Reading {file_path.name}")
        with file_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(f"    ! Skipping malformed JSON at line {line_number}: {exc}")
                    continue
                if isinstance(parsed, dict):
                    rows.append(flatten_dict(parsed))
    return rows


def collect_columns(rows: List[Dict[str, Any]]) -> List[str]:
    seen: Set[str] = set()
    for row in rows:
        seen.update(row.keys())
    return sorted(seen)


def _looks_like_int_id(value: str) -> bool:
    v = value.strip()
    if not v.isdigit():
        return False
    # Avoid coercing identifiers with leading zeros (common in ERP) into int.
    if len(v) > 1 and v.startswith("0"):
        return False
    # Keep very large numbers as text (risk of overflow / not truly numeric).
    return len(v) <= 18


def _safe_parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not _looks_like_int_id(text):
        return None
    try:
        return int(text)
    except Exception:
        return None


def _safe_parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Normalize common formats
    text = text.replace(",", "")
    try:
        return float(text)
    except Exception:
        return None


def infer_column_types(rows: List[Dict[str, Any]], columns: List[str]) -> Dict[str, str]:
    """
    Heuristic typing to fix numeric correctness:
    - Columns containing amount/price/quantity => REAL
    - Columns that look like integer IDs (name ends with id/_id and values are digits without leading zeros) => INTEGER
    - Else => TEXT
    """
    types: Dict[str, str] = {}
    for col in columns:
        col_l = col.lower()
        if any(token in col_l for token in ["amount", "price", "quantity"]):
            types[col] = "REAL"
            continue

        is_id_name = col_l.endswith("id") or col_l.endswith("_id")
        if is_id_name:
            # Look at a handful of values to confirm it's safe to treat as integer.
            seen_values = 0
            good_ints = 0
            for row in rows:
                if col not in row:
                    continue
                val = row.get(col)
                if val in (None, ""):
                    continue
                seen_values += 1
                if _safe_parse_int(val) is not None:
                    good_ints += 1
                if seen_values >= 25:
                    break
            if seen_values > 0 and good_ints / max(seen_values, 1) >= 0.9:
                types[col] = "INTEGER"
                continue

        types[col] = "TEXT"

    # Minimum correctness guarantees requested.
    for required_real in ["totalNetAmount", "netAmount", "quantity"]:
        if required_real in columns:
            types[required_real] = "REAL"
    return types


def create_table(
    cursor: sqlite3.Cursor, table_name: str, columns: List[str], column_types: Dict[str, str]
) -> None:
    escaped_columns = ", ".join(
        f'"{col}" {column_types.get(col, "TEXT")}' for col in columns
    )
    cursor.execute(f'DROP TABLE IF EXISTS "{table_name}"')
    if escaped_columns:
        cursor.execute(f'CREATE TABLE "{table_name}" ({escaped_columns})')
    else:
        cursor.execute(f'CREATE TABLE "{table_name}" ("_empty" TEXT)')


def insert_rows(
    conn: sqlite3.Connection,
    cursor: sqlite3.Cursor,
    table_name: str,
    columns: List[str],
    rows: List[Dict[str, Any]],
    column_types: Dict[str, str],
) -> None:
    if not rows:
        return
    placeholders = ", ".join("?" for _ in columns)
    cols_sql = ", ".join(f'"{c}"' for c in columns)
    sql = f'INSERT INTO "{table_name}" ({cols_sql}) VALUES ({placeholders})'
    payload = []
    for row in rows:
        record = []
        for col in columns:
            v = row.get(col, None)
            t = column_types.get(col, "TEXT")
            if t == "REAL":
                record.append(_safe_parse_float(v))
            elif t == "INTEGER":
                parsed = _safe_parse_int(v)
                record.append(parsed if parsed is not None else None)
            else:
                record.append("" if v is None else str(v))
        payload.append(record)
    cursor.executemany(sql, payload)
    conn.commit()


def create_indexes(conn: sqlite3.Connection) -> None:
    """Create indexes on FK columns used in JOINs for query performance."""
    cursor = conn.cursor()
    existing_tables = {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    existing_indexes = {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }

    print("\nCreating indexes on FK columns...")
    for table, column in FK_INDEXES:
        if table not in existing_tables:
            continue
        # Check column exists in table
        cols = [
            row[1]
            for row in cursor.execute(f'PRAGMA table_info("{table}")').fetchall()
        ]
        if column not in cols:
            continue
        index_name = f"idx_{table}_{column}"
        if index_name in existing_indexes:
            continue
        cursor.execute(
            f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{table}" ("{column}")'
        )
        print(f"  + {index_name}")

    conn.commit()
    print("Indexes created.")


def main() -> None:
    dataset_root = detect_dataset_root()
    print(f"Using dataset folder: {dataset_root}")
    print(f"Writing SQLite database to: {DB_PATH}")

    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    entity_folders = sorted([p for p in dataset_root.iterdir() if p.is_dir()])
    if not entity_folders:
        raise RuntimeError(f"No entity folders found in {dataset_root}")

    for entity_folder in entity_folders:
        table_name = entity_folder.name
        print(f"\nProcessing entity/table: {table_name}")
        files = jsonl_files_for_entity(entity_folder)
        if not files:
            print("  - No JSONL files found. Creating empty table.")
            create_table(cursor, table_name, [])
            conn.commit()
            continue

        rows = load_rows(files)
        columns = collect_columns(rows)
        column_types = infer_column_types(rows, columns)
        print(f"  - Rows loaded: {len(rows)}")
        print(f"  - Columns detected: {len(columns)}")
        create_table(cursor, table_name, columns, column_types)
        insert_rows(conn, cursor, table_name, columns, rows, column_types)
        print("  - Table written successfully.")

    create_indexes(conn)
    conn.close()
    print("\nIngestion complete.")


if __name__ == "__main__":
    main()
