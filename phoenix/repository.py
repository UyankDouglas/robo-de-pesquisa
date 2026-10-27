"""Persistência específica do Phoenix Bot (sobre o banco de execuções do motor).

- runs/items do motor: um item por CNPJ distinto (resultado = solicitações e bilhetes em JSON);
- phx_jobs: planilha de origem (hash), abas/mapeamentos, regras usadas e carimbo dos arquivos;
- phx_rows: cada linha selecionada da planilha (valores para a exportação e chaves normalizadas).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from core.config import RunSettings
from core.models import ItemStatus
from database.repository import Repository, now_iso
from excel.importer import ImportRecord, ImportResult
from phoenix.config import PhoenixSettings, SheetMapping
from phoenix.matcher import CnpjQuery
from phoenix.normalize import CnpjInfo
from phoenix.workbook import SheetData, SourceRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS phx_jobs (
    run_id          INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    source_file     TEXT NOT NULL,
    source_sha256   TEXT NOT NULL,
    sheets_json     TEXT NOT NULL,
    settings_json   TEXT NOT NULL,
    export_stamp    TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phx_rows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sheet           TEXT NOT NULL,
    row_number      INTEGER NOT NULL,
    cnpj            TEXT NOT NULL,
    cnpj_raw        TEXT NOT NULL,
    cnpj_status     TEXT NOT NULL,
    cnpj_message    TEXT,
    acesso_raw      TEXT,
    acesso          TEXT,
    temp_raw        TEXT,
    temp            TEXT,
    fila_raw        TEXT,
    values_json     TEXT NOT NULL,
    warnings_json   TEXT NOT NULL,
    UNIQUE (run_id, sheet, row_number)
);
CREATE INDEX IF NOT EXISTS ix_phx_rows_cnpj ON phx_rows(run_id, cnpj);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _encode(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, dt.date):
        return {"__d__": value.isoformat()}
    if isinstance(value, dt.time):
        return {"__t__": value.isoformat()}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if "__dt__" in value:
            return dt.datetime.fromisoformat(value["__dt__"])
        if "__d__" in value:
            return dt.date.fromisoformat(value["__d__"])
        if "__t__" in value:
            return dt.time.fromisoformat(value["__t__"])
    return value


@dataclass
class JobInfo:
    run_id: int
    source_file: str
    source_sha256: str
    sheets: list[dict[str, Any]]           # [{"mapping": {...}, "headers": [...], "issues": [...]}]
    settings: PhoenixSettings
    export_stamp: str | None
    created_at: str

    def mappings(self) -> list[SheetMapping]:
        return [SheetMapping(sheet=s["mapping"]["sheet"], **{k: v for k, v in s["mapping"].items() if k != "sheet"})
                for s in self.sheets]


class PhoenixRepository:
    def __init__(self, repo: Repository):
        self.repo = repo
        self.conn = repo.conn
        ensure_schema(self.conn)

    def create_job(self, name: str, source_file: str, sha256: str, sheets: list[SheetData],
                   settings: PhoenixSettings, run_settings: RunSettings) -> int:
        order: dict[str, list[SourceRow]] = {}
        for sheet in sheets:
            for row in sheet.rows:
                if row.cnpj.status == "valido":
                    order.setdefault(row.cnpj.value, []).append(row)
        records = [
            ImportRecord(position=i, row_number=i, identifier=cnpj,
                         original={"linhas": str(len(rows)), "abas": ", ".join(sorted({r.sheet for r in rows}))})
            for i, (cnpj, rows) in enumerate(order.items(), start=1)
        ]
        imported = ImportResult(source_file, "; ".join(s.name for s in sheets), "CNPJ",
                                ["linhas", "abas"], 1, records, len(records))
        run_id = self.repo.create_run(name, imported, run_settings)
        sheets_json = [{"mapping": {"sheet": s.name, **s.mapping.to_dict()}, "headers": s.headers, "issues": s.issues}
                       for s in sheets]
        with self.repo._tx() as conn:
            conn.execute(
                "INSERT INTO phx_jobs (run_id, source_file, source_sha256, sheets_json, settings_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, source_file, sha256, json.dumps(sheets_json, ensure_ascii=False),
                 json.dumps(settings.to_dict(), ensure_ascii=False), now_iso()),
            )
            conn.executemany(
                """INSERT INTO phx_rows (run_id, sheet, row_number, cnpj, cnpj_raw, cnpj_status, cnpj_message, acesso_raw,
                       acesso, temp_raw, temp, fila_raw, values_json, warnings_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(run_id, r.sheet, r.row, r.cnpj.value, r.cnpj.raw, r.cnpj.status, r.cnpj.message, r.acesso_raw,
                  r.acesso, r.temp_raw, r.temp, r.fila_raw, json.dumps([_encode(v) for v in r.values], ensure_ascii=False),
                  json.dumps(r.warnings, ensure_ascii=False)) for s in sheets for r in s.rows],
            )
        return run_id

    def job(self, run_id: int) -> JobInfo:
        row = self.conn.execute("SELECT * FROM phx_jobs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Execução {run_id} não é do Phoenix Bot")
        return JobInfo(row["run_id"], row["source_file"], row["source_sha256"], json.loads(row["sheets_json"]),
                       PhoenixSettings.from_dict(json.loads(row["settings_json"])), row["export_stamp"], row["created_at"])

    def jobs(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT run_id FROM phx_jobs ORDER BY run_id DESC")]

    def update_settings(self, run_id: int, settings: PhoenixSettings) -> None:
        self.conn.execute("UPDATE phx_jobs SET settings_json = ? WHERE run_id = ?",
                          (json.dumps(settings.to_dict(), ensure_ascii=False), run_id))

    def export_stamp(self, run_id: int) -> str:
        stamp = self.job(run_id).export_stamp
        if not stamp:
            stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.conn.execute("UPDATE phx_jobs SET export_stamp = ? WHERE run_id = ?", (stamp, run_id))
        return stamp

    def find_resumable(self, sha256: str, mappings: list[SheetMapping]) -> int | None:
        """Execução incompleta da mesma planilha (mesmo conteúdo), mesmas abas e mesmo mapeamento."""
        wanted = [{"sheet": m.sheet, **m.to_dict()} for m in mappings]
        keys = ("sheet", "header_row", "cnpj", "acesso", "fila_atual", "numero_temporario")
        for run_id in self.jobs():
            job = self.job(run_id)
            same = [{k: s["mapping"].get(k) for k in keys} for s in job.sheets] == [{k: m[k] for k in keys} for m in wanted]
            if job.source_sha256 == sha256 and same:
                stats = self.repo.stats(run_id)
                if stats.remaining:
                    return run_id
        return None

    def rows(self, run_id: int) -> list[SourceRow]:
        out = []
        for r in self.conn.execute("SELECT * FROM phx_rows WHERE run_id = ? ORDER BY id", (run_id,)):
            out.append(SourceRow(
                sheet=r["sheet"], row=r["row_number"],
                cnpj=CnpjInfo(r["cnpj_raw"], r["cnpj"], r["cnpj_status"], r["cnpj_message"] or ""),
                acesso_raw=r["acesso_raw"] or "", acesso=r["acesso"] or "",
                temp_raw=r["temp_raw"] or "", temp=r["temp"] or "", fila_raw=r["fila_raw"] or "",
                values=[_decode(v) for v in json.loads(r["values_json"])],
                warnings=json.loads(r["warnings_json"]),
            ))
        return out

    def queries(self, run_id: int) -> dict[str, CnpjQuery]:
        return {item.identifier: CnpjQuery(item.status, item.result, item.message, item.queried_at)
                for item in self.repo.items(run_id)}

    def attempts_by_cnpj(self, run_id: int) -> dict[str, int]:
        return {item.identifier: item.attempts for item in self.repo.items(run_id)}

    def item_statuses(self, run_id: int) -> dict[str, ItemStatus]:
        return {item.identifier: item.status for item in self.repo.items(run_id)}
