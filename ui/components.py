"""Componentes visuais reutilizáveis da interface (CustomTkinter)."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Iterable, Sequence

import customtkinter as ctk

LEVEL_COLORS = {
    "DEBUG": ("#6b7280", "#9ca3af"),
    "INFO": ("#111827", "#e5e7eb"),
    "WARNING": ("#b45309", "#fbbf24"),
    "ERROR": ("#b91c1c", "#f87171"),
    "CRITICAL": ("#b91c1c", "#f87171"),
    "SUCCESS": ("#047857", "#34d399"),
}


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--:--"
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class StatCard(ctk.CTkFrame):
    def __init__(self, master, title: str, value: str = "0", accent: str | tuple[str, str] | None = None):
        super().__init__(master, corner_radius=10)
        self.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=12), text_color=("gray35", "gray70")).grid(
            row=0, column=0, padx=12, pady=(8, 0), sticky="w")
        self.value = ctk.CTkLabel(self, text=value, font=ctk.CTkFont(size=22, weight="bold"),
                                  text_color=accent or ("gray10", "gray95"))
        self.value.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="w")

    def set(self, value) -> None:
        self.value.configure(text=str(value))


class LogView(ctk.CTkTextbox):
    """Área de logs somente leitura com cores por nível e limite de linhas."""

    def __init__(self, master, max_lines: int = 3000, **kwargs):
        super().__init__(master, wrap="word", font=ctk.CTkFont(family="Consolas", size=12), **kwargs)
        self.max_lines = max_lines
        self._apply_colors()
        self.configure(state="disabled")

    def _apply_colors(self) -> None:
        dark = ctk.get_appearance_mode() == "Dark"
        for level, (light_color, dark_color) in LEVEL_COLORS.items():
            self.tag_config(level, foreground=dark_color if dark else light_color)

    def append(self, level: str, text: str) -> None:
        self.configure(state="normal")
        self.insert("end", text + "\n", level if level in LEVEL_COLORS else "INFO")
        lines = int(self.index("end-1c").split(".")[0])
        if lines > self.max_lines:
            self.delete("1.0", f"{lines - self.max_lines}.0")
        self.configure(state="disabled")
        self.see("end")

    def clear(self) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")

    def refresh_theme(self) -> None:
        self._apply_colors()


def style_treeview(widget: tk.Misc) -> None:
    """Ajusta o ttk.Treeview às cores claras/escuras do CustomTkinter."""
    dark = ctk.get_appearance_mode() == "Dark"
    bg, fg, sel, head_bg = ("#2b2b2b", "#e5e7eb", "#1f6aa5", "#3a3a3a") if dark else ("#ffffff", "#111827", "#3b8ed0", "#e5e7eb")
    style = ttk.Style(widget)
    style.theme_use("clam")
    style.configure("WSB.Treeview", background=bg, fieldbackground=bg, foreground=fg, rowheight=24, borderwidth=0)
    style.map("WSB.Treeview", background=[("selected", sel)], foreground=[("selected", "#ffffff")])
    style.configure("WSB.Treeview.Heading", background=head_bg, foreground=fg, relief="flat", font=("Segoe UI", 9, "bold"))
    style.map("WSB.Treeview.Heading", background=[("active", head_bg)])


class Table(ctk.CTkFrame):
    """ttk.Treeview com barras de rolagem, para listas (histórico, prévia da importação)."""

    def __init__(self, master, columns: Sequence[tuple[str, str, int]], height: int = 10):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        style_treeview(self)
        self.tree = ttk.Treeview(self, columns=[c[0] for c in columns], show="headings", height=height,
                                 style="WSB.Treeview", selectmode="browse")
        for key, title, width in columns:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=40, stretch=True, anchor="w")
        yscroll = ctk.CTkScrollbar(self, command=self.tree.yview)
        xscroll = ctk.CTkScrollbar(self, command=self.tree.xview, orientation="horizontal")
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

    def set_rows(self, rows: Iterable[tuple[str, Sequence]]) -> None:
        selected = self.selected()
        self.tree.delete(*self.tree.get_children())
        for iid, values in rows:
            self.tree.insert("", "end", iid=iid, values=list(values))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)

    def selected(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None
