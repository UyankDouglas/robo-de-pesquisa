"""Thread dona do navegador do Phoenix Bot.

O Playwright síncrono só pode ser usado na thread que o criou. Esta thread abre o navegador para o
login manual e executa nela os testes, a inspeção e o lote — reaproveitando a mesma sessão.
A interface conversa com ela por filas e chama pause/resume/stop do motor (thread-safe).
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from core.browser import BrowserManager
from core.engine import ProcessingEngine
from core.session import SessionStore
from database.connection import Database
from phoenix.adapter import PhoenixAdapter
from phoenix.client import PhoenixClient
from phoenix.config import PhoenixSettings, load_selectors
from phoenix.inspector import capture
from phoenix.jobs import make_engine
from phoenix.normalize import mask
from utils.logger import get_logger

log = get_logger("phoenix.worker")


@dataclass
class WorkerEvent:
    command: str
    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class PhoenixWorker(threading.Thread):
    def __init__(self, db: Database, on_event: Callable[[Any], None], settings: Callable[[], PhoenixSettings]):
        super().__init__(name="phoenix-worker", daemon=True)
        self.db = db
        self.on_event = on_event
        self.settings = settings
        self.commands: "queue.Queue[tuple]" = queue.Queue()
        self.browser: BrowserManager | None = None
        self.engine: ProcessingEngine | None = None
        self.busy = False
        self.sessions = SessionStore()

    # ------------------------------------------------------------- interface
    def submit(self, command: str, *args: Any) -> None:
        self.busy = True
        self.commands.put((command, args))

    def shutdown(self) -> None:
        if self.engine:
            self.engine.stop()
        self.commands.put(("quit", ()))

    # ------------------------------------------------------------- thread
    def run(self) -> None:
        while True:
            command, args = self.commands.get()
            if command == "quit":
                self._close_browser()
                return
            try:
                handler = getattr(self, f"_cmd_{command}")
                result = handler(*args) or WorkerEvent(command, True)
            except Exception as exc:
                log.error("%s falhou: %s", command, exc)
                result = WorkerEvent(command, False, f"{type(exc).__name__}: {exc}")
            self.busy = not self.commands.empty()
            self.on_event(result)

    def _client(self) -> PhoenixClient:
        settings = self.settings()
        return PhoenixClient(lambda: self.browser.page, load_selectors(), settings.base_url,
                             settings.timeout_seconds, settings.max_paginas)

    def _ensure_browser(self) -> BrowserManager:
        if self.browser is None or self.browser.browser is None or not self.browser.browser.is_connected():
            settings = self.settings()
            state = self.sessions.load(PhoenixAdapter.id) if settings.persist_session else None
            self.browser = BrowserManager(headless=False, channel=settings.browser_channel,
                                          timeout_seconds=settings.timeout_seconds, storage_state=state).start()
        return self.browser

    def _close_browser(self) -> None:
        if self.browser:
            self.browser.close()
            self.browser = None

    def _save_session(self) -> None:
        if self.settings().persist_session and self.browser:
            state = self.browser.export_storage_state()
            if state and self.sessions.save(PhoenixAdapter.id, state):
                log.info("Sessão do Phoenix2 salva de forma protegida (DPAPI).")

    # ------------------------------------------------------------- comandos
    def _cmd_open_browser(self) -> WorkerEvent:
        self._ensure_browser()
        client = self._client()
        client.open_home()
        self.browser.bring_to_front()
        return WorkerEvent("open_browser", True, "Navegador aberto no Phoenix2. Faça o login e depois clique em 'Testar conexão'.")

    def _cmd_test_connection(self) -> WorkerEvent:
        self._ensure_browser()
        client = self._client()
        if not client.is_logged_in():
            return WorkerEvent("test_connection", False, "Não está logado no Phoenix2 (menu 'Portabilidade' não encontrado). "
                                                         "Faça o login na janela do navegador.")
        client.go_to_status_page()
        self._save_session()
        return WorkerEvent("test_connection", True, "Conexão OK: logado e tela 'Status das Solicitações' aberta pelo menu.")

    def _cmd_test_search(self, cnpj: str) -> WorkerEvent:
        self._ensure_browser()
        client = self._client()
        result = client.consultar(cnpj)
        sols = result["solicitacoes"]
        statuses: dict[str, int] = {}
        for sol in sols:
            for ticket in sol.get("bilhetes", []):
                key = ticket.get("status_bilhete") or "(vazio)"
                statuses[key] = statuses.get(key, 0) + 1
        msg = (f"CNPJ {mask(cnpj)}: {len(sols)} solicitação(ões), {sum(statuses.values())} bilhete(s). "
               + ("Status do Bilhete: " + "; ".join(f"{k} x{v}" for k, v in statuses.items()) if statuses else
                  "Nenhuma Solicitação foi encontrada." if not sols else ""))
        return WorkerEvent("test_search", True, msg, {"result": result})

    def _cmd_inspect(self, label: str) -> WorkerEvent:
        self._ensure_browser()
        menu = load_selectors()["menu"]
        path = capture(self.browser.page, label, [menu["portabilidade"], menu["acompanhamento"], menu["status_solicitacoes"]])
        return WorkerEvent("inspect", True, f"Página capturada em {path}", {"path": str(path)})

    def _cmd_run(self, run_id: int) -> WorkerEvent:
        self._ensure_browser()
        self.engine = make_engine(self.db, run_id, interactive=True, on_event=self.on_event, browser=self.browser)
        try:
            status = self.engine.run()
        finally:
            self.engine = None
        self._save_session()
        return WorkerEvent("run", True, f"Execução {run_id}: {status.label}", {"run_id": run_id})

    def _cmd_close_browser(self) -> WorkerEvent:
        self._close_browser()
        return WorkerEvent("close_browser", True, "Navegador fechado.")
