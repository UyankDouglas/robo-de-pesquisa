"""Janela principal (CustomTkinter).

O processamento roda em uma thread de trabalho (``ProcessingEngine``). A comunicação com a
interface é feita por filas (eventos do motor e logs) lidas periodicamente com ``after()``,
de modo que a janela nunca congela e nenhum widget é tocado fora da thread da interface.
"""
from __future__ import annotations

import json
import os
import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

import customtkinter as ctk

from adapters.registry import AdapterInfo, discover
from core import paths
from core.config import ImportOptions, JobConfig, RunSettings, resolve_path, save_job_config
from core.engine import EngineEvent, EngineState, ProcessingEngine
from core.jobs import create_run, export, mark_stale_runs, open_database
from core.models import ConfigError, ItemStatus, RunStatus
from database.connection import Database
from database.repository import Repository, RunStats
from excel.exporter import EXPORT_FILTERS
from excel.importer import ImportResult, build_processing_list, import_excel, inspect_workbook, read_sheet
from ui.components import LogView, StatCard, Table, format_duration, style_treeview
from utils.logger import attach_queue_handler, get_logger

log = get_logger("ui")

VALIDATIONS = {"Nenhuma": "none", "Somente dígitos": "digits", "CNPJ": "cnpj", "CPF": "cpf", "CPF ou CNPJ": "cpf_cnpj"}
CHANNELS = {"Microsoft Edge (instalado)": "msedge", "Google Chrome (instalado)": "chrome", "Chromium do Playwright": "chromium"}
EXPORT_LABELS = {label: key for key, (label, _) in EXPORT_FILTERS.items()}
THEMES = {"Sistema": "system", "Claro": "light", "Escuro": "dark"}
GREEN = ("#2e7d32", "#2e7d32")
GREEN_HOVER = ("#1b5e20", "#1b5e20")
RED = ("#c62828", "#b71c1c")
RED_HOVER = ("#8e0000", "#7f0000")
ORANGE = ("#ef6c00", "#e65100")
DISABLED = ("#a8adb3", "#4a4d50")


def _key_for(mapping: dict[str, str], value: str, default: str) -> str:
    return next((label for label, key in mapping.items() if key == value), default)


class WebSearchBotApp(ctk.CTk):
    def __init__(self, db: Database | None = None, state_path: Path | None = None):
        super().__init__()
        paths.ensure_dirs()
        self.db = db or open_database()
        mark_stale_runs(self.db)
        self.repo = Repository(self.db.connect())
        self.registry: dict[str, AdapterInfo] = discover()
        self.adapter_by_name = {info.name: info.id for info in self.registry.values()}
        self.state_path = state_path or paths.UI_STATE_PATH

        self.engine: ProcessingEngine | None = None
        self.engine_state = EngineState.IDLE
        self.current_run_id: int | None = None
        self.last_output: str | None = None
        self.frame = None
        self.sheet_infos: dict[str, Any] = {}
        self.ui_queue: "queue.Queue[Any]" = queue.Queue()
        self.log_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._log_handler = attach_queue_handler(self.log_queue)
        self._closing = False
        self._after_ids: dict[str, str] = {}
        self._busy = False

        self.title("Web Search Bot — Robô de Pesquisas e Extração de Dados")
        self._fit_to_screen()
        self._init_vars()
        self._build()
        self._load_ui_state()
        self._refresh_buttons()
        self._refresh_history()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule(100, self._poll)
        self._schedule(1000, self._tick)
        log.info("Web Search Bot pronto. Dados em: %s", paths.DATA_ROOT)

    # =========================================================== variáveis/estado
    def _init_vars(self) -> None:
        self.file_var = ctk.StringVar()
        self.sheet_var = ctk.StringVar(value="—")
        self.column_var = ctk.StringVar(value="—")
        self.start_var = ctk.StringVar()
        self.end_var = ctk.StringVar()
        self.zero_pad_var = ctk.StringVar()
        self.validation_var = ctk.StringVar(value="Nenhuma")
        first_adapter = next(iter(self.adapter_by_name), "")
        self.adapter_var = ctk.StringVar(value=first_adapter)
        self.output_var = ctk.StringVar(value=str(paths.OUTPUT_DIR))
        self.interval_var = ctk.StringVar(value="1")
        self.timeout_var = ctk.StringVar(value="30")
        self.retries_var = ctk.StringVar(value="3")
        self.per_minute_var = ctk.StringVar(value="0")
        self.channel_var = ctk.StringVar(value=_key_for(CHANNELS, "msedge", next(iter(CHANNELS))))
        self.visible_var = ctk.BooleanVar(value=True)
        self.persist_var = ctk.BooleanVar(value=False)
        self.export_filter_var = ctk.StringVar(value=EXPORT_FILTERS["all"][0])
        self.theme_var = ctk.StringVar(value="Sistema")

    def _fit_to_screen(self) -> None:
        """Ajusta a janela à tela (inclusive notebooks 1366x768 e escala de 125%/150% do Windows)."""
        scaling = getattr(self, "_get_window_scaling", lambda: 1.0)() or 1.0
        screen_w = int(self.winfo_screenwidth() / scaling)
        screen_h = int(self.winfo_screenheight() / scaling)
        width, height = min(1400, screen_w - 40), min(900, screen_h - 90)
        self.minsize(min(1000, screen_w - 40), min(620, screen_h - 90))
        self.geometry(f"{width}x{height}+{max((screen_w - width) // 2, 0)}+{max((screen_h - height) // 2 - 20, 0)}")
        if screen_w < 1500 or screen_h < 950:
            self.after(50, lambda: self.state("zoomed"))

    def _schedule(self, ms: int, func: Callable[[], None]) -> None:
        if not self._closing:
            self._after_ids[func.__name__] = self.after(ms, func)

    # =========================================================== construção da tela
    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        left = ctk.CTkScrollableFrame(self, width=340, label_text="Configuração")
        left.grid(row=0, column=0, sticky="nsw", padx=(12, 6), pady=12)
        left.grid_columnconfigure(0, weight=1)
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)
        self._build_config(left)
        self._build_main(right)

    def _section(self, parent, text: str) -> None:
        ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(size=14, weight="bold"), anchor="w").pack(
            fill="x", padx=8, pady=(14, 4))

    def _label(self, parent, text: str) -> None:
        ctk.CTkLabel(parent, text=text, anchor="w").pack(fill="x", padx=8, pady=(6, 0))

    def _field_row(self, parent, pairs: list[tuple[str, ctk.StringVar]]) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=(4, 0))
        for i, (text, var) in enumerate(pairs):
            row.grid_columnconfigure(i, weight=1)
            cell = ctk.CTkFrame(row, fg_color="transparent")
            cell.grid(row=0, column=i, sticky="ew", padx=(0, 6))
            ctk.CTkLabel(cell, text=text, anchor="w", font=ctk.CTkFont(size=12)).pack(fill="x")
            ctk.CTkEntry(cell, textvariable=var).pack(fill="x")

    def _build_config(self, left) -> None:
        # ---------------- planilha
        self._section(left, "1. Planilha")
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=(2, 0))
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(row, textvariable=self.file_var, state="readonly").grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(row, text="Selecionar…", width=96, command=self._choose_file).grid(row=0, column=1)

        self._label(left, "Aba")
        self.sheet_menu = ctk.CTkOptionMenu(left, values=["—"], variable=self.sheet_var, command=self._on_sheet,
                                            dynamic_resizing=False)
        self.sheet_menu.pack(fill="x", padx=8)
        self._label(left, "Coluna de pesquisa")
        self.column_menu = ctk.CTkOptionMenu(left, values=["—"], variable=self.column_var, command=self._on_column,
                                             dynamic_resizing=False)
        self.column_menu.pack(fill="x", padx=8)

        self._field_row(left, [("Do registro nº", self.start_var), ("Até o registro nº", self.end_var)])
        ctk.CTkLabel(left, text="Em branco = todos os registros", anchor="w", font=ctk.CTkFont(size=11),
                     text_color=("gray40", "gray60")).pack(fill="x", padx=8)
        self._field_row(left, [("Completar zeros até (dígitos)", self.zero_pad_var)])
        self._label(left, "Validação dos identificadores")
        ctk.CTkOptionMenu(left, values=list(VALIDATIONS), variable=self.validation_var,
                          command=lambda _: self._update_import_summary()).pack(fill="x", padx=8)
        self.import_info = ctk.CTkLabel(left, text="Nenhuma planilha carregada.", anchor="w", justify="left",
                                        wraplength=300, text_color=("gray30", "gray70"))
        self.import_info.pack(fill="x", padx=8, pady=(8, 0))
        ctk.CTkButton(left, text="Ver lista de processamento", fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self._show_preview).pack(fill="x", padx=8, pady=(6, 0))
        for var in (self.start_var, self.end_var, self.zero_pad_var):
            var.trace_add("write", lambda *_: self._update_import_summary())

        # ---------------- site
        self._section(left, "2. Site / adaptador")
        self.adapter_menu = ctk.CTkOptionMenu(left, values=list(self.adapter_by_name) or ["—"], variable=self.adapter_var,
                                              command=lambda _: self._on_adapter(), dynamic_resizing=False)
        self.adapter_menu.pack(fill="x", padx=8)
        self.adapter_desc = ctk.CTkLabel(left, text="", anchor="w", justify="left", wraplength=300,
                                         font=ctk.CTkFont(size=11), text_color=("gray35", "gray65"))
        self.adapter_desc.pack(fill="x", padx=8, pady=(4, 0))

        # ---------------- execução
        self._section(left, "3. Execução")
        self._label(left, "Pasta de destino")
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=8)
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(row, textvariable=self.output_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(row, text="Selecionar…", width=96, command=self._choose_output).grid(row=0, column=1)
        self._field_row(left, [("Intervalo entre consultas (s)", self.interval_var), ("Timeout (s)", self.timeout_var)])
        self._field_row(left, [("Novas tentativas (erro temporário)", self.retries_var),
                               ("Máx. consultas/minuto (0 = livre)", self.per_minute_var)])
        self._label(left, "Navegador")
        ctk.CTkOptionMenu(left, values=list(CHANNELS), variable=self.channel_var, dynamic_resizing=False).pack(fill="x", padx=8)
        ctk.CTkSwitch(left, text="Mostrar navegador (necessário para login/CAPTCHA)", variable=self.visible_var,
                      onvalue=True, offvalue=False).pack(fill="x", padx=8, pady=(10, 0))
        ctk.CTkSwitch(left, text="Lembrar login (sessão protegida)", variable=self.persist_var,
                      onvalue=True, offvalue=False).pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkButton(left, text="Salvar configuração para agendamento…", fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"), command=self._save_job_config).pack(fill="x", padx=8, pady=(14, 0))

        self._section(left, "Aparência")
        ctk.CTkSegmentedButton(left, values=list(THEMES), variable=self.theme_var,
                               command=self._on_theme).pack(fill="x", padx=8, pady=(0, 12))

    def _build_main(self, right) -> None:
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(5, weight=1)

        header = ctk.CTkFrame(right, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(header, text="Web Search Bot", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")
        ctk.CTkLabel(header, text="  robô de pesquisas e extração de dados", text_color=("gray40", "gray60")).pack(side="left", pady=(6, 0))

        bar = ctk.CTkFrame(right)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        big = ctk.CTkFont(size=13, weight="bold")
        self.btn_start = ctk.CTkButton(bar, text="▶ INICIAR", font=big, width=108, height=38, fg_color=GREEN,
                                       hover_color=GREEN_HOVER, command=self._on_start)
        self.btn_pause = ctk.CTkButton(bar, text="❚❚ PAUSAR", font=big, width=104, height=38, command=self._on_pause)
        self.btn_resume = ctk.CTkButton(bar, text="▶ CONTINUAR", font=big, width=122, height=38, command=self._on_resume)
        self.btn_stop = ctk.CTkButton(bar, text="■ PARAR", font=big, width=94, height=38, fg_color=RED,
                                      hover_color=RED_HOVER, command=self._on_stop)
        self.btn_export = ctk.CTkButton(bar, text="⤓ EXPORTAR EXCEL", font=big, width=150, height=38,
                                        command=self._on_export)
        for i, button in enumerate((self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop, self.btn_export)):
            button.grid(row=0, column=i, padx=(8 if i == 0 else 3, 3), pady=8)
        bar.grid_columnconfigure(5, weight=1)
        ctk.CTkOptionMenu(bar, values=list(EXPORT_LABELS), variable=self.export_filter_var, width=170,
                          dynamic_resizing=False).grid(row=0, column=5, padx=3, sticky="ew")
        self.btn_open = ctk.CTkButton(bar, text="Pasta", width=58, height=38, fg_color="transparent",
                                      border_width=1, text_color=("gray10", "gray90"), command=self._open_output_folder)
        self.btn_open.grid(row=0, column=6, padx=(3, 8))

        self.banner = ctk.CTkLabel(right, text="", fg_color=ORANGE, text_color="white", corner_radius=8,
                                   font=ctk.CTkFont(size=14, weight="bold"), wraplength=900, justify="left")
        self.banner.grid(row=2, column=0, sticky="ew", pady=(10, 0), ipady=8)
        self.banner.grid_remove()

        cards = ctk.CTkFrame(right, fg_color="transparent")
        cards.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        specs = [
            ("total", "Total de registros", None),
            ("found", "Concluídas (encontradas)", ("#2e7d32", "#66bb6a")),
            ("pending", "Pendentes", None),
            ("not_found", "Não encontrados", ("#b26a00", "#ffb74d")),
            ("error", "Com erro", ("#c62828", "#ef5350")),
            ("percent", "Conclusão", ("#1565c0", "#64b5f6")),
            ("elapsed", "Tempo decorrido", None),
            ("eta", "Tempo restante (estim.)", None),
        ]
        self.cards: dict[str, StatCard] = {}
        for i, (key, title, accent) in enumerate(specs):
            cards.grid_columnconfigure(i % 4, weight=1)
            card = StatCard(cards, title, "--" if key in ("elapsed", "eta") else "0", accent)
            card.grid(row=i // 4, column=i % 4, sticky="ew", padx=4, pady=4)
            self.cards[key] = card
        self.cards["percent"].set("0%")
        self.cards["elapsed"].set(format_duration(0))
        self.cards["eta"].set(format_duration(None))

        status = ctk.CTkFrame(right, fg_color="transparent")
        status.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        status.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(status, height=14)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=4)
        self.status_label = ctk.CTkLabel(status, text="Pronto. Selecione a planilha, a coluna e o site e clique em INICIAR.",
                                         anchor="w")
        self.status_label.grid(row=1, column=0, sticky="ew", padx=4, pady=(4, 0))

        tabs = ctk.CTkTabview(right)
        tabs.grid(row=5, column=0, sticky="nsew", pady=(6, 0))
        logs_tab = tabs.add("Logs")
        history_tab = tabs.add("Histórico de execuções")
        logs_tab.grid_columnconfigure(0, weight=1)
        logs_tab.grid_rowconfigure(0, weight=1)
        self.log_view = LogView(logs_tab)
        self.log_view.grid(row=0, column=0, sticky="nsew")
        ctk.CTkButton(logs_tab, text="Limpar", width=80, command=self.log_view.clear).grid(row=1, column=0, sticky="e", pady=(4, 0))

        history_tab.grid_columnconfigure(0, weight=1)
        history_tab.grid_rowconfigure(0, weight=1)
        self.history = Table(history_tab, [
            ("id", "Nº", 50), ("created", "Criada em", 140), ("status", "Situação", 130), ("adapter", "Site", 130),
            ("source", "Planilha [aba/coluna]", 260), ("progress", "Progresso", 110), ("found", "Encontr.", 70),
            ("not_found", "Sem result.", 80), ("error", "Erros", 60), ("pending", "Pend.", 60),
        ], height=8)
        self.history.grid(row=0, column=0, sticky="nsew")
        actions = ctk.CTkFrame(history_tab, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        for text, command in (("Atualizar", self._refresh_history), ("Retomar selecionada", self._resume_selected),
                              ("Repetir erros", self._retry_errors_selected), ("Exportar selecionada", self._export_selected),
                              ("Abrir planilha de saída", self._open_selected_output)):
            ctk.CTkButton(actions, text=text, width=150, command=command).pack(side="left", padx=(0, 6))
        self.history.tree.bind("<Double-1>", lambda _e: self._show_run_details())

    # =========================================================== planilha
    def _choose_file(self) -> None:
        initial = Path(self.file_var.get()).parent if self.file_var.get() else paths.INPUT_DIR
        path = filedialog.askopenfilename(title="Selecione a planilha", initialdir=str(initial),
                                          filetypes=[("Planilhas Excel", "*.xlsx *.xlsm"), ("Todos os arquivos", "*.*")])
        if path:
            self._load_workbook(path)

    def _load_workbook(self, path: str, sheet: str | None = None, column: str | None = None) -> None:
        self.file_var.set(path)
        self.import_info.configure(text="Lendo a planilha…")

        def loaded(infos) -> None:
            self.sheet_infos = {info.name: info for info in infos}
            names = list(self.sheet_infos) or ["—"]
            self.sheet_menu.configure(values=names)
            chosen = sheet if sheet in self.sheet_infos else names[0]
            self.sheet_var.set(chosen)
            self._on_sheet(chosen, column)

        self._background(lambda: inspect_workbook(path), loaded, self._import_failed)

    def _import_failed(self, exc: BaseException) -> None:
        self.import_info.configure(text=f"Erro: {exc}")
        messagebox.showerror("Planilha", str(exc), parent=self)

    def _on_sheet(self, sheet: str, column: str | None = None) -> None:
        if sheet not in self.sheet_infos:
            return
        self.import_info.configure(text="Carregando aba…")
        path = self.file_var.get()

        def loaded(result) -> None:
            frame, info, _blank = result
            self.frame = frame
            columns = [str(c) for c in info.columns] or ["—"]
            self.column_menu.configure(values=columns)
            if column in columns:
                chosen = column
            else:  # sugere a primeira coluna com "doc", "cnpj", "cpf", "protocolo" ou "codigo" no nome
                hints = ("documento", "cnpj", "cpf", "protocolo", "codigo", "código", "identificador")
                chosen = next((c for c in columns if any(h in c.lower() for h in hints)), columns[0])
            self.column_var.set(chosen)
            self._update_import_summary()

        self._background(lambda: read_sheet(path, sheet), loaded, self._import_failed)

    def _on_column(self, _value: str) -> None:
        self._update_import_summary()

    def _int_or_none(self, var: ctk.StringVar, name: str) -> int | None:
        text = var.get().strip()
        if not text:
            return None
        if not text.isdigit():
            raise ConfigError(f"'{name}' deve ser um número inteiro.")
        return int(text)

    def _current_records(self):
        if self.frame is None or self.column_var.get() not in self.frame.columns:
            return None
        return build_processing_list(
            self.frame, self.column_var.get(),
            start=self._int_or_none(self.start_var, "Do registro"),
            end=self._int_or_none(self.end_var, "Até o registro"),
            zero_pad=self._int_or_none(self.zero_pad_var, "Completar zeros"),
            validation=VALIDATIONS[self.validation_var.get()],
        )

    def _update_import_summary(self) -> None:
        try:
            records = self._current_records()
        except ConfigError as exc:
            self.import_info.configure(text=f"⚠ {exc}")
            return
        if records is None:
            return
        pending = sum(r.status == ItemStatus.PENDING for r in records)
        dup = sum(r.status == ItemStatus.DUPLICATE for r in records)
        invalid = sum(r.status == ItemStatus.INVALID for r in records)
        self.import_info.configure(
            text=(f"Aba com {len(self.frame)} registro(s). Selecionados: {len(records)}\n"
                  f"• Para consultar: {pending}\n• Duplicados (não consultados): {dup}\n"
                  f"• Vazios/inválidos: {invalid}"))

    def _show_preview(self) -> None:
        try:
            records = self._current_records()
        except ConfigError as exc:
            messagebox.showwarning("Lista de processamento", str(exc), parent=self)
            return
        if records is None:
            messagebox.showinfo("Lista de processamento", "Selecione a planilha e a coluna primeiro.", parent=self)
            return
        top = ctk.CTkToplevel(self)
        top.title("Lista de processamento")
        top.geometry("820x520")
        top.transient(self)
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(top, text=f"{len(records)} registro(s) — mostrando até 2000", anchor="w").grid(
            row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        table = Table(top, [("pos", "Nº", 60), ("row", "Linha", 60), ("id", "Identificador", 260),
                            ("status", "Situação", 120), ("msg", "Observação", 280)], height=18)
        table.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        table.set_rows((str(i), (r.position, r.row_number, r.identifier,
                                 "Para consultar" if r.status == ItemStatus.PENDING else r.status.label, r.message))
                       for i, r in enumerate(records[:2000]))
        top.after(150, top.lift)

    # =========================================================== site/execução
    def _adapter_id(self) -> str:
        name = self.adapter_var.get()
        if name not in self.adapter_by_name:
            raise ConfigError("Selecione o site/adaptador.")
        return self.adapter_by_name[name]

    def _on_adapter(self) -> None:
        try:
            info = self.registry[self._adapter_id()]
        except ConfigError:
            return
        login = " Exige login manual do operador." if info.cls(None).requires_login else ""
        kind = "API" if info.kind == "api" else "navegador"
        self.adapter_desc.configure(text=f"[{kind}] {info.description}{login}")

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="Pasta de destino", initialdir=self.output_var.get() or str(paths.OUTPUT_DIR))
        if path:
            self.output_var.set(path)

    def _number(self, var: ctk.StringVar, name: str, kind=float):
        try:
            return kind(var.get().strip().replace(",", "."))
        except ValueError as exc:
            raise ConfigError(f"'{name}' deve ser numérico.") from exc

    def _run_settings(self) -> RunSettings:
        return RunSettings(
            adapter_id=self._adapter_id(),
            interval_seconds=self._number(self.interval_var, "Intervalo entre consultas"),
            timeout_seconds=self._number(self.timeout_var, "Timeout"),
            max_retries=self._number(self.retries_var, "Novas tentativas", int),
            max_per_minute=self._number(self.per_minute_var, "Máx. consultas/minuto", int),
            headless=not self.visible_var.get(),
            browser_channel=CHANNELS[self.channel_var.get()],
            persist_session=self.persist_var.get(),
            output_dir=self.output_var.get().strip(),
        ).validate()

    def _import_options(self) -> ImportOptions:
        if not self.file_var.get():
            raise ConfigError("Selecione a planilha de entrada.")
        if self.column_var.get() in ("", "—"):
            raise ConfigError("Selecione a coluna de pesquisa.")
        return ImportOptions(
            file=self.file_var.get(),
            sheet=self.sheet_var.get(),
            column=self.column_var.get(),
            start=self._int_or_none(self.start_var, "Do registro"),
            end=self._int_or_none(self.end_var, "Até o registro"),
            zero_pad=self._int_or_none(self.zero_pad_var, "Completar zeros"),
            validation=VALIDATIONS[self.validation_var.get()],
        ).validate()

    def _apply_execution_settings(self, run_id: int, keep_adapter: bool = True) -> None:
        """Ao retomar, aplica intervalo/timeout/navegador atuais mantendo o site da execução."""
        run = self.repo.get_run(run_id)
        current = self._run_settings()
        settings = run.settings
        for name in ("interval_seconds", "timeout_seconds", "max_retries", "max_per_minute", "headless",
                     "browser_channel", "persist_session"):
            setattr(settings, name, getattr(current, name))
        if not keep_adapter:
            settings.adapter_id = current.adapter_id
        self.repo.update_run(run_id, settings=settings)

    # =========================================================== ações principais
    def _on_start(self) -> None:
        if self.engine is not None or self._busy:
            return
        try:
            options = self._import_options()
            settings = self._run_settings()
        except ConfigError as exc:
            messagebox.showwarning("Configuração", str(exc), parent=self)
            return
        source = str(resolve_path(options.file))
        existing = self.repo.find_resumable_run(source, options.sheet or "", options.column, settings.adapter_id)
        if existing:
            stats = self.repo.stats(existing.id)
            answer = messagebox.askyesnocancel(
                "Execução incompleta encontrada",
                f"A execução nº {existing.id} desta planilha/coluna/site parou com {stats.remaining} de "
                f"{stats.total} registro(s) pendente(s).\n\n"
                "Sim — continuar de onde parou (não repete os já concluídos)\n"
                "Não — criar uma nova execução (consulta todos novamente)\n"
                "Cancelar — voltar", parent=self)
            if answer is None:
                return
            if answer:
                self._apply_execution_settings(existing.id)
                self._launch(existing.id)
                return
        self._set_busy(True, "Importando a planilha…")

        def imported(result: ImportResult) -> None:
            self._set_busy(False)
            if result.to_process == 0:
                messagebox.showwarning("Planilha", "Nenhum registro válido para consultar.\n" + result.summary(), parent=self)
                return
            name = f"{Path(result.file).name} [{result.sheet}/{result.column}]"
            self._launch(create_run(self.db, name, result, settings))

        def failed(exc: BaseException) -> None:
            self._set_busy(False)
            self._import_failed(exc)

        self._background(lambda: import_excel(options), imported, failed)

    def _launch(self, run_id: int) -> None:
        self.current_run_id = run_id
        self.last_output = None
        self.log_view.append("INFO", f"──────── Execução nº {run_id} ────────")
        self.engine = ProcessingEngine(self.db, run_id, interactive=True, on_event=self.ui_queue.put)
        self.engine_state = EngineState.STARTING
        self.engine.start()
        self._refresh_buttons()
        self._refresh_history()

    def _on_pause(self) -> None:
        if self.engine:
            self.engine.pause()
            self.status_label.configure(text="Pausando após a consulta atual…")

    def _on_resume(self) -> None:
        if self.engine:
            self.engine.resume()
            self._hide_banner()

    def _on_stop(self) -> None:
        if self.engine and messagebox.askyesno(
                "Parar execução", "Parar a execução? O progresso fica salvo e pode ser retomado depois.", parent=self):
            self.engine.stop()
            self._hide_banner()
            self.status_label.configure(text="Parando com segurança…")

    def _export_run(self, run_id: int | None) -> None:
        if run_id is None:
            messagebox.showinfo("Exportar", "Nenhuma execução selecionada.", parent=self)
            return
        export_filter = EXPORT_LABELS[self.export_filter_var.get()]

        def done(path: Path) -> None:
            self.last_output = str(path)
            log.info("Planilha exportada: %s", path)
            if messagebox.askyesno("Exportar Excel", f"Planilha gerada:\n{path}\n\nDeseja abri-la agora?", parent=self):
                self._open_path(path)

        self._background(lambda: export(self.db, run_id, export_filter), done, self._show_error)

    def _on_export(self) -> None:
        self._export_run(self.current_run_id or self._selected_run_id())

    # =========================================================== histórico
    def _selected_run_id(self) -> int | None:
        selected = self.history.selected()
        return int(selected) if selected else None

    def _refresh_history(self) -> None:
        rows = []
        for run in self.repo.list_runs(200):
            s = self.repo.stats(run.id)
            rows.append((str(run.id), (
                run.id, run.created_at.replace("T", " "), run.status.label, run.adapter_id,
                f"{Path(run.source_file).name} [{run.sheet}/{run.search_column}]",
                f"{s.done}/{s.total} ({s.percent:.0f}%)", s.found, s.not_found, s.error, s.remaining,
            )))
        self.history.set_rows(rows)

    def _resume_selected(self) -> None:
        run_id = self._selected_run_id()
        if run_id is None or self.engine is not None:
            return
        run = self.repo.get_run(run_id)
        stats = self.repo.stats(run_id)
        if stats.remaining == 0:
            messagebox.showinfo("Retomar", "Esta execução não tem registros pendentes. Use 'Repetir erros' "
                                           "para reconsultar os que falharam.", parent=self)
            return
        try:
            self._apply_execution_settings(run_id)
        except ConfigError as exc:
            messagebox.showwarning("Configuração", str(exc), parent=self)
            return
        log.info("Retomando execução %d (%s).", run_id, run.status.label)
        self._launch(run_id)

    def _retry_errors_selected(self) -> None:
        run_id = self._selected_run_id() or self.current_run_id
        if run_id is None or self.engine is not None:
            return
        errors = self.repo.stats(run_id).error
        if not errors:
            messagebox.showinfo("Repetir erros", "Esta execução não tem registros com erro.", parent=self)
            return
        if messagebox.askyesno("Repetir erros", f"Recolocar {errors} registro(s) com erro na fila e processar agora?",
                               parent=self):
            self.repo.reset_items(run_id, [ItemStatus.ERROR])
            self.repo.add_event(run_id, "retry_errors", f"{errors} registro(s) recolocados na fila")
            self._apply_execution_settings(run_id)
            self._launch(run_id)

    def _export_selected(self) -> None:
        self._export_run(self._selected_run_id())

    def _open_selected_output(self) -> None:
        run_id = self._selected_run_id() or self.current_run_id
        if run_id is None:
            return
        output = self.repo.get_run(run_id).output_file
        if output and Path(output).exists():
            self._open_path(Path(output))
        else:
            messagebox.showinfo("Planilha de saída", "Ainda não há planilha gerada para esta execução. "
                                                     "Use 'Exportar selecionada'.", parent=self)

    def _show_run_details(self) -> None:
        run_id = self._selected_run_id()
        if run_id is None:
            return
        run = self.repo.get_run(run_id)
        s = self.repo.stats(run_id)
        lines = [f"Execução nº {run.id} — {run.name}", f"Situação: {run.status.label}. {run.message or ''}",
                 f"Site: {run.adapter_id} | Planilha: {run.source_file}",
                 f"Total {s.total} | encontrados {s.found} | sem resultado {s.not_found} | erros {s.error} | "
                 f"pendentes {s.remaining} | duplicados {s.duplicate} | inválidos {s.invalid}",
                 f"Saída: {run.output_file or '-'}", "", "Eventos recentes:"]
        lines += [f"  {e['created_at'].replace('T', ' ')}  {e['kind']}  {e['message'] or ''}" for e in self.repo.events(run_id)[-15:]]
        errors = self.repo.items(run_id, [ItemStatus.ERROR])[:15]
        if errors:
            lines += ["", "Registros com erro:"] + [f"  linha {i.row_number}: {i.identifier} — {i.message}" for i in errors]
        messagebox.showinfo(f"Execução nº {run_id}", "\n".join(lines), parent=self)

    # =========================================================== eventos do motor
    def _handle_event(self, event: EngineEvent) -> None:
        data = event.data
        if event.kind == "state":
            self.engine_state = data["state"]
            if self.engine_state != EngineState.WAITING_OPERATOR:
                self._hide_banner()
            labels = {EngineState.STARTING: "Iniciando (abrindo navegador/site)…", EngineState.PAUSED: "Pausado. Clique em CONTINUAR.",
                      EngineState.STOPPING: "Parando com segurança…"}
            if self.engine_state in labels:
                self.status_label.configure(text=f"Execução nº {self.current_run_id}: {labels[self.engine_state]}")
            self._refresh_buttons()
        elif event.kind == "operator":
            self._show_banner(f"⚠ AÇÃO NECESSÁRIA: {data['message']}")
            self.bell()
        elif event.kind == "item_started":
            self.status_label.configure(
                text=f"Execução nº {self.current_run_id}: consultando registro {data['position']} "
                     f"(linha {data['row_number']}): {data['identifier']}")
        elif event.kind == "progress":
            self._show_stats(data["stats"], data.get("eta"))
        elif event.kind == "exported":
            self.last_output = data["path"]
        elif event.kind == "finished":
            self._on_finished(data)

    def _show_stats(self, stats: RunStats, eta: float | None) -> None:
        self.cards["total"].set(stats.total)
        self.cards["found"].set(stats.found)
        self.cards["pending"].set(stats.remaining)
        self.cards["not_found"].set(stats.not_found)
        self.cards["error"].set(stats.error)
        self.cards["percent"].set(f"{stats.percent:.1f}%")
        self.progress.set(stats.percent / 100)
        if stats.remaining == 0:
            self.cards["eta"].set(format_duration(0))

    def _on_finished(self, data: dict[str, Any]) -> None:
        engine = self.engine
        self.engine = None
        self.engine_state = EngineState.IDLE
        self._hide_banner()
        self._refresh_buttons()
        self._refresh_history()
        if engine:
            self.cards["elapsed"].set(format_duration(engine.elapsed()))
        status: RunStatus | None = data.get("run_status")
        message = data.get("message") or ""
        output = data.get("output")
        self.status_label.configure(text=f"Execução nº {self.current_run_id}: {status.label if status else 'não iniciada'}. {message}")
        if self._closing:
            return
        if status is None:
            messagebox.showerror("Execução", message, parent=self)
        elif status == RunStatus.COMPLETED and output:
            if messagebox.askyesno("Execução concluída", f"{message}\n\nPlanilha: {output}\n\nAbrir a planilha agora?", parent=self):
                self._open_path(Path(output))
        elif status in (RunStatus.NEEDS_OPERATOR, RunStatus.FAILED):
            messagebox.showwarning("Execução interrompida", f"{message}\n\nO progresso foi salvo; retome pelo histórico "
                                                            "ou clique em INICIAR novamente.", parent=self)

    # =========================================================== laços de atualização
    def _poll(self) -> None:
        try:
            for _ in range(200):
                try:
                    item = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    if isinstance(item, EngineEvent):
                        self._handle_event(item)
                    else:
                        func, arg = item
                        func(arg)
                except Exception as exc:  # erro de interface não pode parar o laço
                    log.exception("Erro na interface: %s", exc)
            for _ in range(300):
                try:
                    level, text = self.log_queue.get_nowait()
                except queue.Empty:
                    break
                self.log_view.append(level, text)
        finally:
            self._schedule(100, self._poll)

    def _tick(self) -> None:
        if self.engine is not None:
            self.cards["elapsed"].set(format_duration(self.engine.elapsed()))
            eta = self.engine.eta()
            if eta is not None:
                self.cards["eta"].set(format_duration(eta))
        self._schedule(1000, self._tick)

    def _background(self, func: Callable[[], Any], on_done: Callable[[Any], None],
                    on_error: Callable[[BaseException], None] | None = None) -> None:
        def worker() -> None:
            try:
                result = func()
            except Exception as exc:
                self.ui_queue.put((on_error or self._show_error, exc))
            else:
                self.ui_queue.put((on_done, result))

        threading.Thread(target=worker, daemon=True).start()

    # =========================================================== utilidades
    def _refresh_buttons(self) -> None:
        state = self.engine_state if self.engine else EngineState.IDLE
        idle = self.engine is None and not self._busy
        running = state in (EngineState.RUNNING, EngineState.STARTING)
        waiting = state in (EngineState.PAUSED, EngineState.WAITING_OPERATOR)
        for button, enabled, color in ((self.btn_start, idle, GREEN), (self.btn_pause, running, None),
                                       (self.btn_resume, waiting, None), (self.btn_stop, running or waiting, RED)):
            # botões desativados ficam cinza: o CustomTkinter quase não esmaece botões coloridos
            fg = (color or ctk.ThemeManager.theme["CTkButton"]["fg_color"]) if enabled else DISABLED
            button.configure(state="normal" if enabled else "disabled", fg_color=fg)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        if message:
            self.status_label.configure(text=message)
        self._refresh_buttons()

    def _show_banner(self, text: str) -> None:
        self.banner.configure(text=text)
        self.banner.grid()

    def _hide_banner(self) -> None:
        self.banner.grid_remove()

    def _show_error(self, exc: BaseException) -> None:
        log.error("%s", exc)
        messagebox.showerror("Erro", str(exc), parent=self)

    def _open_path(self, path: Path) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except (AttributeError, OSError) as exc:
            messagebox.showinfo("Abrir", f"Arquivo em: {path}\n({exc})", parent=self)

    def _open_output_folder(self) -> None:
        folder = Path(self.last_output).parent if self.last_output else resolve_path(self.output_var.get() or paths.OUTPUT_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        self._open_path(folder)

    def _on_theme(self, value: str) -> None:
        ctk.set_appearance_mode(THEMES[value])
        style_treeview(self)
        self.log_view.refresh_theme()

    def _save_job_config(self) -> None:
        try:
            options = self._import_options()
            settings = self._run_settings()
        except ConfigError as exc:
            messagebox.showwarning("Configuração", str(exc), parent=self)
            return
        if not settings.headless:
            settings.headless = messagebox.askyesno(
                "Agendamento", "No modo agendado o navegador normalmente fica oculto.\n"
                               "Salvar com o navegador oculto? (Não = manter visível)", parent=self)
        path =filedialog.asksaveasfilename(title="Salvar configuração", initialdir=str(paths.CONFIG_DIR),
                                            defaultextension=".json", filetypes=[("Configuração JSON", "*.json")],
                                            initialfile="consulta_agendada.json")
        if not path:
            return
        job = JobConfig(name=Path(path).stem, input=options, settings=settings)
        save_job_config(job, path)
        log.info("Configuração salva em %s", path)
        messagebox.showinfo("Configuração salva",
                            f"Arquivo: {path}\n\nExecutar sem interface:\n  cli.py run --config \"{path}\"\n\n"
                            f"Agendar no Windows (diariamente às 08:00):\n  cli.py schedule --config \"{path}\" "
                            f"--name {Path(path).stem} --time 08:00 --apply", parent=self)

    # =========================================================== estado da tela
    def _load_ui_state(self) -> None:
        self._on_adapter()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for key, var in self._persisted_vars().items():
            if key in state and state[key] is not None:
                var.set(state[key])
        if self.adapter_var.get() not in self.adapter_by_name:
            self.adapter_var.set(next(iter(self.adapter_by_name), ""))
        self._on_adapter()
        self._on_theme(self.theme_var.get() if self.theme_var.get() in THEMES else "Sistema")
        if state.get("file") and Path(state["file"]).exists():
            self._load_workbook(state["file"], state.get("sheet"), state.get("column"))
        else:
            self.file_var.set("")

    def _persisted_vars(self) -> dict[str, Any]:
        return {"file": self.file_var, "start": self.start_var, "end": self.end_var, "zero_pad": self.zero_pad_var,
                "validation": self.validation_var, "adapter": self.adapter_var, "output": self.output_var,
                "interval": self.interval_var, "timeout": self.timeout_var, "retries": self.retries_var,
                "per_minute": self.per_minute_var, "channel": self.channel_var, "visible": self.visible_var,
                "persist": self.persist_var, "export_filter": self.export_filter_var, "theme": self.theme_var}

    def _save_ui_state(self) -> None:
        state = {key: var.get() for key, var in self._persisted_vars().items()}
        state.update(sheet=self.sheet_var.get(), column=self.column_var.get())
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            log.debug("Não foi possível salvar o estado da tela", exc_info=True)

    def _on_close(self) -> None:
        if self.engine is not None:
            if not messagebox.askyesno("Sair", "Há uma execução em andamento. Parar com segurança e sair?", parent=self):
                return
            self.engine.stop()
            self.status_label.configure(text="Encerrando: aguardando a consulta atual terminar…")
            self._wait_engine_then_close(0)
            return
        self._shutdown()

    def _wait_engine_then_close(self, waited: int) -> None:
        engine = self.engine
        if engine is None or engine.join(0) or waited > 900:  # até ~90 s
            self._shutdown()
            return
        self.after(100, lambda: self._wait_engine_then_close(waited + 1))

    def _shutdown(self) -> None:
        self._closing = True
        self._save_ui_state()
        for after_id in self._after_ids.values():
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        import logging

        logging.getLogger("wsb").removeHandler(self._log_handler)
        self.repo.close()
        self.destroy()


def run_app() -> None:
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    app = WebSearchBotApp()
    app.mainloop()
