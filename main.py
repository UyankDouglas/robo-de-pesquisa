"""Web Search Bot — interface gráfica.

Uso:  python main.py      (ou WebSearchBot.exe após o empacotamento)
"""
from __future__ import annotations

import logging
import sys
import traceback

from core import paths


def main() -> int:
    paths.ensure_dirs()
    from utils.logger import setup_logging

    # no .exe sem console (windowed) sys.stderr é None: só grava em arquivo
    setup_logging(logging.INFO, console=sys.stderr is not None)
    try:
        from ui.app import run_app

        run_app()
        return 0
    except Exception:
        logging.getLogger("wsb").critical("Falha fatal na interface:\n%s", traceback.format_exc())
        try:
            from tkinter import messagebox

            messagebox.showerror("Web Search Bot", "Erro inesperado. Detalhes em logs/web_search_bot.log")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
