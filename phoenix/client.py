"""Automação do Phoenix2 Business (Playwright, API síncrona).

Nenhum seletor CSS específico do site é usado: os elementos são encontrados pelos textos visíveis
configurados em config/phoenix/selectors.json (menus, rótulos, nomes de colunas), procurando em
todos os frames da página (aplicações legadas costumam usar frames/iframes). Os resultados só são
aceitos depois de confirmar que a tabela exibida é do CNPJ pesquisado.
"""
from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any, Callable

from core.models import HumanVerificationRequired, SessionExpiredError, TransientError
from phoenix.normalize import mask, normalize_label
from utils.logger import get_logger

log = get_logger("phoenix.client")

_JS_NORM = r"""const norm = s => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
  .replace(/[:*]/g, ' ').replace(/\s+/g, ' ').trim().toLowerCase();"""

# Procura a tabela cujo cabeçalho contém todas as colunas pedidas e devolve cabeçalho + linhas.
JS_FIND_TABLE = _JS_NORM + r"""
(args) => {
  const required = args.required.map(norm);
  const tables = Array.from(document.querySelectorAll('table'));
  for (let ti = 0; ti < tables.length; ti++) {
    const t = tables[ti];
    const rows = Array.from(t.rows);
    for (let h = 0; h < Math.min(rows.length, 6); h++) {
      const texts = Array.from(rows[h].cells).map(c => norm(c.innerText));
      if (!required.every(r => texts.includes(r))) continue;
      const headers = Array.from(rows[h].cells).map(c => (c.innerText || '').replace(/\s+/g, ' ').trim());
      const data = [];
      for (let i = h + 1; i < rows.length; i++) {
        const r = rows[i];
        const cells = Array.from(r.cells).map(c => (c.innerText || '').replace(/\s+/g, ' ').trim());
        const radio = r.querySelector('input[type=radio]');
        if (!radio && cells.every(v => !v)) continue;
        if (cells.length === 1 && !radio) continue;  // linhas de mensagem/paginação dentro da tabela
        data.push({cells, rowIndex: i, hasRadio: !!radio});
      }
      return {headers, rows: data, tableIndex: ti};
    }
  }
  return null;
}"""

# Controle (input/select) associado a um rótulo: <label for>, ou o primeiro controle visível depois do texto.
JS_CONTROL_AFTER_LABEL = _JS_NORM + r"""
(args) => {
  const want = norm(args.label), tag = args.tag;
  const visible = c => c && c.type !== 'hidden' && !!(c.offsetWidth || c.offsetHeight || c.getClientRects().length);
  for (const l of document.querySelectorAll('label')) {
    if (norm(l.textContent) !== want) continue;
    const c = l.htmlFor ? document.getElementById(l.htmlFor) : l.querySelector(tag);
    if (c && c.tagName.toLowerCase() === tag && visible(c)) return c;
  }
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (norm(node.nodeValue) !== want) continue;
    const after = Array.from(document.querySelectorAll(tag)).find(
      c => (node.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING) && visible(c));
    if (after) return after;
  }
  return null;
}"""

JS_SELECT_WITH_OPTION = _JS_NORM + r"""
(args) => {
  const want = norm(args.option);
  for (const s of document.querySelectorAll('select')) {
    const opt = Array.from(s.options).find(o => norm(o.text) === want);
    if (opt) return {found: true};
  }
  return {found: false};
}"""

# Quais textos aparecem visíveis no frame (innerText ignora elementos ocultos) e se há campo de senha.
JS_PAGE_STATE = _JS_NORM + r"""
(texts) => {
  const body = document.body ? norm(document.body.innerText) : '';
  const password = Array.from(document.querySelectorAll('input[type=password]'))
    .some(i => !!(i.offsetWidth || i.offsetHeight || i.getClientRects().length));
  return {found: texts.filter(t => body.includes(norm(t))), password, title: norm(document.title)};
}"""

# Elemento visível cujo texto PRÓPRIO (nós de texto diretos, sem os filhos) é igual ao procurado —
# cobre menus do tipo <li>Portabilidade<ul>...</ul></li>.
JS_OWN_TEXT = _JS_NORM + r"""
(text) => {
  const want = norm(text);
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  return Array.from(document.querySelectorAll('body *')).filter(el => {
    const own = Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.nodeValue).join(' ');
    return norm(own) === want && visible(el);
  }).slice(0, 10);
}"""

JS_CLICK_RADIO = r"""
([ti, ri]) => {
  const t = document.querySelectorAll('table')[ti];
  const radio = t && t.rows[ri] ? t.rows[ri].querySelector('input[type=radio]') : null;
  if (!radio) return false;
  radio.click();
  return radio.checked;
}"""

JS_OPTION_VALUE = _JS_NORM + r"""
(el, option) => {
  const opt = Array.from(el.options || []).find(o => norm(o.text) === norm(option));
  return opt ? opt.value : null;
}"""


class PhoenixClient:
    def __init__(self, page_provider: Callable[[], Any], selectors: dict[str, Any], base_url: str,
                 timeout_seconds: float = 45.0, max_pages: int = 50):
        self._page_provider = page_provider
        self.sel = selectors
        self.base_url = base_url
        self.timeout = timeout_seconds
        self.max_pages = max_pages
        self._titles: set[str] = set()

    # ============================================================ utilidades
    @property
    def page(self):
        return self._page_provider()

    def _frames(self, page=None) -> list:
        page = page or self.page
        main = page.main_frame
        return [main] + [f for f in page.frames if f is not main]

    def _page_state(self, texts: list[str], page=None) -> tuple[set[str], bool]:
        """Textos visíveis encontrados (em qualquer frame) e se há campo de senha visível."""
        found, password = set(), False
        self._titles: set[str] = set()
        for frame in self._frames(page):
            try:
                state = frame.evaluate(JS_PAGE_STATE, texts)
            except Exception:
                continue
            found.update(state["found"])
            password = password or state["password"]
            self._titles.add(state["title"])
        return found, password

    def _visible_text(self, text: str, page=None) -> bool:
        return bool(self._page_state([text], page)[0])

    def _candidates(self, text: str, page=None):
        """Botões, links e elementos visíveis com o texto exato, em todos os frames (em ordem de preferência)."""
        for frame in self._frames(page):
            for make in (lambda f: f.get_by_role("button", name=text, exact=True),
                         lambda f: f.get_by_role("link", name=text, exact=True),
                         lambda f: f.get_by_text(text, exact=True)):
                try:
                    loc = make(frame)
                    for i in range(min(loc.count(), 10)):
                        if loc.nth(i).is_visible():
                            yield loc.nth(i)
                except Exception:
                    continue
        for frame in self._frames(page):
            try:
                handles = frame.evaluate_handle(JS_OWN_TEXT, text).get_properties()
            except Exception:
                continue
            for handle in handles.values():
                element = handle.as_element()
                if element:
                    yield element

    def _find_clickable(self, text: str, page=None):
        return next(self._candidates(text, page), None)

    def _hover_revealing(self, label: str, reveals: str) -> bool:
        """Passa o mouse em cada elemento com o texto até um deles revelar o submenu esperado
        (a mesma palavra pode aparecer fora do menu, por exemplo numa coluna da tabela)."""
        pause = int(self.sel["menu"].get("espera_hover_ms", 400))
        for candidate in list(self._candidates(label)):
            try:
                candidate.hover()
            except Exception:
                continue
            self.page.wait_for_timeout(pause)
            if self._find_clickable(reveals):
                return True
        return False

    def _wait(self, probe: Callable[[], Any], what: str, timeout: float | None = None):
        """Repete ``probe`` até devolver algo verdadeiro (tolerante a recarregamentos de frame)."""
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            self.check_blockers()
            try:
                value = probe()
            except (SessionExpiredError, HumanVerificationRequired):
                raise
            except Exception:
                value = None
            if value:
                return value
            if time.monotonic() > deadline:
                raise TransientError(f"Tempo esgotado aguardando: {what}")
            self.page.wait_for_timeout(250)

    def _find_table(self, required: list[str], page=None) -> tuple[Any, dict] | None:
        for frame in self._frames(page):
            try:
                found = frame.evaluate(JS_FIND_TABLE, {"required": required})
            except Exception:
                continue
            if found:
                return frame, found
        return None

    def _control(self, label: str, tag: str):
        for frame in self._frames():
            try:
                handle = frame.evaluate_handle(JS_CONTROL_AFTER_LABEL, {"label": label, "tag": tag}).as_element()
            except Exception:
                handle = None
            if handle:
                return frame, handle
        return None

    # ============================================================ sessão
    def check_blockers(self, page=None) -> None:
        login = self.sel["login"]
        captcha, expired = login["textos_captcha"], login["textos_sessao_expirada"]
        found, password = self._page_state(captcha + expired, page)
        if found & set(captcha):
            raise HumanVerificationRequired("O Phoenix2 exibiu uma verificação humana (CAPTCHA)")
        if found & set(expired):
            raise SessionExpiredError(f"O Phoenix2 informou: {sorted(found & set(expired))[0]}")
        if password and login.get("indicadores_pagina_login"):
            raise SessionExpiredError("Página de login exibida: sessão inexistente ou expirada")
        titles = {normalize_label(t) for t in login.get("titulos_pagina_login", [])}
        if titles & {normalize_label(t) for t in self._titles}:  # título de qualquer frame
            raise SessionExpiredError("Página de autenticação (SSO) exibida: sessão inexistente ou expirada")

    def open_home(self, force: bool = False) -> None:
        page = self.page
        if force or not page.url.startswith(self.base_url.split("?")[0].rstrip("/")):
            page.goto(self.base_url, wait_until="domcontentloaded")

    def is_logged_in(self) -> bool:
        try:
            self.open_home(force=True)  # recarrega: a tela pode ainda mostrar o login antigo
            self.check_blockers()
        except (SessionExpiredError, HumanVerificationRequired):
            return False
        return self._find_clickable(self.sel["login"]["indicador_logado_texto"]) is not None

    # ============================================================ navegação
    def on_status_page(self) -> bool:
        return (self._visible_text(self.sel["consulta"]["titulo_tela"])
                and self._find_clickable(self.sel["consulta"]["botao_pesquisar"]) is not None)

    def go_to_status_page(self) -> None:
        """Portabilidade -> Acompanhamento -> Status Solicitações (menu por hover)."""
        menu = self.sel["menu"]
        self.open_home()
        self._wait(lambda: self._hover_revealing(menu["portabilidade"], menu["acompanhamento"]),
                   "menu Portabilidade -> Acompanhamento")
        self._wait(lambda: self._hover_revealing(menu["acompanhamento"], menu["status_solicitacoes"]),
                   "submenu Acompanhamento -> Status Solicitações", 15)
        item = self._wait(lambda: self._find_clickable(menu["status_solicitacoes"]), "item Status Solicitações", 10)
        item.click()
        self._wait(self.on_status_page, "tela Status das Solicitações")

    # ============================================================ consulta
    def search(self, cnpj: str) -> dict | None:
        """Pesquisa um CNPJ. Devolve a tabela da 1ª página ou None se não houver solicitações."""
        c = self.sel["consulta"]
        self.go_to_status_page()
        found = self._control(c["rotulo_tipo_consulta"], "select")
        if not found or not found[1].evaluate(JS_OPTION_VALUE, c["opcao_cnpj"]):
            found = None
            for frame in self._frames():  # alternativa: o único select que tem a opção "CNPJ"
                if frame.evaluate(JS_SELECT_WITH_OPTION, {"option": c["opcao_cnpj"]})["found"]:
                    selects = frame.locator("select")
                    for i in range(selects.count()):
                        handle = selects.nth(i).element_handle()
                        if handle.evaluate(JS_OPTION_VALUE, c["opcao_cnpj"]):
                            found = (frame, handle)
                            break
                if found:
                    break
        if not found:
            raise TransientError(f"Campo '{c['rotulo_tipo_consulta']}' com a opção '{c['opcao_cnpj']}' não encontrado")
        found[1].select_option(value=found[1].evaluate(JS_OPTION_VALUE, c["opcao_cnpj"]))

        value = self._control(c["rotulo_valor"], "input")
        if not value:
            raise TransientError(f"Campo '{c['rotulo_valor']}' não encontrado")
        value[1].fill(cnpj)
        for label in c.get("rotulos_periodo", []):  # períodos ficam vazios
            period = self._control(label, "input")
            if period and period[1].input_value():
                period[1].fill("")

        button = self._find_clickable(c["botao_pesquisar"])
        if not button:
            raise TransientError("Botão Pesquisar não encontrado")
        button.click()
        return self._wait(lambda: self._search_outcome(cnpj), f"resultado da pesquisa do CNPJ {mask(cnpj)}")

    def _search_outcome(self, cnpj: str):
        c, r = self.sel["consulta"], self.sel["resultados"]
        errors = list(c.get("textos_erro", []))
        found_texts, _ = self._page_state([c["texto_sem_resultado"]] + errors)
        if c["texto_sem_resultado"] in found_texts:
            return {"empty": True}
        found = self._find_table(r["colunas_identificacao"])
        if not found:
            if found_texts & set(errors):  # só sem tabela: o texto pode aparecer dentro dos resultados
                raise TransientError(f"O Phoenix2 exibiu um erro: {sorted(found_texts & set(errors))[0]}")
            return None
        frame, table = found
        cnpj_col = self._col(table["headers"], "CNPJ")
        if cnpj_col is not None:
            digits = {re.sub(r"\D", "", row["cells"][cnpj_col]).zfill(14)
                      for row in table["rows"] if cnpj_col < len(row["cells"]) and row["cells"][cnpj_col]}
            if digits and digits != {cnpj}:
                return None  # ainda é a tabela de outra pesquisa (ou resultado inesperado)
        return {"empty": False, "table": table}

    @staticmethod
    def _col(headers: list[str], name: str) -> int | None:
        wanted = normalize_label(name)
        return next((i for i, h in enumerate(headers) if normalize_label(h) == wanted), None)

    def _row_dict(self, headers: list[str], cells: list[str]) -> dict[str, str]:
        offset = max(len(cells) - len(headers), 0)
        out = {}
        for i, header in enumerate(headers):
            if header and i + offset < len(cells):
                out[header] = cells[i + offset]
        return out

    def _page_signature(self, table: dict) -> tuple:
        return tuple(tuple(row["cells"]) for row in table["rows"])

    def _next_page(self, current: dict) -> dict | None:
        r = self.sel["resultados"]
        control = next((b for b in (self._find_clickable(t) for t in r["textos_proxima_pagina"]) if b), None)
        if control is None:
            return None
        before = self._page_signature(current)
        control.click()

        def changed():
            found = self._find_table(r["colunas_identificacao"])
            return found[1] if found and self._page_signature(found[1]) != before else None

        try:
            return self._wait(changed, "próxima página de solicitações", 15)
        except TransientError:
            return None  # controle existe mas não muda a tabela: última página

    def list_requests(self, cnpj: str) -> list[dict]:
        """Todas as solicitações do CNPJ, percorrendo a paginação."""
        outcome = self.search(cnpj)
        if outcome["empty"]:
            return []
        pages = [outcome["table"]]
        seen = {self._page_signature(outcome["table"])}
        while len(pages) < self.max_pages:
            nxt = self._next_page(pages[-1])
            if not nxt or self._page_signature(nxt) in seen:
                break
            seen.add(self._page_signature(nxt))
            pages.append(nxt)
        requests = []
        for page_no, table in enumerate(pages, start=1):
            for position, row in enumerate(r for r in table["rows"] if r["hasRadio"]):
                requests.append({
                    "id": f"pag{page_no}-sol{position + 1}",
                    "pagina": page_no,
                    "posicao": position,
                    "dados": self._row_dict(table["headers"], row["cells"]),
                    "_signature": self._page_signature(table),
                })
        return requests

    # ============================================================ detalhes
    def _goto_listing_page(self, cnpj: str, request: dict) -> dict:
        """Garante que a listagem exibida é a página da solicitação (volta ou repete a pesquisa)."""
        r = self.sel["resultados"]
        found = self._find_table(r["colunas_identificacao"])
        if found and self._page_signature(found[1]) == request["_signature"]:
            return found[1]
        outcome = self.search(cnpj)
        table = outcome.get("table") if not outcome["empty"] else None
        for _ in range(request["pagina"] - 1):
            table = self._next_page(table) if table else None
        if not table or self._page_signature(table) != request["_signature"]:
            raise TransientError("A listagem de solicitações mudou durante a consulta")
        return table

    def open_details(self, cnpj: str, request: dict):
        """Seleciona a solicitação e clica em Detalhar. Devolve a página onde está o detalhe."""
        r = self.sel["resultados"]
        table = self._goto_listing_page(cnpj, request)
        frame, _ = self._find_table(r["colunas_identificacao"])
        row = [x for x in table["rows"] if x["hasRadio"]][request["posicao"]]
        if not frame.evaluate(JS_CLICK_RADIO, [table["tableIndex"], row["rowIndex"]]):
            raise TransientError("Não foi possível selecionar a solicitação na listagem")
        button = self._find_clickable(r["botao_detalhar"])
        if not button:
            raise TransientError("Botão Detalhar não encontrado")
        context = self.page.context
        before = list(context.pages)
        button.click()
        ident = self.sel["detalhe"]["colunas_identificacao"]

        def detail_page():
            for p in list(context.pages):
                if p not in before and self._find_table(ident, p):
                    return p  # detalhe aberto em nova janela
            return self.page if self._find_table(ident) else None

        return self._wait(detail_page, "página Detalhes Solicitação")

    def extract_tickets(self, page=None) -> list[dict[str, str]]:
        d = self.sel["detalhe"]
        found = self._find_table(d["colunas_identificacao"], page)
        if not found:
            raise TransientError("Tabela de bilhetes não encontrada")
        _, table = found
        index = {key: self._col(table["headers"], label) for key, label in d["colunas_bilhetes"].items()}
        missing = [d["colunas_bilhetes"][k] for k, v in index.items() if v is None]
        tickets = []
        for row in table["rows"]:
            cells = row["cells"]
            offset = max(len(cells) - len(table["headers"]), 0)
            ticket = {key: (cells[i + offset] if i is not None and i + offset < len(cells) else "")
                      for key, i in index.items()}
            if any(ticket.values()):
                tickets.append(ticket)
        if missing:
            log.warning("Colunas não encontradas na tabela Bilhetes: %s", ", ".join(missing))
        return tickets

    def back_from_details(self, detail_page) -> None:
        if detail_page is not self.page:
            detail_page.close()
            return
        back = self._find_clickable(self.sel["detalhe"]["botao_voltar"])
        try:
            if back:
                back.click()
            else:
                self.page.go_back(wait_until="domcontentloaded")
        except Exception as exc:  # a próxima solicitação refaz a pesquisa se necessário
            log.debug("Não foi possível voltar da página de detalhes: %s", exc)

    # ============================================================ fluxo completo
    def consultar(self, cnpj: str) -> dict[str, Any]:
        requests = self.list_requests(cnpj)
        log.info("CNPJ %s: %d solicitação(ões) encontrada(s).", mask(cnpj), len(requests))
        for request in requests:
            detail = self.open_details(cnpj, request)
            request["bilhetes"] = self.extract_tickets(detail)
            log.info("  solicitação %s: %d bilhete(s).", request["id"], len(request["bilhetes"]))
            self.back_from_details(detail)
        for request in requests:
            request.pop("_signature", None)
        return {"cnpj": cnpj, "consultado_em": dt.datetime.now().isoformat(timespec="seconds"),
                "solicitacoes": requests}
