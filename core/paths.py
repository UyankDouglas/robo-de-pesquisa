"""Localização de pastas do aplicativo (funciona em modo fonte e empacotado com PyInstaller).

- ``APP_DIR``: pasta gravável do usuário (config, input, output, logs, data, sessions).
  Em modo fonte é a raiz do projeto; no .exe é a pasta onde o executável está.
- ``RESOURCE_DIR``: arquivos somente-leitura empacotados (demo_site/static, config de exemplo).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


if _is_frozen():
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = Path(__file__).resolve().parent.parent
    RESOURCE_DIR = APP_DIR

# Permite redirecionar os dados (útil em testes e em instalações compartilhadas).
DATA_ROOT = Path(os.environ.get("WSB_HOME", APP_DIR)).resolve()

CONFIG_DIR = DATA_ROOT / "config"
SITES_DIR = CONFIG_DIR / "sites"
INPUT_DIR = DATA_ROOT / "input"
OUTPUT_DIR = DATA_ROOT / "output"
LOGS_DIR = DATA_ROOT / "logs"
DATA_DIR = DATA_ROOT / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
LOCKS_DIR = DATA_DIR / "locks"
DB_PATH = DATA_DIR / "web_search_bot.db"
UI_STATE_PATH = DATA_DIR / "ui_state.json"

# Sites declarativos que acompanham o programa (somente leitura).
BUNDLED_SITES_DIR = RESOURCE_DIR / "config" / "sites"
DEMO_STATIC_DIR = RESOURCE_DIR / "demo_site" / "static"


def ensure_dirs() -> None:
    for folder in (CONFIG_DIR, SITES_DIR, INPUT_DIR, OUTPUT_DIR, LOGS_DIR, DATA_DIR, SESSIONS_DIR, LOCKS_DIR):
        folder.mkdir(parents=True, exist_ok=True)
