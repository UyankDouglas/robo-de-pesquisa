"""Adaptadores do site local de demonstração (``demo_site``).

Servem de exemplo completo de como integrar um site: o adaptador de navegador e o de API
consultam o mesmo site fictício por caminhos diferentes. Para um site real, copie o
adaptador mais adequado e troque URL, seletores e campos pelos do site autorizado.
"""
from __future__ import annotations

from typing import Any

from adapters.base_adapter import AdapterContext, ApiAdapter, BrowserAdapter
from core.models import (
    HumanVerificationRequired,
    PermanentError,
    RateLimitedError,
    SearchResult,
    SessionExpiredError,
    TransientError,
)

DEMO_FIELDS = [
    ("nome", "NOME / RAZÃO SOCIAL"),
    ("situacao", "SITUAÇÃO CADASTRAL"),
    ("data_registro", "DATA DE REGISTRO"),
    ("municipio", "MUNICÍPIO"),
    ("uf", "UF"),
    ("detalhes", "DETALHES"),
]

# Espera o resultado *desta* pesquisa (data-query) sair do estado "carregando".
_WAIT_RESULT_JS = """(q) => {
  const el = document.querySelector('[data-testid="result"]');
  return !!el && el.dataset.query === q && !['idle', 'loading'].includes(el.dataset.state);
}"""


def _start_demo_server(options: dict[str, Any], require_login: bool) -> None:
    if not options.get("autostart_server", True):
        return
    from demo_site.server import DemoOptions, ensure_server

    ensure_server(
        options["base_url"],
        DemoOptions(
            require_login=require_login,
            session_ttl=float(options.get("session_ttl", 0)),
            max_per_minute=int(options.get("server_max_per_minute", 0)),
        ),
    )


class DemoBrowserAdapter(BrowserAdapter):
    id = "demo"
    display_name = "Demonstração — navegador (site local)"
    description = "Pesquisa no site fictício local usando o navegador (Playwright)."
    fields = DEMO_FIELDS

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        return {"base_url": "http://127.0.0.1:8765", "autostart_server": True, "requires_login": False}

    @property
    def base_url(self) -> str:
        return str(self.options["base_url"]).rstrip("/")

    def open(self, ctx: AdapterContext) -> None:
        super().open(ctx)
        _start_demo_server(self.options, self.requires_login)

    def _goto_search_page(self):
        page = self.page
        page.goto(self.base_url + "/", wait_until="domcontentloaded")
        if "/login" in page.url:
            raise SessionExpiredError("O site pediu login (sessão inexistente ou expirada)")
        return page

    def search(self, identifier: str) -> SearchResult:
        page = self._goto_search_page()
        page.get_by_test_id("search-input").fill(identifier)
        page.get_by_test_id("search-button").click()
        page.wait_for_function(_WAIT_RESULT_JS, arg=identifier, timeout=self.timeout_ms)

        result = page.get_by_test_id("result")
        state = result.get_attribute("data-state")
        if state == "found":
            data = {}
            for field in result.locator("[data-field]").all():
                data[field.get_attribute("data-field")] = field.inner_text().strip()
            return SearchResult.found_with(data)
        if state == "not-found":
            return SearchResult.not_found(result.inner_text().strip())
        if state == "captcha":
            raise HumanVerificationRequired("O site exibiu uma verificação humana (CAPTCHA)")
        if state == "session-expired":
            raise SessionExpiredError("Sessão expirada")
        if state == "rate-limited":
            retry_after = result.get_attribute("data-retry-after")
            raise RateLimitedError("O site pediu para aguardar antes de novas consultas",
                                   float(retry_after) if retry_after else None)
        if state == "error":
            status = result.get_attribute("data-http-status") or ""
            message = result.inner_text().strip()
            if status.isdigit() and 400 <= int(status) < 500:
                raise PermanentError(f"Consulta recusada (HTTP {status}): {message}")
            raise TransientError(f"Erro do site (HTTP {status or '?'}): {message}")
        raise TransientError(f"Estado de resultado inesperado: {state}")

    # -------------------------------------------------------- login assistido
    def is_logged_in(self) -> bool:
        if not self.requires_login:
            return True
        page = self.page
        page.goto(self.base_url + "/", wait_until="domcontentloaded")
        return "/login" not in page.url and page.get_by_test_id("logged-user").count() > 0

    def begin_login(self) -> str:
        self.page.goto(self.base_url + "/login", wait_until="domcontentloaded")
        self.browser.bring_to_front()
        return ("Faça login na janela do navegador (demonstração: usuário demo, senha demo123) "
                "e depois clique em CONTINUAR.")


class DemoLoginAdapter(DemoBrowserAdapter):
    id = "demo_login"
    display_name = "Demonstração com login — navegador (site local)"
    description = "Igual à demonstração, mas o site exige login manual do operador."

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        return {"base_url": "http://127.0.0.1:8766", "autostart_server": True, "requires_login": True, "session_ttl": 0}


class DemoApiAdapter(ApiAdapter):
    id = "demo_api"
    display_name = "Demonstração — API JSON (sem navegador)"
    description = "Consulta a API JSON do site fictício. Modelo para sites que oferecem API oficial."
    fields = DEMO_FIELDS

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        return {"base_url": "http://127.0.0.1:8765", "autostart_server": True}

    def open(self, ctx: AdapterContext) -> None:
        super().open(ctx)
        _start_demo_server(self.options, require_login=False)

    def search(self, identifier: str) -> SearchResult:
        status, body = self.get_json(str(self.options["base_url"]).rstrip("/") + "/api/search", {"q": identifier})
        if status == 200 and isinstance(body, dict):
            if body.get("status") == "found":
                return SearchResult.found_with(body.get("data") or {})
            if body.get("status") == "not_found":
                return SearchResult.not_found(body.get("message", "Nenhum resultado"))
        if status == 404:
            return SearchResult.not_found("Não encontrado (HTTP 404)")
        raise PermanentError(f"Resposta inesperada da API (HTTP {status})")
