"""Contrato dos adaptadores de site.

Cada site (ou API) é um adaptador. O motor só conhece esta interface, então incluir um site
novo não exige mexer no código principal. Há três formas de integrar:

1. ``ApiAdapter``      — consulta uma API oficial/autorizada (preferível quando existir);
2. ``BrowserAdapter``  — automação com Playwright escrita em Python;
3. Site declarativo   — arquivo JSON em ``config/sites`` (ver ``declarative_adapter.py``).

Regras para os adaptadores:
- devolver ``SearchResult.not_found()`` quando o site informa que não há resultado (não é erro);
- levantar ``TransientError`` para falhas temporárias (o motor repete com espera progressiva);
- levantar ``PermanentError`` para falhas definitivas do registro;
- levantar ``HumanVerificationRequired`` ao detectar CAPTCHA — nunca tentar resolvê-lo;
- levantar ``SessionExpiredError`` quando a sessão autenticada não for mais válida.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from core.models import (
    HumanVerificationRequired,
    PermanentError,
    RateLimitedError,
    SearchResult,
    SessionExpiredError,
    TransientError,
)
from utils.logger import get_logger

if TYPE_CHECKING:
    from core.browser import BrowserManager

USER_AGENT = "WebSearchBot/1.0 (automacao autorizada)"


@dataclass
class AdapterContext:
    timeout_seconds: float = 30.0
    browser: "BrowserManager | None" = None
    extra: dict[str, Any] = field(default_factory=dict)


class BaseAdapter(ABC):
    id: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    kind: ClassVar[str] = "browser"          # "browser" ou "api"
    requires_browser: ClassVar[bool] = True
    fields: ClassVar[list[tuple[str, str]]] = []   # (chave, título da coluna no Excel)
    site_min_interval: ClassVar[float] = 0.0       # intervalo mínimo exigido pelo site (s)

    def __init__(self, options: dict[str, Any] | None = None):
        self.options: dict[str, Any] = {**self.default_options(), **(options or {})}
        self.ctx = AdapterContext()
        self.log = get_logger(f"adapter.{self.id or type(self).__name__}")

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        return {}

    # ------------------------------------------------------------ metadados
    @property
    def requires_login(self) -> bool:
        return bool(self.options.get("requires_login", False))

    def field_labels(self) -> list[tuple[str, str]]:
        return list(self.fields)

    def min_interval(self) -> float:
        return float(self.options.get("min_interval_seconds", self.site_min_interval))

    # ------------------------------------------------------------ ciclo de vida
    def open(self, ctx: AdapterContext) -> None:
        self.ctx = ctx

    def close(self) -> None:
        pass

    @abstractmethod
    def search(self, identifier: str) -> SearchResult:
        """Consulta um identificador e devolve o resultado padronizado."""

    # ------------------------------------------------------------ login assistido
    def is_logged_in(self) -> bool:
        return True

    def begin_login(self) -> str:
        """Prepara o login manual (ex.: abre a página de login). Retorna a instrução ao operador."""
        return "Faça o login no navegador e depois clique em CONTINUAR."

    def storage_state(self) -> dict[str, Any] | None:
        return None

    # ------------------------------------------------------------ erros
    def classify_error(self, exc: BaseException) -> str:
        """'transient' (tenta de novo) ou 'permanent'."""
        if isinstance(exc, (TransientError, TimeoutError, ConnectionError, socket.timeout)):
            return "transient"
        return "permanent"

    def recover(self, exc: BaseException) -> None:
        """Chamado após falha temporária, antes da nova tentativa."""


class BrowserAdapter(BaseAdapter):
    kind = "browser"
    requires_browser = True

    @property
    def browser(self) -> "BrowserManager":
        if self.ctx.browser is None:
            raise RuntimeError("Adaptador de navegador sem navegador aberto")
        return self.ctx.browser

    @property
    def page(self):
        return self.browser.page

    @property
    def timeout_ms(self) -> int:
        return int(self.ctx.timeout_seconds * 1000)

    def storage_state(self) -> dict[str, Any] | None:
        return self.browser.export_storage_state()

    def classify_error(self, exc: BaseException) -> str:
        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError:  # pragma: no cover
            PlaywrightError = ()  # type: ignore[assignment]
        if PlaywrightError and isinstance(exc, PlaywrightError):  # inclui TimeoutError do Playwright
            return "transient"
        return super().classify_error(exc)

    def recover(self, exc: BaseException) -> None:
        try:
            self.browser.ensure_page()
        except Exception as err:  # pragma: no cover
            self.log.debug("Falha ao recuperar a página: %s", err)


class ApiAdapter(BaseAdapter):
    """Base para APIs HTTP/JSON (usa apenas a biblioteca padrão)."""

    kind = "api"
    requires_browser = False

    def default_headers(self) -> dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept": "application/json"}

    def get_json(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
        """GET que converte respostas HTTP nas exceções esperadas pelo motor.

        Retorna (status, json) para 2xx e 404; demais códigos viram exceções.
        """
        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={**self.default_headers(), **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=self.ctx.timeout_seconds) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(body) if body else None
            except ValueError:
                payload = None
            if exc.code == 404:
                return 404, payload
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                raise RateLimitedError(
                    "Limite de requisições do serviço atingido (HTTP 429)",
                    float(retry_after) if retry_after and retry_after.isdigit() else None,
                ) from exc
            if exc.code in (401, 419):
                raise SessionExpiredError(f"Autenticação recusada (HTTP {exc.code})") from exc
            if exc.code == 403 and isinstance(payload, dict) and payload.get("captcha"):
                raise HumanVerificationRequired("O serviço exigiu verificação humana") from exc
            if exc.code >= 500 or exc.code == 408:
                raise TransientError(f"Erro temporário do serviço (HTTP {exc.code})") from exc
            raise PermanentError(f"Consulta recusada (HTTP {exc.code})") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise TransientError(f"Falha de rede: {reason}") from exc
