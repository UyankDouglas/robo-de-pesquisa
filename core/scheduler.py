"""Integração com o Agendador de Tarefas do Windows (schtasks).

Gera um .bat curto que chama o modo sem interface com a configuração salva e monta o comando
``schtasks`` para registrá-lo. O registro da tarefa só acontece quando o operador pede
(``cli.py schedule ... --apply``), pois altera a configuração do Windows.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from core import paths

SCHEDULES = ("ONCE", "DAILY", "WEEKLY", "HOURLY", "MINUTE")


def cli_command() -> list[str]:
    """Comando que executa o modo sem interface (fonte ou .exe)."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).with_name("WebSearchBotCLI.exe"))]
    return [sys.executable, str(paths.APP_DIR / "cli.py")]


def write_wrapper(task_name: str, config_path: Path, folder: Path | None = None) -> Path:
    """Cria ``scheduled/<tarefa>.bat`` que roda o robô e repassa o código de saída."""
    folder = folder or (paths.DATA_ROOT / "scheduled")
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", task_name)
    command = " ".join(f'"{part}"' for part in cli_command())
    wrapper = folder / f"{safe}.bat"
    wrapper.write_text(
        "@echo off\r\n"
        "rem Gerado pelo Web Search Bot - executa a configuracao salva sem interface grafica.\r\n"
        f'cd /d "{paths.DATA_ROOT}"\r\n'
        f'{command} run --config "{config_path.resolve()}"\r\n'
        "exit /b %ERRORLEVEL%\r\n",
        encoding="oem" if sys.platform == "win32" else "utf-8",  # o cmd.exe lê .bat na página OEM
        errors="replace",
    )
    return wrapper


def build_schtasks_command(
    task_name: str,
    wrapper: Path,
    schedule: str = "DAILY",
    start_time: str = "08:00",
    modifier: int | None = None,
    days: str | None = None,
) -> list[str]:
    schedule = schedule.upper()
    if schedule not in SCHEDULES:
        raise ValueError(f"Agendamento inválido: {schedule}. Use {', '.join(SCHEDULES)}.")
    if not re.fullmatch(r"\d{2}:\d{2}", start_time):
        raise ValueError("Horário deve estar no formato HH:MM")
    command = [
        "schtasks", "/Create",
        "/TN", f"WebSearchBot\\{task_name}",
        "/TR", f'"{wrapper}"',
        "/SC", schedule,
        "/ST", start_time,
        "/F",
    ]
    if modifier:
        command += ["/MO", str(modifier)]
    if days and schedule == "WEEKLY":
        command += ["/D", days]
    return command


def apply(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, check=False)
