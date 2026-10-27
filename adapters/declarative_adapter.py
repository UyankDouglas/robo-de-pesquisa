"""Adaptador configurado por JSON: integra um site novo sem escrever Python.

Cada arquivo ``config/sites/<id>.json`` descreve URL, seletores e campos de um site.
Veja ``config/sites/_modelo_site.json.example`` e a seção "Como adicionar um site" do README.

Prefira seletores estáveis: ``data-testid``, ``id``, ``name``, atributos ARIA ou texto do rótulo
(``label=...`` / ``role=...`` são aceitos pelo Playwright), evitando caminhos CSS frágeis.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adapters.base_adapter import AdapterContext, BrowserAdapter
from core.models import (
    ConfigError,
    HumanVerificationRequired,
    SearchResult,
    SessionExpiredError,
    TransientError,
)


@dataclass
class FieldSpec:
    key: str
    label: str
    selector: str
    attribute: str | None = None     # None = texto visível do elemento
    regex: str | None = None         # opcional: extrai o 1º grupo da expressão
    scope: str = "result"            # "result" (dentro do bloco de resultado) ou "page"


@dataclass
class SiteDefinition:
    id: str
    name: str
    description: str = ""
    search_url: str = ""                       # página com o formulário de pesquisa
    search_url_template: str | None = None     # alternativa: URL direta com {query}
    navigate_each_search: bool = True
    wait_until: str = "domcontentloaded"
    search_input: str | None = None
    submit: str | None = None                  # None = pressiona Enter no campo
    result_selector: str = ""
    not_found_selector: str | None = None
    error_selector: str | None = None
    captcha_selector: str | None = None
    fields: list[FieldSpec] = field(default_factory=list)
    requires_login: bool = False
    login_url: str | None = None
    logged_in_selector: str | None = None      # presente = usuário autenticado
    login_page_selector: str | None = None     # presente = página de login (sessão expirada)
    min_interval_seconds: float = 0.0
    demo_server: dict[str, Any] | None = None  # apenas para o site local de demonstração
    source: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source: str = "") -> "SiteDefinition":
        def need(key: str) -> Any:
            if not raw.get(key):
                raise ConfigError(f"{source or 'site'}: campo obrigatório '{key}' ausente")
            return raw[key]

        login = raw.get("login") or {}
        definition = cls(
            id=need("id"),
            name=need("name"),
            description=raw.get("description", ""),
            search_url=raw.get("search_url", ""),
            search_url_template=raw.get("search_url_template"),
            navigate_each_search=bool(raw.get("navigate_each_search", True)),
            wait_until=raw.get("wait_until", "domcontentloaded"),
            search_input=raw.get("search_input"),
            submit=raw.get("submit"),
            result_selector=need("result_selector"),
            not_found_selector=raw.get("not_found_selector"),
            error_selector=raw.get("error_selector"),
            captcha_selector=raw.get("captcha_selector"),
            fields=[
                FieldSpec(
                    key=f["key"],
                    label=f.get("label", f["key"]),
                    selector=f["selector"],
                    attribute=f.get("attribute"),
                    regex=f.get("regex"),
                    scope=f.get("scope", "result"),
                )
                for f in raw.get("fields", [])
            ],
            requires_login=bool(login.get("required", False)),
            login_url=login.get("url"),
            logged_in_selector=login.get("logged_in_selector"),
            login_page_selector=login.get("login_page_selector"),
            min_interval_seconds=float(raw.get("min_interval_seconds", 0)),
            demo_server=raw.get("demo_server"),
            source=source,
        )
        if not definition.search_url_template:
            if not definition.search_url or not definition.search_input:
                raise ConfigError(f"{source or definition.id}: informe 'search_url' e 'search_input' "
                                  "ou 'search_url_template'")
        if not definition.fields:
            raise ConfigError(f"{source or definition.id}: defina ao menos um item em 'fields'")
        return definition

    @classmethod
    def load(cls, path: Path) -> "SiteDefinition":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Não foi possível ler {path.name}: {exc}") from exc
        return cls.from_dict(raw, source=path.name)


class DeclarativeSiteAdapter(BrowserAdapter):
    """Classe base; o registro cria uma subclasse por arquivo JSON com ``definition`` preenchida."""

    definition: SiteDefinition

    @property
    def requires_login(self) -> bool:
        return bool(self.options.get("requires_login", self.definition.requires_login))

    def min_interval(self) -> float:
        return float(self.options.get("min_interval_seconds", self.definition.min_interval_seconds))

    @property
    def search_url(self) -> str:
        return str(self.options.get("search_url", self.definition.search_url))

    def open(self, ctx: AdapterContext) -> None:
        super().open(ctx)
        demo = self.definition.demo_server
        if demo and demo.get("autostart", True) and self.options.get("autostart_server", True):
            from demo_site.server import DemoOptions, ensure_server

            parts = urllib.parse.urlsplit(self.search_url)
            ensure_server(f"{parts.scheme}://{parts.netloc}", DemoOptions(require_login=bool(demo.get("require_login", False))))

    # ---------------------------------------------------------------- auxiliares
    def _visible(self, selector: str | None) -> bool:
        if not selector:
            return False
        return self.page.locator(selector).filter(visible=True).count() > 0

    def _check_blockers(self) -> None:
        d = self.definition
        if self._visible(d.captcha_selector):
            raise HumanVerificationRequired("O site exibiu uma verificação humana (CAPTCHA)")
        if self._visible(d.login_page_selector):
            raise SessionExpiredError("O site exibiu a página de login (sessão expirada)")

    def _extract(self, spec: FieldSpec, container) -> str:
        scope = self.page if spec.scope == "page" else container
        target = scope.locator(spec.selector)
        if target.count() == 0:
            return ""
        element = target.first
        value = element.get_attribute(spec.attribute) if spec.attribute else element.inner_text()
        value = (value or "").strip()
        if spec.regex:
            match = re.search(spec.regex, value)
            value = (match.group(1) if match and match.groups() else match.group(0) if match else "").strip()
        return value

    # ---------------------------------------------------------------- pesquisa
    def search(self, identifier: str) -> SearchResult:
        d = self.definition
        page = self.page
        if d.search_url_template:
            page.goto(d.search_url_template.format(query=urllib.parse.quote(identifier, safe="")), wait_until=d.wait_until)
        else:
            if d.navigate_each_search or not page.url.startswith(self.search_url):
                page.goto(self.search_url, wait_until=d.wait_until)
            self._check_blockers()
            field_input = page.locator(d.search_input).first
            field_input.fill(identifier)
            if d.submit:
                page.locator(d.submit).first.click()
            else:
                field_input.press("Enter")

        outcomes = [s for s in (d.captcha_selector, d.login_page_selector, d.error_selector,
                                d.not_found_selector, d.result_selector) if s]
        combined = page.locator(outcomes[0])
        for selector in outcomes[1:]:
            combined = combined.or_(page.locator(selector))
        combined.filter(visible=True).first.wait_for(state="visible", timeout=self.timeout_ms)

        self._check_blockers()
        if self._visible(d.error_selector):
            text = page.locator(d.error_selector).filter(visible=True).first.inner_text().strip()
            raise TransientError(f"O site informou erro: {text}")
        if self._visible(d.not_found_selector):
            text = page.locator(d.not_found_selector).filter(visible=True).first.inner_text().strip()
            return SearchResult.not_found(text or "Nenhum resultado")
        container = page.locator(d.result_selector).filter(visible=True).first
        return SearchResult.found_with({spec.key: self._extract(spec, container) for spec in d.fields})

    # ---------------------------------------------------------------- login
    def is_logged_in(self) -> bool:
        if not self.requires_login:
            return True
        d = self.definition
        page = self.page
        page.goto(self.search_url or d.login_url, wait_until=d.wait_until)
        if d.login_page_selector and self._visible(d.login_page_selector):
            return False
        if d.logged_in_selector:
            return self._visible(d.logged_in_selector)
        return True

    def begin_login(self) -> str:
        d = self.definition
        if d.login_url:
            self.page.goto(d.login_url, wait_until=d.wait_until)
        self.browser.bring_to_front()
        return f"Faça login em '{d.name}' na janela do navegador e depois clique em CONTINUAR."


def make_adapter_class(definition: SiteDefinition) -> type[DeclarativeSiteAdapter]:
    return type(
        f"DeclarativeSite_{re.sub(r'[^0-9A-Za-z_]', '_', definition.id)}",
        (DeclarativeSiteAdapter,),
        {
            "id": definition.id,
            "display_name": definition.name,
            "description": definition.description or f"Site configurado em {definition.source}",
            "fields": [(f.key, f.label) for f in definition.fields],
            "site_min_interval": definition.min_interval_seconds,
            "definition": definition,
        },
    )
