"""MODELO — adaptador para uma API oficial/documentada (preferível à automação do navegador).

Como usar:
1. Confirme na documentação oficial que a API existe, que seu uso é permitido para este fim e quais
   são os limites (requisições por minuto, horário, termos de uso).
2. Copie para ``adapters/<nome>_adapter.py`` e preencha os pontos PREENCHER com a documentação REAL.
3. Credenciais/tokens NUNCA vão no código-fonte nem em arquivos do projeto: leia de variável de ambiente
   (ex.: definida pelo administrador no Windows) como abaixo.
4. Teste:  python cli.py test-site --adapter <id> --query <valor>
"""
from __future__ import annotations

import os
from typing import Any

from adapters.base_adapter import ApiAdapter
from core.models import ConfigError, PermanentError, SearchResult


class MinhaApiAdapter(ApiAdapter):
    id = "minha_api"                          # PREENCHER
    display_name = "Minha API oficial"        # PREENCHER
    description = "Consulta via API documentada."
    fields = [("campo1", "CAMPO 1")]          # PREENCHER
    site_min_interval = 1.0                   # PREENCHER conforme o limite documentado

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        return {"base_url": "PREENCHER", "token_env": "MINHA_API_TOKEN"}

    def default_headers(self) -> dict[str, str]:
        headers = super().default_headers()
        token = os.environ.get(self.options["token_env"], "")
        if not token:
            raise ConfigError(f"Defina a variável de ambiente {self.options['token_env']} com o token da API.")
        headers["Authorization"] = f"Bearer {token}"      # PREENCHER conforme a documentação
        return headers

    def search(self, identifier: str) -> SearchResult:
        # get_json já converte 429 -> espera, 5xx/timeout -> nova tentativa, 401 -> sessão expirada
        status, body = self.get_json(f"{self.options['base_url']}/PREENCHER/{identifier}")
        if status == 404:
            return SearchResult.not_found("Não encontrado")
        if status == 200 and isinstance(body, dict):
            return SearchResult.found_with({"campo1": body.get("PREENCHER", "")})
        raise PermanentError(f"Resposta inesperada (HTTP {status})")
