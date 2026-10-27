"""Operações de alto nível compartilhadas pela interface gráfica e pelo modo sem interface."""
from __future__ import annotations

from pathlib import Path

from core import paths
from core.config import JobConfig, RunSettings, output_dir_for
from core.locking import RunLock
from core.models import ItemStatus, RunStatus
from database.connection import Database
from database.repository import Repository
from excel.exporter import default_output_path, export_run
from excel.importer import ImportResult, import_excel
from utils.logger import get_logger

log = get_logger("jobs")


def open_database(path: Path | None = None) -> Database:
    paths.ensure_dirs()
    return Database(path or paths.DB_PATH)


def mark_stale_runs(db: Database, lock_dir: Path | None = None) -> int:
    """Execuções marcadas como 'em execução' sem processo ativo foram interrompidas (queda, fechamento)."""
    repo = Repository(db.connect())
    try:
        count = 0
        for run in repo.runs_with_status(RunStatus.RUNNING, RunStatus.PAUSED):
            if not RunLock.is_locked(run.id, lock_dir):
                repo.set_run_status(run.id, RunStatus.INTERRUPTED, "Execução interrompida inesperadamente")
                repo.add_event(run.id, "interrupted")
                count += 1
        return count
    finally:
        repo.close()


def create_run(db: Database, name: str, imported: ImportResult, settings: RunSettings) -> int:
    settings.validate()
    repo = Repository(db.connect())
    try:
        run_id = repo.create_run(name, imported, settings)
        log.info("Execução %d criada. %s", run_id, imported.summary())
        return run_id
    finally:
        repo.close()


def prepare_job_run(db: Database, job: JobConfig) -> tuple[int, bool]:
    """Cria a execução do modo agendado ou retoma uma incompleta da mesma planilha/coluna/site.

    Retorna (id da execução, True se foi retomada).
    """
    job.validate()
    imported = import_excel(job.input)
    repo = Repository(db.connect())
    try:
        if job.resume:
            existing = repo.find_resumable_run(imported.file, imported.sheet, imported.column, job.settings.adapter_id)
            if existing:
                repo.update_run(existing.id, settings=job.settings)
                if job.retry_errors_on_resume:
                    reset = repo.reset_items(existing.id, [ItemStatus.ERROR])
                    log.info("%d registro(s) com erro voltaram para a fila.", reset)
                log.info("Retomando a execução %d (%s).", existing.id, existing.status.label)
                return existing.id, True
            if job.retry_errors_on_resume:
                # execução anterior concluída, mas com erros: repete só os erros
                for run in repo.list_runs(50):
                    same = (run.source_file, run.sheet, run.search_column, run.adapter_id) == (
                        imported.file, imported.sheet, imported.column, job.settings.adapter_id)
                    if same and repo.stats(run.id).error:
                        reset = repo.reset_items(run.id, [ItemStatus.ERROR])
                        repo.update_run(run.id, settings=job.settings)
                        log.info("Repetindo %d erro(s) da execução %d.", reset, run.id)
                        return run.id, True
        run_id = repo.create_run(job.name, imported, job.settings)
        log.info("Execução %d criada. %s", run_id, imported.summary())
        return run_id, False
    finally:
        repo.close()


def field_labels_for(adapter_id: str) -> list[tuple[str, str]]:
    from adapters.registry import discover

    info = discover().get(adapter_id)
    return list(info.fields) if info else []


def export(db: Database, run_id: int, export_filter: str = "all", output_path: str | Path | None = None) -> Path:
    repo = Repository(db.connect())
    try:
        run = repo.get_run(run_id)
        if output_path is None:
            base = default_output_path(run, output_dir_for(run.settings))
            output_path = base if export_filter == "all" else base.with_name(f"{base.stem}_{export_filter}{base.suffix}")
        return export_run(repo, run_id, Path(output_path), export_filter, field_labels_for(run.adapter_id))
    finally:
        repo.close()
