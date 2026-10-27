"""Acesso a dados: execuções, registros, tentativas (auditoria) e eventos."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from core.config import RunSettings
from core.models import RESUMABLE_RUN_STATUSES, ItemStatus, RunStatus

if TYPE_CHECKING:
    from excel.importer import ImportResult


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class RunInfo:
    id: int
    name: str
    adapter_id: str
    source_file: str
    sheet: str
    search_column: str
    columns: list[str]
    settings: RunSettings
    status: RunStatus
    output_file: str | None
    message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RunInfo":
        return cls(
            id=row["id"],
            name=row["name"],
            adapter_id=row["adapter_id"],
            source_file=row["source_file"],
            sheet=row["sheet"],
            search_column=row["search_column"],
            columns=json.loads(row["columns_json"]),
            settings=RunSettings.from_dict(json.loads(row["settings_json"])),
            status=RunStatus(row["status"]),
            output_file=row["output_file"],
            message=row["message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            updated_at=row["updated_at"],
        )


@dataclass
class ItemRecord:
    id: int
    run_id: int
    position: int
    row_number: int
    identifier: str
    original: dict[str, str]
    status: ItemStatus
    result: dict[str, Any] | None
    message: str | None
    attempts: int
    duplicate_of: int | None
    queried_at: str | None
    duration_ms: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ItemRecord":
        return cls(
            id=row["id"],
            run_id=row["run_id"],
            position=row["position"],
            row_number=row["row_number"],
            identifier=row["identifier"],
            original=json.loads(row["original_json"]),
            status=ItemStatus(row["status"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            message=row["message"],
            attempts=row["attempts"],
            duplicate_of=row["duplicate_of"],
            queried_at=row["queried_at"],
            duration_ms=row["duration_ms"],
        )


@dataclass
class RunStats:
    pending: int = 0
    processing: int = 0
    found: int = 0
    not_found: int = 0
    error: int = 0
    duplicate: int = 0
    invalid: int = 0

    @property
    def total(self) -> int:
        """Registros que entram na consulta (sem duplicados/inválidos)."""
        return self.pending + self.processing + self.found + self.not_found + self.error

    @property
    def done(self) -> int:
        return self.found + self.not_found + self.error

    @property
    def remaining(self) -> int:
        return self.pending + self.processing

    @property
    def percent(self) -> float:
        return 100.0 * self.done / self.total if self.total else 100.0


@dataclass
class AttemptRecord:
    attempt_no: int
    started_at: str
    finished_at: str
    outcome: str
    message: str | None


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ runs
    def create_run(self, name: str, imported: "ImportResult", settings: RunSettings) -> int:
        ts = now_iso()
        with self._tx() as conn:
            cur = conn.execute(
                """INSERT INTO runs (name, adapter_id, source_file, sheet, search_column, columns_json,
                                     settings_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    settings.adapter_id,
                    imported.file,
                    imported.sheet,
                    imported.column,
                    json.dumps(imported.columns, ensure_ascii=False),
                    json.dumps(settings.to_dict(), ensure_ascii=False),
                    RunStatus.CREATED.value,
                    ts,
                    ts,
                ),
            )
            run_id = int(cur.lastrowid)
            id_by_row: dict[int, int] = {}
            for rec in imported.records:
                duplicate_of = id_by_row.get(rec.duplicate_of_row) if rec.duplicate_of_row else None
                cur = conn.execute(
                    """INSERT INTO items (run_id, position, row_number, identifier, original_json, status,
                                          message, duplicate_of, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        rec.position,
                        rec.row_number,
                        rec.identifier,
                        json.dumps(rec.original, ensure_ascii=False),
                        rec.status.value,
                        rec.message or None,
                        duplicate_of,
                        ts,
                    ),
                )
                id_by_row[rec.row_number] = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO run_events (run_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
                (run_id, ts, "created", imported.summary()),
            )
        return run_id

    def get_run(self, run_id: int) -> RunInfo:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Execução {run_id} não encontrada")
        return RunInfo.from_row(row)

    def list_runs(self, limit: int = 200) -> list[RunInfo]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [RunInfo.from_row(r) for r in rows]

    def runs_with_status(self, *statuses: RunStatus) -> list[RunInfo]:
        marks = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT * FROM runs WHERE status IN ({marks}) ORDER BY id", [s.value for s in statuses]
        ).fetchall()
        return [RunInfo.from_row(r) for r in rows]

    def find_resumable_run(self, source_file: str, sheet: str, column: str, adapter_id: str) -> RunInfo | None:
        marks = ",".join("?" * len(RESUMABLE_RUN_STATUSES))
        row = self.conn.execute(
            f"""SELECT r.* FROM runs r
                WHERE r.source_file = ? AND r.sheet = ? AND r.search_column = ? AND r.adapter_id = ?
                  AND r.status IN ({marks})
                  AND EXISTS (SELECT 1 FROM items i WHERE i.run_id = r.id AND i.status IN ('pending', 'processing'))
                ORDER BY r.id DESC LIMIT 1""",
            [source_file, sheet, column, adapter_id, *[s.value for s in RESUMABLE_RUN_STATUSES]],
        ).fetchone()
        return RunInfo.from_row(row) if row else None

    def update_run(self, run_id: int, **fields: Any) -> None:
        allowed = {"status", "output_file", "message", "started_at", "finished_at", "name", "settings"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Campos desconhecidos: {unknown}")
        if "status" in fields and isinstance(fields["status"], RunStatus):
            fields["status"] = fields["status"].value
        if "settings" in fields:
            fields["settings_json"] = json.dumps(fields.pop("settings").to_dict(), ensure_ascii=False)
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", [*fields.values(), run_id])

    def set_run_status(self, run_id: int, status: RunStatus, message: str | None = None) -> None:
        fields: dict[str, Any] = {"status": status}
        if message is not None:
            fields["message"] = message
        if status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED, RunStatus.NEEDS_OPERATOR):
            fields["finished_at"] = now_iso()
        self.update_run(run_id, **fields)

    def delete_run(self, run_id: int) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))

    # ----------------------------------------------------------------- items
    def recover_interrupted(self, run_id: int) -> int:
        """Registros que ficaram 'em processamento' (queda/fechamento) voltam para a fila."""
        cur = self.conn.execute(
            "UPDATE items SET status = 'pending', updated_at = ? WHERE run_id = ? AND status = 'processing'",
            (now_iso(), run_id),
        )
        return cur.rowcount

    def next_pending(self, run_id: int) -> ItemRecord | None:
        row = self.conn.execute(
            "SELECT * FROM items WHERE run_id = ? AND status = 'pending' ORDER BY position LIMIT 1", (run_id,)
        ).fetchone()
        return ItemRecord.from_row(row) if row else None

    def mark_processing(self, item_id: int) -> None:
        self.conn.execute(
            "UPDATE items SET status = 'processing', updated_at = ? WHERE id = ?", (now_iso(), item_id)
        )

    def record_attempt(
        self,
        item_id: int,
        run_id: int,
        adapter_id: str,
        started_at: str,
        outcome: str,
        message: str | None = None,
    ) -> int:
        """Grava uma tentativa e incrementa o contador do registro. Retorna o nº da tentativa."""
        with self._tx() as conn:
            attempts = conn.execute(
                "UPDATE items SET attempts = attempts + 1, updated_at = ? WHERE id = ? RETURNING attempts",
                (now_iso(), item_id),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO attempts (item_id, run_id, attempt_no, adapter_id, started_at, finished_at, outcome, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, run_id, attempts, adapter_id, started_at, now_iso(), outcome, message),
            )
        return int(attempts)

    def complete_item(
        self,
        item_id: int,
        status: ItemStatus,
        result: dict[str, Any] | None = None,
        message: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE items SET status = ?, result_json = ?, message = ?, queried_at = ?, duration_ms = ?, updated_at = ?
               WHERE id = ?""",
            (
                status.value,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                message,
                now_iso(),
                duration_ms,
                now_iso(),
                item_id,
            ),
        )

    def release_item(self, item_id: int) -> None:
        """Devolve um registro para a fila sem registrar resultado (ex.: parada durante a consulta)."""
        self.conn.execute(
            "UPDATE items SET status = 'pending', updated_at = ? WHERE id = ? AND status = 'processing'",
            (now_iso(), item_id),
        )

    def reset_items(self, run_id: int, statuses: Iterable[ItemStatus]) -> int:
        """Recoloca registros na fila (ex.: repetir erros ou reconsultar concluídos por solicitação)."""
        values = [s.value for s in statuses if s in (ItemStatus.ERROR, ItemStatus.FOUND, ItemStatus.NOT_FOUND)]
        if not values:
            return 0
        marks = ",".join("?" * len(values))
        cur = self.conn.execute(
            f"""UPDATE items SET status = 'pending', result_json = NULL, message = NULL, queried_at = NULL,
                       duration_ms = NULL, updated_at = ?
                WHERE run_id = ? AND status IN ({marks})""",
            [now_iso(), run_id, *values],
        )
        return cur.rowcount

    def get_item(self, item_id: int) -> ItemRecord:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(f"Registro {item_id} não encontrado")
        return ItemRecord.from_row(row)

    def items(self, run_id: int, statuses: Iterable[ItemStatus] | None = None) -> list[ItemRecord]:
        sql, params = "SELECT * FROM items WHERE run_id = ?", [run_id]
        if statuses is not None:
            values = [s.value for s in statuses]
            sql += f" AND status IN ({','.join('?' * len(values))})"
            params += values
        rows = self.conn.execute(sql + " ORDER BY position", params).fetchall()
        return [ItemRecord.from_row(r) for r in rows]

    def stats(self, run_id: int) -> RunStats:
        stats = RunStats()
        for row in self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM items WHERE run_id = ? GROUP BY status", (run_id,)
        ):
            setattr(stats, row["status"], row["n"])
        return stats

    # -------------------------------------------------------------- auditoria
    def attempts(self, item_id: int) -> list[AttemptRecord]:
        rows = self.conn.execute(
            "SELECT attempt_no, started_at, finished_at, outcome, message FROM attempts WHERE item_id = ? ORDER BY id",
            (item_id,),
        ).fetchall()
        return [AttemptRecord(**dict(r)) for r in rows]

    def add_event(self, run_id: int, kind: str, message: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO run_events (run_id, created_at, kind, message) VALUES (?, ?, ?, ?)",
            (run_id, now_iso(), kind, message),
        )

    def events(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT created_at, kind, message FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]
