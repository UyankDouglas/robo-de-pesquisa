"""Conexão SQLite e criação/migração do esquema.

Cada thread deve abrir a própria conexão (``Database.connect``). O modo WAL permite que a
interface leia estatísticas enquanto o worker grava.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    adapter_id      TEXT NOT NULL,
    source_file     TEXT NOT NULL,
    sheet           TEXT NOT NULL,
    search_column   TEXT NOT NULL,
    columns_json    TEXT NOT NULL,          -- colunas originais, na ordem da planilha
    settings_json   TEXT NOT NULL,          -- RunSettings usados (a retomada reaproveita)
    status          TEXT NOT NULL,
    output_file     TEXT,
    message         TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,       -- ordem na planilha (nº do registro)
    row_number      INTEGER NOT NULL,       -- linha no Excel
    identifier      TEXT NOT NULL,
    original_json   TEXT NOT NULL,          -- linha original completa (preservada)
    status          TEXT NOT NULL,
    result_json     TEXT,
    message         TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    duplicate_of    INTEGER REFERENCES items(id),
    queried_at      TEXT,                   -- data/hora da última consulta
    duration_ms     INTEGER,
    updated_at      TEXT NOT NULL,
    UNIQUE (run_id, position)
);
CREATE INDEX IF NOT EXISTS ix_items_run_status ON items(run_id, status, position);
CREATE INDEX IF NOT EXISTS ix_items_run_identifier ON items(run_id, identifier);

-- Auditoria: uma linha por tentativa de consulta.
CREATE TABLE IF NOT EXISTS attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt_no      INTEGER NOT NULL,
    adapter_id      TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    outcome         TEXT NOT NULL,          -- found / not_found / transient_error / error / operator / session
    message         TEXT
);
CREATE INDEX IF NOT EXISTS ix_attempts_item ON attempts(item_id);

-- Eventos relevantes da execução (início, pausa, retomada, exportação...).
CREATE TABLE IF NOT EXISTS run_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    kind            TEXT NOT NULL,
    message         TEXT
);
CREATE INDEX IF NOT EXISTS ix_run_events_run ON run_events(run_id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        if not self._initialized:
            self._migrate(conn)
            self._initialized = True
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
