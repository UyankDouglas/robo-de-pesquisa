"""Tipos compartilhados: status, resultados de pesquisa e exceções do domínio."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ItemStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    FOUND = "found"
    NOT_FOUND = "not_found"
    ERROR = "error"
    DUPLICATE = "duplicate"
    INVALID = "invalid"

    @property
    def label(self) -> str:
        return ITEM_STATUS_LABELS[self]


ITEM_STATUS_LABELS = {
    ItemStatus.PENDING: "Pendente",
    ItemStatus.PROCESSING: "Em processamento",
    ItemStatus.FOUND: "Concluído",
    ItemStatus.NOT_FOUND: "Sem resultado",
    ItemStatus.ERROR: "Erro",
    ItemStatus.DUPLICATE: "Duplicado",
    ItemStatus.INVALID: "Inválido",
}

ITEM_RESULT_SUMMARY = {
    ItemStatus.PENDING: "Aguardando consulta",
    ItemStatus.PROCESSING: "Consulta interrompida",
    ItemStatus.FOUND: "Informação encontrada",
    ItemStatus.NOT_FOUND: "Não encontrado",
    ItemStatus.ERROR: "Falha na consulta",
    ItemStatus.DUPLICATE: "Registro repetido na planilha",
    ItemStatus.INVALID: "Registro não consultado",
}

# Status que entram na fila de processamento / contam no total.
PROCESSABLE_STATUSES = (
    ItemStatus.PENDING,
    ItemStatus.PROCESSING,
    ItemStatus.FOUND,
    ItemStatus.NOT_FOUND,
    ItemStatus.ERROR,
)


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"            # parada pelo operador; pode ser retomada
    INTERRUPTED = "interrupted"    # processo caiu sem finalizar; pode ser retomada
    NEEDS_OPERATOR = "needs_operator"  # CAPTCHA/login sem operador (modo agendado)
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def label(self) -> str:
        return RUN_STATUS_LABELS[self]


RUN_STATUS_LABELS = {
    RunStatus.CREATED: "Criada",
    RunStatus.RUNNING: "Em execução",
    RunStatus.PAUSED: "Pausada",
    RunStatus.STOPPED: "Parada",
    RunStatus.INTERRUPTED: "Interrompida",
    RunStatus.NEEDS_OPERATOR: "Aguardando operador",
    RunStatus.COMPLETED: "Concluída",
    RunStatus.FAILED: "Falhou",
}

RESUMABLE_RUN_STATUSES = (
    RunStatus.CREATED,
    RunStatus.RUNNING,
    RunStatus.PAUSED,
    RunStatus.STOPPED,
    RunStatus.INTERRUPTED,
    RunStatus.NEEDS_OPERATOR,
    RunStatus.FAILED,
)


@dataclass
class SearchResult:
    """Resultado padronizado devolvido por qualquer adaptador."""

    found: bool
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    @classmethod
    def found_with(cls, data: dict[str, Any], message: str = "") -> "SearchResult":
        return cls(found=True, data=dict(data), message=message)

    @classmethod
    def not_found(cls, message: str = "Nenhum resultado") -> "SearchResult":
        return cls(found=False, data={}, message=message)


# ---------------------------------------------------------------------------
# Exceções levantadas pelos adaptadores e tratadas pelo motor
# ---------------------------------------------------------------------------
class AdapterError(Exception):
    """Base para erros de consulta."""


class TransientError(AdapterError):
    """Falha temporária (rede, timeout, HTTP 5xx). O motor tenta novamente com espera progressiva."""


class PermanentError(AdapterError):
    """Falha definitiva para o registro (ex.: identificador recusado pelo site). Não há nova tentativa."""


class RateLimitedError(TransientError):
    """O serviço pediu para reduzir o ritmo (HTTP 429 ou mensagem equivalente)."""

    def __init__(self, message: str = "Limite de requisições atingido", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class HumanVerificationRequired(AdapterError):
    """CAPTCHA ou validação humana. O robô NÃO tenta burlar: pausa e pede ação do operador."""


class SessionExpiredError(AdapterError):
    """A sessão autenticada expirou ou não existe; é necessário novo login manual."""


class OperatorRequired(Exception):
    """Levantada pelo motor quando precisa de um operador e está em modo não interativo."""


class AlreadyRunningError(Exception):
    """Outra instância já está processando a mesma execução."""


class ConfigError(Exception):
    """Configuração inválida ou incompleta."""
