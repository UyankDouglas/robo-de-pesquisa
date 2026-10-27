"""PHOENIX BOT — Consulta de Portabilidade TIM (interface gráfica).

Uso:  python phoenix_main.py   (ou PhoenixBot.exe após o empacotamento)
"""
from __future__ import annotations

import logging
import sys
import traceback

from core import paths


def main() -> int:
    paths.ensure_dirs()
    from utils.logger import setup_logging

    setup_logging(logging.INFO, console=sys.stderr is not None)
    try:
        from ui.phoenix_app import run_app

        run_app()
        return 0
    except Exception:
        logging.getLogger("wsb").critical("Falha fatal na interface:\n%s", traceback.format_exc())
        try:
            from tkinter import messagebox

            messagebox.showerror("PHOENIX BOT", "Erro inesperado. Detalhes em logs/web_search_bot.log")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
