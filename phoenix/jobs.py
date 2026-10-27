"""Operações do Phoenix Bot usadas pela interface e pelo modo sem interface."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core import paths
from core.browser import BorrowedBrowser, BrowserManager
from core.config import RunSettings, resolve_path
from core.engine import EngineEvent, ProcessingEngine
from core.locking import RunLock
from core.models import ConfigError, RunStatus
from database.connection import Database
from database.repository import Repository
from phoenix.adapter import PhoenixAdapter
from phoenix.config import PhoenixSettings, SheetMapping, load_selectors
from phoenix.exporter import build_report, export_job
from phoenix.matcher import MatchReport
from phoenix.normalize import PhoneRules
from phoenix.repository import PhoenixRepository
from phoenix.workbook import file_sha256, load_sheets
from utils.logger import get_logger

log = get_logger("phoenix.jobs")


def open_database(path: Path | None = None) -> Database:
    paths.ensure_dirs()
    db = Database(path or (paths.DATA_DIR / "phoenix_bot.db"))
    conn = db.connect()
    PhoenixRepository(Repository(conn))  # cria as tabelas do Phoenix
    conn.close()
    return db


def mark_stale_runs(db: Database) -> int:
    repo = Repository(db.connect())
    try:
        count = 0
        for run in repo.runs_with_status(RunStatus.RUNNING, RunStatus.PAUSED):
            if not RunLock.is_locked(run.id):
                repo.set_run_status(run.id, RunStatus.INTERRUPTED, "Execução interrompida inesperadamente")
                count += 1
        return count
    finally:
        repo.close()


def phone_rules(settings: PhoenixSettings) -> PhoneRules:
    return PhoneRules(strip_ddi_55=settings.match.strip_ddi_55, strip_trunk_zero=settings.match.strip_trunk_zero)


def run_settings_for(settings: PhoenixSettings) -> RunSettings:
    return RunSettings(
        adapter_id=PhoenixAdapter.id,
        adapter_options={"base_url": settings.base_url, "max_paginas": settings.max_paginas},
        interval_seconds=settings.interval_seconds,
        max_per_minute=settings.max_per_minute,
        timeout_seconds=settings.timeout_seconds,
        max_retries=settings.max_retries,
        backoff_base_seconds=settings.backoff_base_seconds,
        backoff_max_seconds=settings.backoff_max_seconds,
        headless=settings.headless,
        browser_channel=settings.browser_channel,
        persist_session=settings.persist_session,
        autosave_every=settings.autosave_every,
        autosave_seconds=120,
        output_dir=settings.output_dir,
        mask_identifiers_in_logs=True,  # CNPJ e telefones nunca aparecem completos nos logs
    ).validate()


def create_job(db: Database, source_file: str | Path, mappings: dict[str, SheetMapping],
               settings: PhoenixSettings, resume: bool = True) -> tuple[int, bool]:
    """Importa as abas selecionadas e cria a execução (ou devolve a incompleta equivalente)."""
    if not mappings:
        raise ConfigError("Selecione ao menos uma aba.")
    settings.validate()
    source = str(Path(source_file).resolve())
    sha = file_sha256(source)
    repo = Repository(db.connect())
    prepo = PhoenixRepository(repo)
    try:
        if resume:
            existing = prepo.find_resumable(sha, list(mappings.values()))
            if existing:
                return existing, True
        sheets = load_sheets(source, mappings, phone_rules(settings))
        name = f"{Path(source).name} [{len(sheets)} aba(s)]"
        run_id = prepo.create_job(name, source, sha, sheets, settings, run_settings_for(settings))
        log.info("Execução %d criada: %d linha(s), %d CNPJ(s) distinto(s).", run_id,
                 sum(len(s.rows) for s in sheets), repo.stats(run_id).total)
        return run_id, False
    finally:
        repo.close()


def apply_settings(db: Database, run_id: int, settings: PhoenixSettings) -> None:
    """Na retomada, usa intervalo/timeout/regras atuais (a correspondência é recalculada na exportação)."""
    repo = Repository(db.connect())
    try:
        PhoenixRepository(repo).update_settings(run_id, settings.validate())
        repo.update_run(run_id, settings=run_settings_for(settings))
    finally:
        repo.close()


def output_dir(settings: PhoenixSettings) -> Path:
    return resolve_path(settings.output_dir) if settings.output_dir else paths.OUTPUT_DIR / "phoenix"


def export(db: Database, run_id: int) -> dict[str, Path]:
    repo = Repository(db.connect())
    try:
        settings = PhoenixRepository(repo).job(run_id).settings
        return export_job(repo, run_id, output_dir(settings))
    finally:
        repo.close()


def report(db: Database, run_id: int) -> MatchReport:
    repo = Repository(db.connect())
    try:
        return build_report(PhoenixRepository(repo), run_id)[1]
    finally:
        repo.close()


def make_engine(db: Database, run_id: int, *, interactive: bool,
                on_event: Callable[[EngineEvent], None] | None = None,
                browser: BrowserManager | None = None,
                selectors: dict[str, Any] | None = None) -> ProcessingEngine:
    """Motor configurado para o Phoenix. Com ``browser``, reaproveita o navegador já logado."""
    repo = Repository(db.connect())
    try:
        settings = PhoenixRepository(repo).job(run_id).settings
    finally:
        repo.close()
    selectors = selectors or load_selectors()

    def adapter_factory(run_settings: RunSettings) -> PhoenixAdapter:
        return PhoenixAdapter({**run_settings.adapter_options, "selectors": selectors})

    def exporter(repo: Repository, rid: int):
        files = export_job(repo, rid, output_dir(settings))
        return files.get("RELATORIO_TECNICO")

    kwargs: dict[str, Any] = {}
    if browser is not None:
        kwargs["browser_factory"] = lambda s, state: BorrowedBrowser(browser)
    return ProcessingEngine(db, run_id, interactive=interactive, on_event=on_event,
                            adapter_factory=adapter_factory, exporter=exporter, **kwargs)
