"""MODELO — adaptador Python para um site acessado pelo navegador (Playwright).

Como usar:
1. Copie este arquivo para ``adapters/<nome_do_site>_adapter.py`` (o nome PRECISA terminar em ``_adapter.py``).
2. Preencha ``id``, ``display_name``, ``fields`` e os seletores marcados com PREENCHER usando o site REAL
   e autorizado. Este modelo não contém seletores de nenhum site real.
3. Teste uma consulta:  python cli.py test-site --adapter <id> --query <valor> --visible
4. O site aparece automaticamente na lista da interface.

Use um adaptador JSON (config/sites) se o site for simples; use Python quando precisar de lógica
(várias páginas, abas, tabelas, downloads, regras de "não encontrado" complexas).
"""
from __future__ import annotations

from typing import Any

from adapters.base_adapter import BrowserAdapter
from core.models import HumanVerificationRequired, SearchResult, SessionExpiredError, TransientError


class MeuSiteAdapter(BrowserAdapter):
    id = "meu_site"                                  # PREENCHER: identificador único (sem espaços)
    display_name = "Meu site (navegador)"            # PREENCHER: nome exibido na interface
    description = "Consulta X no site Y."            # PREENCHER
    fields = [                                       # PREENCHER: (chave, título da coluna no Excel)
        ("campo1", "CAMPO 1"),
        ("campo2", "CAMPO 2"),
    ]
    site_min_interval = 0.0                          # intervalo mínimo exigido pelos termos do site (s)

    @classmethod
    def default_options(cls) -> dict[str, Any]:
        return {
            "search_url": "PREENCHER",               # URL da página de pesquisa
            "requires_login": False,
            "login_url": None,
        }

    def search(self, identifier: str) -> SearchResult:
        page = self.page
        page.goto(self.options["search_url"], wait_until="domcontentloaded")

        # 1) bloqueios: nunca tente contornar CAPTCHA — apenas avise o operador
        if page.locator("PREENCHER: seletor do CAPTCHA").count():
            raise HumanVerificationRequired("O site pediu verificação humana")
        if page.locator("PREENCHER: seletor do formulário de login").count():
            raise SessionExpiredError("Sessão expirada")

        # 2) pesquisa — prefira get_by_label / get_by_role / get_by_test_id a caminhos CSS longos
        page.get_by_label("PREENCHER: rótulo do campo").fill(identifier)
        page.get_by_role("button", name="PREENCHER: texto do botão").click()

        # 3) espera o resultado OU a mensagem de "não encontrado" (esperas automáticas do Playwright)
        result = page.locator("PREENCHER: seletor do bloco de resultado")
        not_found = page.locator("PREENCHER: seletor da mensagem de nenhum resultado")
        error = page.locator("PREENCHER: seletor de mensagem de erro do site")
        result.or_(not_found).or_(error).first.wait_for(state="visible", timeout=self.timeout_ms)

        if error.count() and error.first.is_visible():
            raise TransientError(error.first.inner_text())     # o motor repete com espera progressiva
        if not_found.count() and not_found.first.is_visible():
            return SearchResult.not_found(not_found.first.inner_text())  # não é erro

        # 4) extração padronizada
        return SearchResult.found_with({
            "campo1": result.locator("PREENCHER").first.inner_text().strip(),
            "campo2": result.locator("PREENCHER").first.inner_text().strip(),
        })

    # Login manual assistido (somente se requires_login=True)
    def is_logged_in(self) -> bool:
        if not self.requires_login:
            return True
        self.page.goto(self.options["search_url"], wait_until="domcontentloaded")
        return self.page.locator("PREENCHER: elemento que só aparece logado").count() > 0

    def begin_login(self) -> str:
        if self.options.get("login_url"):
            self.page.goto(self.options["login_url"])
        self.browser.bring_to_front()
        return "Faça login na janela do navegador e clique em CONTINUAR."
