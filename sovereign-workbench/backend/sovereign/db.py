"""SQLite persistence layer.

Design notes
------------
* One database file on ext4 in WAL mode. The whole control plane is a single process,
  so a module-level connection with `check_same_thread=False` plus a re-entrant lock
  gives us serialised writes without a connection pool.
* Schema is created idempotently at import time by `init_db()`; there is no migration
  tool because the deployment model is "ship the appliance", not "upgrade in place".
* Every table that records a decision keeps the *reason* alongside the outcome. An
  operator must be able to ask "why" of any row without reading the code.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

from .config import DB_PATH

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=10000;

-- ---------------------------------------------------------------- control plane
CREATE TABLE IF NOT EXISTS tasks (
    id                TEXT PRIMARY KEY,
    parent_id         TEXT,
    title             TEXT NOT NULL,
    prompt            TEXT NOT NULL,
    owner             TEXT NOT NULL DEFAULT 'operator',
    department        TEXT NOT NULL DEFAULT 'default',
    workflow          TEXT NOT NULL DEFAULT 'general',
    task_type         TEXT,
    priority          TEXT NOT NULL DEFAULT 'MEDIUM',
    effective_priority REAL,
    state             TEXT NOT NULL DEFAULT 'QUEUED',
    state_reason      TEXT,
    required_caps     TEXT,
    selected_model    TEXT,
    selected_backend  TEXT,
    routing_reason    TEXT,
    est_context_tokens INTEGER DEFAULT 0,
    est_vram_mb       INTEGER DEFAULT 0,
    policy_mode       TEXT,
    attachments       TEXT,
    result            TEXT,
    error             TEXT,
    created_at        REAL NOT NULL,
    admitted_at       REAL,
    started_at        REAL,
    finished_at       REAL,
    heartbeat_at      REAL,
    queue_wait_s      REAL,
    runtime_s         REAL,
    steps_used        INTEGER DEFAULT 0,
    retries           INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS ix_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS task_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    label     TEXT,
    detail    TEXT,
    payload   TEXT,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_task ON task_events(task_id, seq);

CREATE TABLE IF NOT EXISTS checkpoints (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    step       INTEGER NOT NULL,
    reason     TEXT,
    state_blob TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ckpt_task ON checkpoints(task_id, step);

-- ---------------------------------------------------------------- model plane
CREATE TABLE IF NOT EXISTS model_registry (
    name            TEXT PRIMARY KEY,
    backend         TEXT NOT NULL,
    backend_ref     TEXT NOT NULL,
    family          TEXT,
    role            TEXT,
    prompt_adapter  TEXT DEFAULT 'chat',
    modality        TEXT DEFAULT 'text',
    caps            TEXT NOT NULL,
    ctx_max         INTEGER DEFAULT 8192,
    weights_mb      INTEGER DEFAULT 0,
    est_vram_mb     INTEGER DEFAULT 0,
    est_ram_mb      INTEGER DEFAULT 0,
    kv_mb_per_1k    REAL DEFAULT 0,
    enabled         INTEGER DEFAULT 1,
    notes           TEXT
);

-- Empirically measured, not guessed. Written by scripts/probe_models.py.
CREATE TABLE IF NOT EXISTS model_profiles (
    model            TEXT PRIMARY KEY,
    cold_load_s      REAL,
    warm_ttft_s      REAL,
    decode_tps       REAL,
    prefill_tps      REAL,
    vram_resident_mb REAL,
    ram_resident_mb  REAL,
    gpu_layers       INTEGER,
    kv_budget_tokens INTEGER,
    measured_at      REAL,
    samples          INTEGER DEFAULT 0,
    raw              TEXT
);

CREATE TABLE IF NOT EXISTS model_health (
    model      TEXT PRIMARY KEY,
    status     TEXT,
    detail     TEXT,
    failures   INTEGER DEFAULT 0,
    open_until REAL DEFAULT 0,
    checked_at REAL
);

CREATE TABLE IF NOT EXISTS residency_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    action    TEXT NOT NULL,
    model     TEXT,
    evicted   TEXT,
    reason    TEXT,
    cost_s    REAL,
    vram_mb   REAL,
    ts        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS hardware_profile (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    payload  TEXT NOT NULL,
    ts       REAL NOT NULL
);

-- ---------------------------------------------------------------- knowledge plane
CREATE TABLE IF NOT EXISTS documents (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    doc_class     TEXT,
    path          TEXT NOT NULL,
    sha256        TEXT,
    pages         INTEGER DEFAULT 0,
    bytes         INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'PENDING',
    status_detail TEXT,
    meta          TEXT,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id           TEXT PRIMARY KEY,
    doc_id       TEXT NOT NULL,
    page_no      INTEGER NOT NULL,
    width        REAL, height REAL,
    text         TEXT,
    extractor    TEXT,
    ocr_conf     REAL,
    image_path   TEXT,
    words        TEXT,
    UNIQUE(doc_id, page_no)
);

CREATE TABLE IF NOT EXISTS chunks (
    id        TEXT PRIMARY KEY,
    doc_id    TEXT NOT NULL,
    page_no   INTEGER,
    ordinal   INTEGER,
    text      TEXT NOT NULL,
    region    TEXT,
    tokens    INTEGER,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS ix_chunks_doc ON chunks(doc_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, chunk_id UNINDEXED, tokenize='porter unicode61'
);

-- ---------------------------------------------------------------- evidence plane
CREATE TABLE IF NOT EXISTS evidence (
    id         TEXT PRIMARY KEY,
    task_id    TEXT,
    doc_id     TEXT,
    page_no    INTEGER,
    region     TEXT,
    snippet    TEXT,
    score      REAL,
    kind       TEXT DEFAULT 'text',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL,
    entity       TEXT,
    attribute    TEXT,
    value        TEXT,
    unit         TEXT,
    statement    TEXT NOT NULL,
    ev_class     TEXT NOT NULL,          -- A source | B derived | C interpretation | D unsupported
    evidence_ids TEXT,
    calc_id      TEXT,
    confidence   REAL,
    rationale    TEXT,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS calculations (
    id          TEXT PRIMARY KEY,
    task_id     TEXT,
    label       TEXT,
    expression  TEXT NOT NULL,
    inputs      TEXT NOT NULL,
    input_prov  TEXT,
    result      TEXT,
    unit        TEXT,
    steps       TEXT,
    ok          INTEGER DEFAULT 1,
    error       TEXT,
    created_at  REAL NOT NULL
);

-- ---------------------------------------------------------------- tool / policy plane
CREATE TABLE IF NOT EXISTS tool_calls (
    id          TEXT PRIMARY KEY,
    task_id     TEXT,
    step        INTEGER,
    tool        TEXT NOT NULL,
    args        TEXT,
    decision    TEXT,
    policy_mode TEXT,
    reason      TEXT,
    result      TEXT,
    ok          INTEGER,
    duration_s  REAL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    task_id     TEXT,
    tool        TEXT,
    args        TEXT,
    summary     TEXT,
    state       TEXT DEFAULT 'PENDING',
    decided_by  TEXT,
    decided_at  REAL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS network_events (
    id          TEXT PRIMARY KEY,
    task_id     TEXT,
    process     TEXT,
    pid         INTEGER,
    destination TEXT,
    port        INTEGER,
    layer       TEXT,                    -- tool-policy | app-guard | sandbox-netns | nftables
    policy      TEXT,
    result      TEXT,                    -- DENIED | BLOCKED | ALLOWED
    detail      TEXT,
    ts          REAL NOT NULL
);

-- Hash-chained, append-only. See audit.py.
CREATE TABLE IF NOT EXISTS audit_log (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    actor     TEXT,
    task_id   TEXT,
    category  TEXT NOT NULL,
    action    TEXT NOT NULL,
    outcome   TEXT,
    detail    TEXT,
    prev_hash TEXT NOT NULL,
    hash      TEXT NOT NULL
);

-- Anchors the audit chain. Hash-linking alone detects an edited or removed
-- *interior* row, because the next row's prev_hash stops matching. It does not
-- detect truncation: lopping off the most recent entries leaves a shorter but
-- internally consistent chain. Recording the head hash and the entry count
-- separately makes that visible.
CREATE TABLE IF NOT EXISTS audit_anchor (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    head    TEXT NOT NULL,
    entries INTEGER NOT NULL,
    max_seq INTEGER NOT NULL,
    ts      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id         TEXT PRIMARY KEY,
    task_id    TEXT,
    name       TEXT NOT NULL,
    kind       TEXT,
    path       TEXT NOT NULL,
    bytes      INTEGER,
    sha256     TEXT,
    meta       TEXT,
    created_at REAL NOT NULL
);

-- ---------------------------------------------------------------- drawings plane
CREATE TABLE IF NOT EXISTS drawings (
    id          TEXT PRIMARY KEY,
    doc_id      TEXT,
    title       TEXT,
    page_no     INTEGER DEFAULT 1,
    source_kind TEXT,                    -- vector | raster
    width       REAL, height REAL,
    image_path  TEXT,
    status      TEXT,
    summary     TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS drawing_symbols (
    id         TEXT PRIMARY KEY,
    drawing_id TEXT NOT NULL,
    label      TEXT,
    sym_class  TEXT,
    bbox       TEXT,
    confidence REAL,
    method     TEXT,
    tag        TEXT
);

CREATE TABLE IF NOT EXISTS drawing_tags (
    id         TEXT PRIMARY KEY,
    drawing_id TEXT NOT NULL,
    text       TEXT,
    tag_type   TEXT,
    bbox       TEXT,
    confidence REAL,
    symbol_id  TEXT,
    assoc_dist REAL
);

CREATE TABLE IF NOT EXISTS drawing_edges (
    id         TEXT PRIMARY KEY,
    drawing_id TEXT NOT NULL,
    src        TEXT,
    dst        TEXT,
    line_type  TEXT,
    polyline   TEXT,
    confidence REAL,
    status     TEXT,                     -- CONFIRMED | PROBABLE | UNRESOLVED
    rationale  TEXT
);

-- ---------------------------------------------------------------- benchmarks
CREATE TABLE IF NOT EXISTS benchmarks (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    variant  TEXT,
    payload  TEXT,
    ts       REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
            c.row_factory = sqlite3.Row
            c.executescript(SCHEMA)
            c.commit()
            _conn = c
        return _conn


def init_db() -> None:
    connect()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Serialised write transaction."""
    c = connect()
    with _lock:
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def execute(sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    with tx() as c:
        return c.execute(sql, params)


def executemany(sql: str, rows: Iterable[Sequence[Any]]) -> None:
    with tx() as c:
        c.executemany(sql, list(rows))


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    c = connect()
    with _lock:
        return c.execute(sql, params).fetchall()


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def insert(table: str, row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))


def upsert(table: str, row: dict[str, Any], key: str) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != key)
    execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks}) "
        f"ON CONFLICT({key}) DO UPDATE SET {updates}",
        list(row.values()),
    )


def update(table: str, key_col: str, key_val: Any, row: dict[str, Any]) -> None:
    if not row:
        return
    sets = ", ".join(f"{k}=?" for k in row)
    execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?", [*row.values(), key_val])


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def jload(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdump(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()
