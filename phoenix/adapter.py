"""Adaptador do Phoenix2 para o motor de execução (uma "consulta" = um CNPJ distinto)."""
from __future__ import annotations

from typing import Any

from adapters.base_adapter import AdapterContext, BrowserAdapter
from core.models import SearchResult
from phoenix.client import PhoenixClient
from phoenix.config import load_selectors


class PhoenixAdapter(BrowserAdapter):
    id = "phoenix2"
    display_name = "TIM Phoenix2 Business — Status Solicitações"
    description = "Consulta bilhetes de portabilidade por CNPJ (login manual do operador)."
    fields = [("solicitacoes", "SOLICITAÇÕES")]

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        return {"base_url": "", "requires_login": True, "max_paginas": 50, "selectors": None}

    def open(self, ctx: AdapterContext) -> None:
        super().open(ctx)
        self.client = PhoenixClient(
            lambda: self.browser.page,
            self.options.get("selectors") or load_selectors(),
            self.options["base_url"],
            timeout_seconds=ctx.timeout_seconds,
            max_pages=int(self.options.get("max_paginas", 50)),
        )

    def search(self, identifier: str) -> SearchResult:
        result = self.client.consultar(identifier)
        if not result["solicitacoes"]:
            return SearchResult.not_found("Nenhuma Solicitação foi encontrada.")
        tickets = sum(len(s.get("bilhetes", [])) for s in result["solicitacoes"])
        return SearchResult.found_with(result, f"{len(result['solicitacoes'])} solicitação(ões), {tickets} bilhete(s)")

    def is_logged_in(self) -> bool:
        return self.client.is_logged_in()

    def begin_login(self) -> str:
        self.client.open_home()
        self.browser.bring_to_front()
        return ("Faça o login no Phoenix2 na janela do navegador (inclusive autenticação adicional, se houver) "
                "e depois clique em CONTINUAR.")
