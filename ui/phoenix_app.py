"""PHOENIX BOT — Consulta de Portabilidade TIM (interface CustomTkinter).

O navegador e o lote rodam na thread ``PhoenixWorker``; a janela recebe eventos por fila e nunca
congela. Cálculos pesados (leitura da planilha, indicadores, exportação) rodam em segundo plano.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

import customtkinter as ctk

from core import paths
from core.engine import EngineEvent, EngineState
from core.models import ConfigError, RunStatus
from database.repository import Repository
from phoenix import jobs
from phoenix.config import PhoenixSettings, SheetMapping, load_mappings, load_settings, save_mappings, save_settings
from phoenix.normalize import normalize_cnpj
from phoenix.repository import PhoenixRepository
from phoenix.worker import PhoenixWorker, WorkerEvent
from phoenix.workbook import SheetSummary, detect_mappings, list_sheets, load_sheets
from ui.components import LogView, StatCard, Table, format_duration
from utils.logger import attach_queue_handler, get_logger

log = get_logger("ui.phoenix")

GREEN, RED, DISABLED = ("#2e7d32", "#2e7d32"), ("#c62828", "#b71c1c"), ("#a8adb3", "#4a4d50")
ORANGE = ("#ef6c00", "#e65100")
CHANNELS = {"Microsoft Edge (Chromium)": "msedge", "Google Chrome": "chrome", "Chromium do Playwright": "chromium"}
MULTI = {"Usar o bilhete mais recente (se a data comprovar)": "mais_recente", "Sempre enviar para revisão": "revisar"}
NONE_FILA, NONE_COL = "(criar só na exportação)", "(nenhuma)"


class PhoenixApp(ctk.CTk):
    def __init__(self, db=None):
        super().__init__()
        paths.ensure_dirs()
        self.db = db or jobs.open_database()
        jobs.mark_stale_runs(self.db)
        self.settings = load_settings()
        self.mappings: dict[str, SheetMapping] = load_mappings()
        self.summaries: dict[str, SheetSummary] = {}
        self.sheet_vars: dict[str, ctk.BooleanVar] = {}
        self.current_run: int | None = None
        self.engine_state = EngineState.IDLE
        self.running = False
        self._stats_busy = False
        self._last_stats = 0.0
        self.events: "queue.Queue[Any]" = queue.Queue()
        self.logs: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._log_handler = attach_queue_handler(self.logs)
        self.worker = PhoenixWorker(self.db, self.events.put, lambda: self.settings)
        self.worker.start()
        self._closing = False

        self.title("PHOENIX BOT — Consulta de Portabilidade TIM")
        self._fit()
        self._vars()
        self._build()
        self._refresh_buttons()
        self._refresh_history()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.after(1000, self._tick)
        log.info("PHOENIX BOT pronto. Use 'Abrir navegador' para fazer o login manual no Phoenix2.")

    # ================================================================ montagem
    def _fit(self) -> None:
        scale = getattr(self, "_get_window_scaling", lambda: 1.0)() or 1.0
        sw, sh = int(self.winfo_screenwidth() / scale), int(self.winfo_screenheight() / scale)
        w, h = min(1440, sw - 40), min(920, sh - 90)
        self.minsize(min(1040, sw - 40), min(640, sh - 90))
        self.geometry(f"{w}x{h}+{max((sw - w) // 2, 0)}+{max((sh - h) // 2 - 20, 0)}")
        if sw < 1500 or sh < 950:
            self.after(50, lambda: self.state("zoomed"))

    def _vars(self) -> None:
        s = self.settings
        self.file_var = ctk.StringVar()
        self.output_var = ctk.StringVar(value=s.output_dir)
        self.interval_var = ctk.StringVar(value=str(s.interval_seconds))
        self.timeout_var = ctk.StringVar(value=str(s.timeout_seconds))
        self.retries_var = ctk.StringVar(value=str(s.max_retries))
        self.channel_var = ctk.StringVar(value=next((k for k, v in CHANNELS.items() if v == s.browser_channel), next(iter(CHANNELS))))
        self.multi_var = ctk.StringVar(value=next(k for k, v in MULTI.items() if v == s.match.multiplos_bilhetes))
        self.temp_var = ctk.BooleanVar(value=s.match.usar_numero_temporario)
        self.ddi_var = ctk.BooleanVar(value=s.match.strip_ddi_55)
        self.falha_var = ctk.StringVar(value="; ".join(s.status.falha_parcial_portada))
        self.persist_var = ctk.BooleanVar(value=s.persist_session)

    def _label(self, parent, text, **kw) -> ctk.CTkLabel:
        label = ctk.CTkLabel(parent, text=text, anchor="w", justify="left", **kw)
        label.pack(fill="x", padx=8, pady=(6, 0))
        return label

    def _section(self, parent, text) -> None:
        ctk.CTkLabel(parent, text=text, anchor="w", font=ctk.CTkFont(size=14, weight="bold")).pack(fill="x", padx=8, pady=(14, 2))

    def _entry_row(self, parent, pairs) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=(4, 0))
        for i, (text, var) in enumerate(pairs):
            row.grid_columnconfigure(i, weight=1)
            cell = ctk.CTkFrame(row, fg_color="transparent")
            cell.grid(row=0, column=i, sticky="ew", padx=(0, 6))
            ctk.CTkLabel(cell, text=text, anchor="w", font=ctk.CTkFont(size=11)).pack(fill="x")
            ctk.CTkEntry(cell, textvariable=var).pack(fill="x")

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        left = ctk.CTkScrollableFrame(self, width=350, label_text="Configuração")
        left.grid(row=0, column=0, sticky="nsw", padx=(12, 6), pady=12)
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)

        self._section(left, "1. Planilha")
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=8)
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(row, textvariable=self.file_var, state="readonly").grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(row, text="Selecionar…", width=92, command=self._choose_file).grid(row=0, column=1)
        self._label(left, "Abas (ocultas ficam desmarcadas; marque se quiser processar):", font=ctk.CTkFont(size=11))
        self.sheets_box = ctk.CTkScrollableFrame(left, height=190)
        self.sheets_box.pack(fill="x", padx=8, pady=(2, 0))
        buttons = ctk.CTkFrame(left, fg_color="transparent")
        buttons.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkButton(buttons, text="Ver/editar mapeamento", command=self._edit_mapping).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(buttons, text="Validar", width=80, command=self._validate).pack(side="left")
        self.validation = self._label(left, "Nenhuma planilha carregada.", wraplength=310, text_color=("gray30", "gray70"))

        self._section(left, "2. Consulta")
        self._label(left, "Pasta de saída")
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=8)
        row.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(row, textvariable=self.output_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(row, text="…", width=36, command=self._choose_output).grid(row=0, column=1)
        self._entry_row(left, [("Intervalo (s)", self.interval_var), ("Timeout (s)", self.timeout_var),
                               ("Tentativas", self.retries_var)])
        self._label(left, "Navegador")
        ctk.CTkOptionMenu(left, values=list(CHANNELS), variable=self.channel_var, dynamic_resizing=False).pack(fill="x", padx=8)
        ctk.CTkSwitch(left, text="Lembrar login (sessão protegida DPAPI)", variable=self.persist_var).pack(fill="x", padx=8, pady=(8, 0))

        self._section(left, "3. Regras")
        self._label(left, "Status de 'Falha parcial / portada' (separe por ;)\nConfirme no Phoenix2 antes de alterar:",
                    font=ctk.CTkFont(size=11))
        ctk.CTkEntry(left, textvariable=self.falha_var).pack(fill="x", padx=8)
        self._label(left, "Mesmo número com vários bilhetes")
        ctk.CTkOptionMenu(left, values=list(MULTI), variable=self.multi_var, dynamic_resizing=False).pack(fill="x", padx=8)
        ctk.CTkSwitch(left, text="Remover DDI 55 (12/13 dígitos)", variable=self.ddi_var).pack(fill="x", padx=8, pady=(8, 0))
        ctk.CTkSwitch(left, text="Usar número temporário (só se inequívoco)", variable=self.temp_var).pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkButton(left, text="Salvar configurações", command=self._save_settings).pack(fill="x", padx=8, pady=(12, 12))

        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(6, weight=1)
        ctk.CTkLabel(right, text="PHOENIX BOT — Consulta de Portabilidade TIM", anchor="w",
                     font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, sticky="ew")
        session = ctk.CTkFrame(right)
        session.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.btn_browser = ctk.CTkButton(session, text="Abrir navegador (login)", command=lambda: self._submit("open_browser"))
        self.btn_test = ctk.CTkButton(session, text="Testar conexão", command=lambda: self._submit("test_connection"))
        self.btn_query = ctk.CTkButton(session, text="Consulta de teste", command=self._test_search)
        self.btn_inspect = ctk.CTkButton(session, text="Inspecionar página", command=self._inspect)
        for i, b in enumerate((self.btn_browser, self.btn_test, self.btn_query, self.btn_inspect)):
            session.grid_columnconfigure(i, weight=1)
            b.grid(row=0, column=i, padx=4, pady=8, sticky="ew")

        bar = ctk.CTkFrame(right)
        bar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        bold = ctk.CTkFont(size=13, weight="bold")
        self.btn_run = ctk.CTkButton(bar, text="▶ EXECUTAR LOTE", font=bold, height=38, command=self._run)
        self.btn_pause = ctk.CTkButton(bar, text="❚❚ PAUSAR", font=bold, height=38, command=self._pause)
        self.btn_resume = ctk.CTkButton(bar, text="▶ CONTINUAR", font=bold, height=38, command=self._continue)
        self.btn_cancel = ctk.CTkButton(bar, text="■ CANCELAR", font=bold, height=38, command=self._cancel)
        self.btn_retake = ctk.CTkButton(bar, text="⟲ RETOMAR", font=bold, height=38, command=self._retake)
        self.btn_export = ctk.CTkButton(bar, text="⤓ EXPORTAR", font=bold, height=38, command=self._export)
        self.btn_folder = ctk.CTkButton(bar, text="Pasta", width=60, height=38, fg_color="transparent", border_width=1,
                                        text_color=("gray10", "gray90"), command=self._open_folder)
        for i, b in enumerate((self.btn_run, self.btn_pause, self.btn_resume, self.btn_cancel, self.btn_retake,
                               self.btn_export, self.btn_folder)):
            bar.grid_columnconfigure(i, weight=0 if b is self.btn_folder else 1)
            b.grid(row=0, column=i, padx=3, pady=8, sticky="ew")

        self.banner = ctk.CTkLabel(right, text="", fg_color=ORANGE, text_color="white", corner_radius=8, wraplength=900,
                                   font=ctk.CTkFont(size=13, weight="bold"))
        self.banner.grid(row=3, column=0, sticky="ew", pady=(8, 0), ipady=6)
        self.banner.grid_remove()

        cards = ctk.CTkFrame(right, fg_color="transparent")
        cards.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        specs = [("linhas", "Total de linhas", None), ("cnpjs", "CNPJs distintos", None), ("pesquisados", "CNPJs pesquisados", None),
                 ("bilhetes", "Bilhetes analisados", None), ("andamento", "Em andamento", ("#2e7d32", "#66bb6a")),
                 ("falha", "Falha parcial / portada", ("#6a1b9a", "#ce93d8")), ("conflito", "Em conflito", ("#b26a00", "#ffb74d")),
                 ("outros", "Outros status ignorados", None), ("sem", "Sem correspondência", None),
                 ("erros", "Erros de consulta", ("#c62828", "#ef5350")), ("percent", "Concluído", ("#1565c0", "#64b5f6")),
                 ("tempo", "Tempo decorrido", None)]
        self.cards: dict[str, StatCard] = {}
        for i, (key, title, accent) in enumerate(specs):
            cards.grid_columnconfigure(i % 6, weight=1)
            card = StatCard(cards, title, "0", accent)
            card.grid(row=i // 6, column=i % 6, sticky="ew", padx=3, pady=3)
            self.cards[key] = card
        self.cards["tempo"].set(format_duration(0))

        status = ctk.CTkFrame(right, fg_color="transparent")
        status.grid(row=5, column=0, sticky="ew", pady=(4, 0))
        status.grid_columnconfigure(0, weight=1)
        self.progress = ctk.CTkProgressBar(status, height=12)
        self.progress.set(0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=3)
        self.status = ctk.CTkLabel(status, text="Pronto.", anchor="w")
        self.status.grid(row=1, column=0, sticky="ew", padx=3)

        tabs = ctk.CTkTabview(right)
        tabs.grid(row=6, column=0, sticky="nsew", pady=(4, 0))
        logs, hist = tabs.add("Logs"), tabs.add("Histórico")
        for tab in (logs, hist):
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)
        self.log_view = LogView(logs)
        self.log_view.grid(row=0, column=0, sticky="nsew")
        self.history = Table(hist, [("id", "Nº", 50), ("created", "Criada em", 140), ("status", "Situação", 130),
                                    ("source", "Planilha / abas", 330), ("progress", "CNPJs", 110), ("errors", "Erros", 60)], height=6)
        self.history.grid(row=0, column=0, sticky="nsew")

    # ================================================================ planilha
    def _choose_file(self) -> None:
        path = filedialog.askopenfilename(title="Planilha da esteira", filetypes=[("Excel", "*.xlsx *.xlsm")], parent=self)
        if path:
            self._load_workbook(path)

    def _load_workbook(self, path: str) -> None:
        self.file_var.set(path)
        self.validation.configure(text="Lendo abas… (planilhas grandes levam alguns segundos)")
        self._background(lambda: list_sheets(path), self._sheets_loaded)

    def _sheets_loaded(self, summaries: list[SheetSummary]) -> None:
        for child in self.sheets_box.winfo_children():
            child.destroy()
        self.summaries = {s.name: s for s in summaries}
        self.sheet_vars = {}
        for s in summaries:
            mapped = s.name in self.mappings
            var = ctk.BooleanVar(value=mapped and not s.hidden)
            label = f"{s.name}  ({s.data_rows} linhas{', oculta' if s.hidden else ''}{', mapeada' if mapped else ''})"
            ctk.CTkCheckBox(self.sheets_box, text=label, variable=var).pack(fill="x", anchor="w", pady=1)
            self.sheet_vars[s.name] = var
        self.validation.configure(text=f"{len(summaries)} aba(s). Selecione as abas e clique em 'Validar'.")

    def _names(self) -> list[str]:
        return [n for n, v in self.sheet_vars.items() if v.get()]

    def _resolve(self, source: str, names: list[str]) -> dict[str, SheetMapping]:
        """Roda em segundo plano: detecta o mapeamento das abas ainda não configuradas (lê a planilha)."""
        missing = [n for n in names if n not in self.mappings]
        if missing:
            self.mappings.update(detect_mappings(source, missing))
        return {n: self.mappings[n] for n in names}

    def _validate(self) -> None:
        source, names = self.file_var.get(), self._names()
        if not source:
            return
        if not names:
            messagebox.showinfo("Validar", "Marque ao menos uma aba.", parent=self)
            return
        self.validation.configure(text="Validando…")
        rules = jobs.phone_rules(self.settings)

        def work():
            return load_sheets(source, self._resolve(source, names), rules)

        def done(sheets) -> None:
            rows = [r for s in sheets for r in s.rows]
            cnpjs = {r.cnpj.value for r in rows if r.cnpj.value}
            invalid = sum(1 for r in rows if r.cnpj.status not in ("valido", "vazio"))
            no_phone = sum(1 for r in rows if not r.acesso)
            lines = [f"{len(rows)} linha(s) em {len(sheets)} aba(s); {len(cnpjs)} CNPJ(s) distinto(s).",
                     f"CNPJ inválido/CPF: {invalid} · sem número de acesso: {no_phone}."]
            for s in sheets:
                for issue in s.issues:
                    lines.append(f"• {s.name}: {issue}")
            self.validation.configure(text="\n".join(lines))
            self.cards["linhas"].set(len(rows))
            self.cards["cnpjs"].set(len(cnpjs))
            log.info("Validação: %s", " ".join(lines[:2]))

        self._background(work, done, lambda e: self.validation.configure(text=f"⚠ {e}"))

    def _edit_mapping(self) -> None:
        source, names = self.file_var.get(), self._names()
        if not self.summaries:
            return
        if not names:
            messagebox.showinfo("Mapeamento", "Marque as abas primeiro.", parent=self)
            return
        self._background(lambda: self._resolve(source, names), self._open_mapping_dialog)

    def _open_mapping_dialog(self, selected: dict[str, SheetMapping]) -> None:
        top = ctk.CTkToplevel(self)
        top.title("Mapeamento das colunas")
        top.geometry("980x520")
        top.transient(self)
        frame = ctk.CTkScrollableFrame(top)
        frame.pack(fill="both", expand=True, padx=10, pady=10)
        headers = ["Aba", "Cabeçalho", "CNPJ", "Acesso", "Fila Atual", "Nº temporário"]
        for j, h in enumerate(headers):
            ctk.CTkLabel(frame, text=h, font=ctk.CTkFont(weight="bold")).grid(row=0, column=j, padx=4, sticky="w")
        editors = {}
        for i, (name, m) in enumerate(selected.items(), start=1):
            cols = [f"{k} — {v[:22]}" for k, v in self.summaries[name].headers.items()]
            pick = lambda letter, empty: next((c for c in cols if c.split(" — ")[0] == letter), empty)
            row_vars = {
                "header_row": ctk.StringVar(value=str(m.header_row)),
                "cnpj": ctk.StringVar(value=pick(m.cnpj, NONE_COL)),
                "acesso": ctk.StringVar(value=pick(m.acesso, NONE_COL)),
                "fila_atual": ctk.StringVar(value=pick(m.fila_atual, NONE_FILA)),
                "numero_temporario": ctk.StringVar(value=pick(m.numero_temporario, NONE_COL)),
            }
            ctk.CTkLabel(frame, text=name, anchor="w").grid(row=i * 2 - 1, column=0, sticky="w", padx=4)
            ctk.CTkEntry(frame, textvariable=row_vars["header_row"], width=50).grid(row=i * 2 - 1, column=1, padx=4)
            for j, key in enumerate(("cnpj", "acesso", "fila_atual", "numero_temporario"), start=2):
                empty = NONE_FILA if key == "fila_atual" else NONE_COL
                ctk.CTkOptionMenu(frame, values=[empty] + cols, variable=row_vars[key], width=170,
                                  dynamic_resizing=False).grid(row=i * 2 - 1, column=j, padx=4, pady=2)
            if m.note:
                ctk.CTkLabel(frame, text=m.note, anchor="w", wraplength=900, font=ctk.CTkFont(size=11),
                             text_color=("gray35", "gray65")).grid(row=i * 2, column=0, columnspan=6, sticky="w", padx=4)
            editors[name] = row_vars

        def save() -> None:
            for name, v in editors.items():
                letter = lambda s: None if s.get() in (NONE_COL, NONE_FILA) else s.get().split(" — ")[0]
                m = self.mappings[name]
                m.header_row = int(v["header_row"].get() or 1)
                m.cnpj, m.acesso = letter(v["cnpj"]), letter(v["acesso"])
                m.fila_atual, m.numero_temporario = letter(v["fila_atual"]), letter(v["numero_temporario"])
                heads = self.summaries[name].headers
                m.expected_headers = {l: heads.get(l, "") for l in (m.cnpj, m.acesso, m.fila_atual, m.numero_temporario) if l}
            path = save_mappings(self.mappings, Path(self.file_var.get()).name)
            log.info("Mapeamento salvo em %s", path)
            top.destroy()
            self._validate()

        ctk.CTkButton(top, text="Salvar mapeamento", command=save).pack(pady=(0, 10))
        top.after(150, top.lift)

    # ================================================================ configuração
    def current_settings(self) -> PhoenixSettings:
        s = load_settings()
        try:
            s.interval_seconds = float(self.interval_var.get().replace(",", "."))
            s.timeout_seconds = float(self.timeout_var.get().replace(",", "."))
            s.max_retries = int(self.retries_var.get())
        except ValueError as exc:
            raise ConfigError("Intervalo, timeout e tentativas devem ser números.") from exc
        s.output_dir = self.output_var.get().strip() or s.output_dir
        s.browser_channel = CHANNELS[self.channel_var.get()]
        s.persist_session = self.persist_var.get()
        s.match.multiplos_bilhetes = MULTI[self.multi_var.get()]
        s.match.usar_numero_temporario = self.temp_var.get()
        s.match.strip_ddi_55 = self.ddi_var.get()
        s.status.falha_parcial_portada = [x.strip() for x in self.falha_var.get().split(";") if x.strip()]
        s.headless = False
        return s.validate()

    def _save_settings(self) -> None:
        try:
            self.settings = self.current_settings()
            path = save_settings(self.settings)
        except ConfigError as exc:
            messagebox.showwarning("Configuração", str(exc), parent=self)
            return
        log.info("Configurações salvas em %s", path)

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="Pasta de saída", parent=self)
        if path:
            self.output_var.set(path)

    # ================================================================ ações
    def _submit(self, command: str, *args) -> None:
        try:
            self.settings = self.current_settings()
        except ConfigError as exc:
            messagebox.showwarning("Configuração", str(exc), parent=self)
            return
        self.worker.submit(command, *args)
        self._refresh_buttons()
        self.status.configure(text=f"Executando: {command.replace('_', ' ')}…")

    def _test_search(self) -> None:
        value = ctk.CTkInputDialog(title="Consulta de teste", text="CNPJ para a consulta de teste:").get_input()
        if not value:
            return
        info = normalize_cnpj(value)
        if info.status != "valido":
            messagebox.showwarning("Consulta de teste", f"CNPJ inválido: {info.message}", parent=self)
            return
        self._submit("test_search", info.value)

    def _inspect(self) -> None:
        label = ctk.CTkInputDialog(title="Inspecionar página", text="Nome da etapa (ex.: menu, status, resultados, detalhes):").get_input()
        if label:
            self._submit("inspect", label)

    def _run(self) -> None:
        if self.running or not self.file_var.get():
            return
        try:
            settings = self.current_settings()
        except ConfigError as exc:
            messagebox.showwarning("Configuração", str(exc), parent=self)
            return
        names, source = self._names(), self.file_var.get()
        if not names:
            messagebox.showinfo("Executar", "Marque ao menos uma aba.", parent=self)
            return
        self.running = True
        self._refresh_buttons()
        self.status.configure(text="Importando as abas selecionadas…")

        def created(result) -> None:
            run_id, resumed = result
            if resumed and not messagebox.askyesno(
                    "Execução incompleta", f"A execução nº {run_id} desta planilha/abas está incompleta.\n\n"
                                           "Sim: continuar de onde parou (não repete CNPJs já pesquisados)\n"
                                           "Não: criar uma nova execução", parent=self):
                self._background(lambda: jobs.create_job(self.db, source, self._resolve(source, names), settings,
                                                         resume=False), created, self._failed)
                return
            if resumed:
                jobs.apply_settings(self.db, run_id, settings)
            self._start_run(run_id)

        self._background(lambda: jobs.create_job(self.db, source, self._resolve(source, names), settings), created, self._failed)

    def _start_run(self, run_id: int) -> None:
        self.current_run = run_id
        self.running = True
        self.log_view.append("INFO", f"──────── Execução nº {run_id} ────────")
        self.worker.submit("run", run_id)
        self._refresh_buttons()
        self._refresh_history()

    def _failed(self, exc: BaseException) -> None:
        self.running = False
        self._refresh_buttons()
        self.status.configure(text=f"Erro: {exc}")
        messagebox.showerror("PHOENIX BOT", str(exc), parent=self)

    def _pause(self) -> None:
        if self.worker.engine:
            self.worker.engine.pause()

    def _continue(self) -> None:
        if self.worker.engine:
            self.worker.engine.resume()
            self.banner.grid_remove()

    def _cancel(self) -> None:
        if self.worker.engine and messagebox.askyesno("Cancelar", "Cancelar com segurança? O progresso fica salvo.", parent=self):
            self.worker.engine.stop()

    def _retake(self) -> None:
        selected = self.history.selected()
        run_id = int(selected) if selected else self.current_run
        if run_id is None or self.running:
            return
        try:
            jobs.apply_settings(self.db, run_id, self.current_settings())
        except ConfigError as exc:
            messagebox.showwarning("Configuração", str(exc), parent=self)
            return
        self._start_run(run_id)

    def _export(self) -> None:
        selected = self.history.selected()
        run_id = int(selected) if selected else self.current_run
        if run_id is None:
            return

        def done(files) -> None:
            text = "\n".join(str(p) for p in files.values())
            log.info("Relatórios exportados:\n%s", text)
            if messagebox.askyesno("Exportar", f"Arquivos gerados:\n{text}\n\nAbrir a pasta?", parent=self):
                os.startfile(str(next(iter(files.values())).parent))  # type: ignore[attr-defined]

        self._background(lambda: jobs.export(self.db, run_id), done, self._failed)

    def _open_folder(self) -> None:
        folder = jobs.output_dir(self.settings)
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))  # type: ignore[attr-defined]

    # ================================================================ eventos
    def _handle(self, event: Any) -> None:
        if isinstance(event, WorkerEvent):
            level = "INFO" if event.ok else "ERROR"
            self.log_view.append(level, f"[{event.command}] {event.message}")
            self.status.configure(text=event.message[:200])
            if event.command == "run":
                self.running = False
                self._refresh_history()
                self._update_stats(force=True)
            elif event.command in ("test_connection", "test_search", "inspect") or not event.ok:
                (messagebox.showinfo if event.ok else messagebox.showwarning)("PHOENIX BOT", event.message, parent=self)
            self._refresh_buttons()
            return
        if not isinstance(event, EngineEvent):
            return
        data = event.data
        if event.kind == "state":
            self.engine_state = data["state"]
            if self.engine_state != EngineState.WAITING_OPERATOR:
                self.banner.grid_remove()
            self._refresh_buttons()
        elif event.kind == "operator":
            self.banner.configure(text=f"⚠ AÇÃO NECESSÁRIA: {data['message']}")
            self.banner.grid()
            self.bell()
        elif event.kind == "item_started":
            self.status.configure(text=f"Execução nº {self.current_run}: consultando CNPJ {data['identifier']} "
                                       f"({data['position']}º)")
        elif event.kind == "progress":
            stats = data["stats"]
            self.cards["cnpjs"].set(stats.total)
            self.cards["pesquisados"].set(stats.done)
            self.cards["erros"].set(stats.error)
            self.cards["percent"].set(f"{stats.percent:.1f}%")
            self.progress.set(stats.percent / 100)
            self._update_stats()
        elif event.kind == "finished":
            status: RunStatus | None = data.get("run_status")
            self.status.configure(text=f"Execução nº {self.current_run}: {status.label if status else 'não iniciada'}. "
                                       f"{data.get('message') or ''}")
            if status in (RunStatus.NEEDS_OPERATOR, RunStatus.FAILED):
                messagebox.showwarning("Execução interrompida", f"{data.get('message')}\n\nO progresso foi salvo; use RETOMAR.",
                                       parent=self)

    def _update_stats(self, force: bool = False) -> None:
        if self.current_run is None or self._stats_busy or (not force and time.monotonic() - self._last_stats < 3):
            return
        self._stats_busy, self._last_stats = True, time.monotonic()
        run_id = self.current_run

        def done(summary) -> None:
            self._stats_busy = False
            for key, name in (("linhas", "linhas"), ("bilhetes", "bilhetes_analisados"), ("andamento", "em_andamento"),
                              ("falha", "falha_parcial_portada"), ("conflito", "em_conflito"), ("outros", "outros_status"),
                              ("sem", "sem_correspondencia")):
                self.cards[key].set(summary[name])

        def failed(exc) -> None:
            self._stats_busy = False

        self._background(lambda: jobs.report(self.db, run_id).summary(), done, failed)

    def _refresh_history(self) -> None:
        repo = Repository(self.db.connect())
        try:
            prepo = PhoenixRepository(repo)
            rows = []
            for run_id in prepo.jobs()[:100]:
                run, stats = repo.get_run(run_id), repo.stats(run_id)
                rows.append((str(run_id), (run_id, run.created_at.replace("T", " "), run.status.label, run.name,
                                           f"{stats.done}/{stats.total}", stats.error)))
            self.history.set_rows(rows)
        finally:
            repo.close()

    def _refresh_buttons(self) -> None:
        busy = self.worker.busy or self.running
        engine = self.worker.engine is not None
        running = engine and self.engine_state in (EngineState.RUNNING, EngineState.STARTING)
        waiting = engine and self.engine_state in (EngineState.PAUSED, EngineState.WAITING_OPERATOR)
        for button, enabled, color in (
                (self.btn_browser, not busy, None), (self.btn_test, not busy, None), (self.btn_query, not busy, None),
                (self.btn_inspect, not busy, None), (self.btn_run, not busy, GREEN), (self.btn_pause, running, None),
                (self.btn_resume, waiting, None), (self.btn_cancel, running or waiting, RED),
                (self.btn_retake, not busy, None), (self.btn_export, not busy, None)):
            fg = (color or ctk.ThemeManager.theme["CTkButton"]["fg_color"]) if enabled else DISABLED
            button.configure(state="normal" if enabled else "disabled", fg_color=fg)

    # ================================================================ laços
    def _background(self, func: Callable[[], Any], on_done: Callable[[Any], None],
                    on_error: Callable[[BaseException], None] | None = None) -> None:
        def worker() -> None:
            try:
                result = func()
            except Exception as exc:
                self.events.put(("call", on_error or self._failed, exc))
            else:
                self.events.put(("call", on_done, result))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        try:
            for _ in range(200):
                try:
                    item = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    if isinstance(item, tuple) and item and item[0] == "call":
                        item[1](item[2])
                    else:
                        self._handle(item)
                except Exception as exc:
                    log.exception("Erro na interface: %s", exc)
            for _ in range(300):
                try:
                    level, text = self.logs.get_nowait()
                except queue.Empty:
                    break
                self.log_view.append(level, text)
        finally:
            if not self._closing:
                self.after(100, self._poll)

    def _tick(self) -> None:
        engine = self.worker.engine
        if engine is not None:
            self.cards["tempo"].set(format_duration(engine.elapsed()))
            eta = engine.eta()
            if eta is not None and self.engine_state == EngineState.RUNNING:
                self.status.configure(text=self.status.cget("text").split(" · restante")[0]
                                      + f" · restante ≈ {format_duration(eta)}")
        if not self._closing:
            self.after(1000, self._tick)

    def _on_close(self) -> None:
        if self.worker.engine and not messagebox.askyesno("Sair", "Há um lote em andamento. Cancelar com segurança e sair?", parent=self):
            return
        self._closing = True
        self.worker.shutdown()
        self.worker.join(timeout=90)
        import logging

        logging.getLogger("wsb").removeHandler(self._log_handler)
        self.destroy()


def run_app() -> None:
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    PhoenixApp().mainloop()
