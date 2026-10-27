"""Motor de processamento: percorre a fila de registros de uma execução.

Fluxo por registro: aguarda a vez (intervalo/limites) -> pesquisa -> grava resultado e
tentativa no SQLite -> salva o Excel periodicamente -> próximo registro.

Controles thread-safe: ``pause()``, ``resume()`` (também confirma ação do operador) e ``stop()``.
A parada é segura: a consulta em andamento termina (ou expira pelo timeout), o progresso é
gravado e a execução fica disponível para retomada.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from adapters.base_adapter import AdapterContext, BaseAdapter
from core.browser import BrowserManager
from core.config import RunSettings, output_dir_for
from core.locking import RunLock
from core.models import (
    HumanVerificationRequired,
    ItemStatus,
    OperatorRequired,
    RateLimitedError,
    RunStatus,
    SessionExpiredError,
)
from core.retry import RateLimiter, RetryPolicy, interruptible_sleep
from core.session import SessionStore
from database.connection import Database
from database.repository import ItemRecord, Repository, RunInfo, RunStats, now_iso
from excel.exporter import default_output_path, export_run
from utils.logger import get_logger, mask_identifier, run_log

log = get_logger("engine")

MAX_RATE_LIMIT_WAITS = 10


class EngineState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_OPERATOR = "waiting_operator"
    STOPPING = "stopping"
    FINISHED = "finished"


@dataclass
class EngineEvent:
    kind: str  # state | item_started | item_finished | progress | operator | exported | finished
    data: dict[str, Any] = field(default_factory=dict)


class _Cancelled(Exception):
    pass


def default_adapter_factory(settings: RunSettings) -> BaseAdapter:
    from adapters.registry import create_adapter

    return create_adapter(settings.adapter_id, settings.adapter_options)


def default_browser_factory(settings: RunSettings, storage_state: dict | None) -> BrowserManager:
    return BrowserManager(
        headless=settings.headless,
        channel=settings.browser_channel,
        timeout_seconds=settings.timeout_seconds,
        storage_state=storage_state,
    ).start()


class ProcessingEngine:
    def __init__(
        self,
        db: Database,
        run_id: int,
        *,
        interactive: bool = True,
        on_event: Callable[[EngineEvent], None] | None = None,
        adapter_factory: Callable[[RunSettings], BaseAdapter] = default_adapter_factory,
        browser_factory: Callable[[RunSettings, dict | None], BrowserManager] = default_browser_factory,
        session_store: SessionStore | None = None,
        export_filter: str = "all",
        exporter: Callable[[Repository, int], Path | None] | None = None,
        lock_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.db = db
        self.run_id = run_id
        self.interactive = interactive
        self.on_event = on_event
        self.adapter_factory = adapter_factory
        self.browser_factory = browser_factory
        self.sessions = session_store or SessionStore()
        self.export_filter = export_filter
        self.exporter = exporter  # None = planilha de resultados padrão (excel.exporter)
        self.lock_dir = lock_dir
        self.clock = clock

        self._stop = threading.Event()
        self._pause = threading.Event()
        self._operator_ack = threading.Event()
        self._state = EngineState.IDLE
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None

        self._started_at: float | None = None
        self._idle_total = 0.0
        self._idle_since: float | None = None
        self._processed = 0
        self._remaining = 0
        self._since_save = 0
        self._last_save = 0.0
        self._last_export_duration = 0.0

        self.final_status: RunStatus | None = None
        self.final_message: str = ""
        self.output_path: Path | None = None
        self._settings: RunSettings | None = None
        self._browser: BrowserManager | None = None
        self._field_labels: list[tuple[str, str]] = []

    # ================================================================ controles
    @property
    def state(self) -> EngineState:
        with self._state_lock:
            return self._state

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("O motor já está em execução")
        self._thread = threading.Thread(target=self._thread_main, name=f"engine-run-{self.run_id}", daemon=True)
        self._thread.start()
        return self._thread

    def join(self, timeout: float | None = None) -> bool:
        if self._thread:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def pause(self) -> None:
        if self.state in (EngineState.RUNNING, EngineState.STARTING):
            self._pause.set()
            log.info("Pausa solicitada: a consulta atual será concluída antes de pausar.")

    def resume(self) -> None:
        """Continua após pausa ou confirma que o operador concluiu a ação pedida (login/CAPTCHA)."""
        self._pause.clear()
        self._operator_ack.set()

    def stop(self) -> None:
        if not self._stop.is_set():
            log.info("Parada solicitada: finalizando com segurança...")
        self._stop.set()
        self._pause.clear()
        self._operator_ack.set()
        if self.state not in (EngineState.FINISHED, EngineState.IDLE):
            self._set_state(EngineState.STOPPING)

    # ================================================================ métricas
    def elapsed(self) -> float:
        """Tempo ativo (sem pausas e esperas pelo operador)."""
        if self._started_at is None:
            return 0.0
        idle = self._idle_total + (self.clock() - self._idle_since if self._idle_since else 0.0)
        return max(self.clock() - self._started_at - idle, 0.0)

    def eta(self) -> float | None:
        """Estimativa do tempo restante com base na velocidade observada nesta sessão."""
        if self._processed == 0:
            return None
        return self.elapsed() / self._processed * self._remaining

    # ================================================================ internos
    def _emit(self, kind: str, **data: Any) -> None:
        if self.on_event:
            try:
                self.on_event(EngineEvent(kind, data))
            except Exception:  # pragma: no cover - a interface nunca derruba o worker
                log.debug("Falha ao entregar evento %s", kind, exc_info=True)

    def _set_state(self, state: EngineState, message: str = "") -> None:
        with self._state_lock:
            previous, self._state = self._state, state
        idle_states = (EngineState.PAUSED, EngineState.WAITING_OPERATOR)
        if state in idle_states and previous not in idle_states:
            self._idle_since = self.clock()
        elif state not in idle_states and self._idle_since is not None:
            self._idle_total += self.clock() - self._idle_since
            self._idle_since = None
        self._emit("state", state=state, message=message)

    def _sleep(self, seconds: float) -> bool:
        return interruptible_sleep(seconds, self._stop, self._pause)

    def _checkpoint(self, repo: Repository) -> None:
        """Ponto seguro entre operações: aplica pausa e parada."""
        if self._stop.is_set():
            raise _Cancelled()
        if self._pause.is_set():
            self._set_state(EngineState.PAUSED, "Execução pausada")
            repo.set_run_status(self.run_id, RunStatus.PAUSED)
            repo.add_event(self.run_id, "paused")
            log.info("Execução pausada. Clique em CONTINUAR para prosseguir.")
            self._export(repo)  # garante o Excel atualizado durante a pausa
            while self._pause.is_set() and not self._stop.is_set():
                self._operator_ack.wait(0.2)
            if self._stop.is_set():
                raise _Cancelled()
            repo.set_run_status(self.run_id, RunStatus.RUNNING)
            repo.add_event(self.run_id, "resumed")
            log.info("Execução retomada.")
            self._set_state(EngineState.RUNNING)

    def _wait_turn(self, repo: Repository, rate: RateLimiter) -> None:
        while True:
            self._checkpoint(repo)
            if rate.acquire(self._sleep):
                return

    def _display(self, identifier: str) -> str:
        if self._settings and self._settings.mask_identifiers_in_logs:
            return mask_identifier(identifier)
        return identifier

    def _request_operator(self, repo: Repository, reason: str, message: str) -> None:
        """Pausa até o operador agir no navegador e clicar em CONTINUAR."""
        settings = self._settings
        if not self.interactive:
            raise OperatorRequired(message)
        if settings and settings.headless and self._browser is not None:
            raise OperatorRequired(
                f"{message} O navegador está oculto (modo headless): desative essa opção e retome a execução."
            )
        self._operator_ack.clear()
        repo.add_event(self.run_id, f"operator_{reason}", message)
        log.warning("AÇÃO DO OPERADOR NECESSÁRIA: %s", message)
        self._set_state(EngineState.WAITING_OPERATOR, message)
        self._emit("operator", reason=reason, message=message)
        if self._browser:
            self._browser.bring_to_front()
        while not self._operator_ack.is_set():
            self._operator_ack.wait(0.2)
        if self._stop.is_set():
            raise _Cancelled()
        self._set_state(EngineState.RUNNING)

    def _login_flow(self, repo: Repository, adapter: BaseAdapter) -> None:
        while True:
            instructions = adapter.begin_login() if self.interactive else (
                f"O site '{adapter.display_name}' exige login e não há operador neste modo. Faça o login pela "
                f"interface ou salve a sessão antes (comando 'login' do modo sem interface)."
            )
            self._request_operator(repo, "login", instructions)
            if adapter.is_logged_in():
                log.info("Login confirmado.")
                repo.add_event(self.run_id, "login_ok")
                self._save_session(adapter)
                return
            log.warning("O login não foi confirmado no site. Tente novamente.")

    def _save_session(self, adapter: BaseAdapter) -> None:
        if self._settings and self._settings.persist_session and adapter.requires_login:
            state = adapter.storage_state()
            if state and self.sessions.save(adapter.id, state):
                log.info("Sessão salva de forma protegida (DPAPI).")

    def _export(self, repo: Repository, adapter: BaseAdapter | None = None) -> None:
        settings = self._settings
        if settings is None:
            return
        try:
            started = self.clock()
            if self.exporter is not None:
                saved = self.exporter(repo, self.run_id)
            else:
                run = repo.get_run(self.run_id)
                target = Path(run.output_file) if run.output_file else default_output_path(run, output_dir_for(settings))
                labels = adapter.field_labels() if adapter else self._field_labels
                saved = export_run(repo, self.run_id, target, self.export_filter, labels)
                if not run.output_file:
                    repo.update_run(self.run_id, output_file=str(target))
            self.output_path = saved
            self._since_save = 0
            self._last_save = self.clock()
            self._last_export_duration = self._last_save - started
            self._emit("exported", path=str(saved) if saved else "")
        except Exception as exc:
            log.error("Não foi possível salvar o Excel agora (%s). Os resultados continuam salvos no banco.", exc)

    def _autosave_due(self, settings: RunSettings) -> bool:
        """Regrava o Excel a cada N registros ou N segundos, mas sem gastar mais de ~10% do tempo
        exportando (planilhas grandes). O SQLite já guarda cada resultado no momento da consulta."""
        since_last = self.clock() - self._last_save
        if since_last < self._last_export_duration * 10:
            return False
        return self._since_save >= settings.autosave_every or since_last >= settings.autosave_seconds

    def _progress(self, repo: Repository) -> RunStats:
        stats = repo.stats(self.run_id)
        self._remaining = stats.remaining
        self._emit("progress", stats=stats, elapsed=self.elapsed(), eta=self.eta())
        return stats

    # ================================================================ execução
    def _thread_main(self) -> None:
        try:
            self.run()
        except Exception as exc:  # AlreadyRunningError e afins
            self.final_status, self.final_message = None, str(exc)
            log.error("%s", exc)
            self._set_state(EngineState.FINISHED, str(exc))
            self._emit("finished", run_status=None, message=str(exc), stats=None, output=None)

    def run(self) -> RunStatus:
        """Executa de forma bloqueante (o CLI chama direto; a interface usa ``start()``)."""
        self._set_state(EngineState.STARTING)
        lock = RunLock(self.run_id, self.lock_dir).acquire()  # AlreadyRunningError se ocupado
        repo = Repository(self.db.connect())
        try:
            run = repo.get_run(self.run_id)  # KeyError se a execução não existir
        except Exception:
            repo.close()
            lock.release()
            raise
        status, message = RunStatus.FAILED, ""
        adapter: BaseAdapter | None = None
        stats = RunStats()
        try:
            with run_log(self.run_id):
                try:
                    self._settings = settings = run.settings
                    recovered = repo.recover_interrupted(self.run_id)
                    if recovered:
                        log.info("%d registro(s) interrompido(s) voltaram para a fila.", recovered)
                    kind = "resumed" if run.started_at else "started"
                    repo.update_run(self.run_id, status=RunStatus.RUNNING, message=None,
                                    **({} if run.started_at else {"started_at": now_iso()}))
                    repo.add_event(self.run_id, kind)
                    self._started_at, self._last_save = self.clock(), self.clock()
                    stats = self._progress(repo)
                    log.info("Execução %d %s: %d registro(s), %d pendente(s). Site: %s",
                             self.run_id, "retomada" if kind == "resumed" else "iniciada",
                             stats.total, stats.remaining, settings.adapter_id)

                    adapter = self.adapter_factory(settings)
                    self._field_labels = adapter.field_labels()
                    status, message = self._run_with_adapter(repo, run, adapter)
                except _Cancelled:
                    status, message = RunStatus.STOPPED, "Execução parada pelo operador"
                except OperatorRequired as exc:
                    status, message = RunStatus.NEEDS_OPERATOR, str(exc)
                    log.error("Execução interrompida: %s", exc)
                except Exception as exc:
                    status, message = RunStatus.FAILED, f"{type(exc).__name__}: {exc}"
                    log.exception("Falha na execução: %s", exc)
                finally:
                    repo.recover_interrupted(self.run_id)
                    if status == RunStatus.COMPLETED:
                        stats = repo.stats(self.run_id)
                        message = (f"Concluída: {stats.found} encontrado(s), {stats.not_found} sem resultado, "
                                   f"{stats.error} erro(s).")
                    repo.set_run_status(self.run_id, status, message)
                    repo.add_event(self.run_id, status.value, message)
                    self._export(repo, adapter)
                    stats = self._progress(repo)
                    self.final_status, self.final_message = status, message
                    log.info("Execução %d finalizada: %s. %s", self.run_id, status.label, message)
        finally:
            repo.close()
            lock.release()
        self._set_state(EngineState.FINISHED, message)
        self._emit("finished", run_status=status, message=message, stats=stats,
                   output=str(self.output_path) if self.output_path else None)
        return status

    def _run_with_adapter(self, repo: Repository, run: RunInfo, adapter: BaseAdapter) -> tuple[RunStatus, str]:
        settings = run.settings
        browser = None
        try:
            if adapter.requires_browser:
                storage = None
                if settings.persist_session and adapter.requires_login:
                    storage = self.sessions.load(adapter.id)
                    if storage:
                        log.info("Sessão salva encontrada; tentando reutilizá-la.")
                browser = self._browser = self.browser_factory(settings, storage)
            adapter.open(AdapterContext(timeout_seconds=settings.timeout_seconds, browser=browser))
            self._set_state(EngineState.RUNNING)
            if adapter.requires_login and not adapter.is_logged_in():
                self._login_flow(repo, adapter)

            rate = RateLimiter(max(settings.interval_seconds, adapter.min_interval()), settings.max_per_minute, self.clock)
            policy = RetryPolicy(settings.max_retries, settings.backoff_base_seconds, settings.backoff_max_seconds)
            while True:
                self._checkpoint(repo)
                item = repo.next_pending(self.run_id)
                if item is None:
                    break
                self._process_item(repo, item, adapter, rate, policy)
                self._processed += 1
                self._since_save += 1
                self._progress(repo)
                if self._autosave_due(settings):
                    self._export(repo, adapter)
            self._save_session(adapter)
            return RunStatus.COMPLETED, ""
        finally:
            try:
                adapter.close()
            except Exception:
                log.debug("Erro ao fechar adaptador", exc_info=True)
            if browser is not None:
                browser.close()
            self._browser = None

    def _process_item(
        self,
        repo: Repository,
        item: ItemRecord,
        adapter: BaseAdapter,
        rate: RateLimiter,
        policy: RetryPolicy,
    ) -> ItemStatus:
        shown = self._display(item.identifier)
        retries = rate_waits = 0
        started = self.clock()
        while True:
            self._wait_turn(repo, rate)
            repo.mark_processing(item.id)
            self._emit("item_started", item_id=item.id, position=item.position, row_number=item.row_number,
                       identifier=shown)
            log.info("Consultando registro %d (linha %d): %s", item.position, item.row_number, shown)
            attempt_started = now_iso()
            try:
                result = adapter.search(item.identifier)
            except HumanVerificationRequired as exc:
                repo.record_attempt(item.id, self.run_id, adapter.id, attempt_started, "operator", str(exc))
                self._request_operator(repo, "captcha",
                                       f"{exc}. Resolva a verificação na janela do navegador e clique em CONTINUAR.")
                continue
            except SessionExpiredError as exc:
                repo.record_attempt(item.id, self.run_id, adapter.id, attempt_started, "session", str(exc))
                log.warning("Sessão expirada: %s", exc)
                self._login_flow(repo, adapter)
                continue
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}".splitlines()[0][:500]
                transient = adapter.classify_error(exc) == "transient"
                if isinstance(exc, RateLimitedError) and rate_waits < MAX_RATE_LIMIT_WAITS:
                    rate_waits += 1
                    wait = exc.retry_after or policy.delay(rate_waits)
                    repo.record_attempt(item.id, self.run_id, adapter.id, attempt_started, "rate_limited", message)
                    log.warning("%s: limite de consultas do site atingido; aguardando %.0fs.", shown, wait)
                    rate.block_for(wait)
                    continue
                if transient and retries < policy.max_retries:
                    retries += 1
                    delay = policy.delay(retries)
                    repo.record_attempt(item.id, self.run_id, adapter.id, attempt_started, "transient_error", message)
                    log.warning("%s: falha temporária (%s). Nova tentativa %d/%d em %.1fs.",
                                shown, message, retries, policy.max_retries, delay)
                    rate.block_for(delay)
                    try:
                        adapter.recover(exc)
                    except Exception:
                        log.debug("Falha ao recuperar adaptador", exc_info=True)
                    continue
                repo.record_attempt(item.id, self.run_id, adapter.id, attempt_started, "error", message)
                repo.complete_item(item.id, ItemStatus.ERROR, None, message, self._ms_since(started))
                log.error("%s: ERRO — %s", shown, message)
                self._emit("item_finished", item_id=item.id, status=ItemStatus.ERROR, identifier=shown,
                           message=message, row_number=item.row_number)
                return ItemStatus.ERROR

            status = ItemStatus.FOUND if result.found else ItemStatus.NOT_FOUND
            repo.record_attempt(item.id, self.run_id, adapter.id, attempt_started, status.value, result.message or None)
            repo.complete_item(item.id, status, result.data if result.found else None,
                               result.message or None, self._ms_since(started))
            if status == ItemStatus.FOUND:
                log.info("%s: concluído — informação encontrada.", shown)
            else:
                log.info("%s: sem resultado (%s).", shown, result.message or "não encontrado")
            self._emit("item_finished", item_id=item.id, status=status, identifier=shown,
                       message=result.message, row_number=item.row_number)
            return status

    def _ms_since(self, started: float) -> int:
        return int((self.clock() - started) * 1000)
