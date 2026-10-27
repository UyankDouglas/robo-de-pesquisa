"""Configuração de logs: arquivo rotativo geral, arquivo por execução e encaminhamento para a interface."""
from __future__ import annotations

import logging
import queue
import re
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator

from core import paths

APP_LOGGER = "wsb"
FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
SHORT_FORMAT = "%(asctime)s %(levelname)s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_SENSITIVE = re.compile(
    r"(?i)\b(password|passwd|senha|token|secret|segredo|authorization|cookie|api[_-]?key)\b(\s*[=:]\s*)([^\s,;&]+)"
)


class RedactSecretsFilter(logging.Filter):
    """Mascara valores de campos sensíveis (senha=..., token: ...) antes de gravar."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _SENSITIVE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


class QueueLogHandler(logging.Handler):
    """Envia registros formatados para uma fila consumida pela thread da interface."""

    def __init__(self, target: "queue.Queue[tuple[str, str]]", level: int = logging.INFO):
        super().__init__(level)
        self.target = target
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.target.put_nowait((record.levelname, self.format(record)))
        except Exception:  # pragma: no cover - nunca derrubar o worker por causa do log
            self.handleError(record)


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{APP_LOGGER}.{name}" if name else APP_LOGGER)


def setup_logging(level: int = logging.INFO, console: bool = True, log_dir: Path | None = None) -> logging.Logger:
    """Idempotente: pode ser chamada várias vezes sem duplicar handlers."""
    log_dir = log_dir or paths.LOGS_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(APP_LOGGER)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, "_wsb_managed", False):
            logger.removeHandler(handler)
            handler.close()

    file_handler = RotatingFileHandler(
        log_dir / "web_search_bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
    file_handler.addFilter(RedactSecretsFilter())
    file_handler._wsb_managed = True  # type: ignore[attr-defined]
    logger.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setLevel(level)
        stream.setFormatter(logging.Formatter(SHORT_FORMAT, "%H:%M:%S"))
        stream.addFilter(RedactSecretsFilter())
        stream._wsb_managed = True  # type: ignore[attr-defined]
        logger.addHandler(stream)
    return logger


def attach_queue_handler(target: "queue.Queue[tuple[str, str]]", level: int = logging.INFO) -> QueueLogHandler:
    handler = QueueLogHandler(target, level)
    handler.addFilter(RedactSecretsFilter())
    logger = logging.getLogger(APP_LOGGER)
    if logger.getEffectiveLevel() > level:
        logger.setLevel(level)
    logger.addHandler(handler)
    return handler


@contextmanager
def run_log(run_id: int, log_dir: Path | None = None) -> Iterator[Path]:
    """Grava, enquanto ativo, um arquivo de log exclusivo da execução: logs/run_<id>.log."""
    log_dir = log_dir or paths.LOGS_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"run_{run_id}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
    handler.addFilter(RedactSecretsFilter())
    logger = logging.getLogger(APP_LOGGER)
    logger.addHandler(handler)
    try:
        yield path
    finally:
        logger.removeHandler(handler)
        handler.close()


def mask_identifier(value: str) -> str:
    """Mantém só o começo e o fim do identificador (ex.: 123*******0190)."""
    value = str(value)
    if len(value) <= 4:
        return "*" * len(value)
    keep = 3 if len(value) > 8 else 1
    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]
