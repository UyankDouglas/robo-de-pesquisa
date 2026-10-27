"""Simulador LOCAL e FICTÍCIO do fluxo Phoenix2 (somente para testes automatizados).

Reproduz o fluxo descrito pelo operador — login, menu horizontal com hover (Portabilidade ->
Acompanhamento -> Status Solicitações), formulário "Consultar Bilhetes", resultados com radio,
paginação, "Detalhar", página "Detalhes Solicitação" com a seção "Bilhetes" e sessão que expira.
O HTML é inventado e NÃO representa o HTML real do Phoenix2; o cliente não usa nenhum seletor
específico daqui (só os textos visíveis), por isso o teste aqui não substitui a validação real.
"""
from __future__ import annotations

import html
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BASE = "/phoenix2Business/"
USER, PASSWORD = "operador", "simulador"
COOKIE = "sim_phx_session"
PAGE_SIZE = 5
TICKET_COLUMNS = ["Número a portar", "Bilhete", "Número Temporário", "Status do Bilhete", "Autorização Doadora",
                  "Autorizado pela EA", "Motivo Recusa", "Status da Linha", "Motivo da Falha"]

STYLE = """
body{font-family:Verdana,Arial;font-size:12px;margin:0}
.menu{background:#003a8c;margin:0;padding:0;list-style:none;display:flex}
.menu li{position:relative;padding:8px 14px;color:#fff;cursor:pointer}
.menu li ul{display:none;position:absolute;top:100%;left:0;background:#0b57c2;list-style:none;padding:0;min-width:200px}
.menu li ul li ul{top:0;left:100%}
.menu li:hover>ul{display:block}
.menu a{color:#fff;text-decoration:none}
table.grid{border-collapse:collapse;margin:8px}
table.grid td,table.grid th{border:1px solid #999;padding:3px 6px;white-space:nowrap}
.scroll{overflow-x:auto;width:600px}
"""


@dataclass
class SimState:
    data: dict[str, list[dict[str, Any]]]
    use_iframe: bool = False
    latency: float = 0.05
    session_ttl: float = 0.0
    sso: bool = False  # sem sessão: página "Click Provider Link" (como o servidor real responde)
    sessions: dict[str, float] = field(default_factory=dict)
    searches: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def expire_all(self) -> None:
        with self.lock:
            self.sessions.clear()


def fmt_cnpj(c: str) -> str:
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}" if len(c) == 14 and c.isdigit() else c


def fmt_phone(n: str) -> str:
    return f"({n[:2]}) {n[2:7]}-{n[7:]}" if len(n) == 11 else n


class Handler(BaseHTTPRequestHandler):
    state: SimState

    def log_message(self, *args) -> None:
        pass

    # ------------------------------------------------------------- base
    def _send(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _page(self, title: str, body: str) -> str:
        return f"<html><head><title>{title}</title><style>{STYLE}</style></head><body>{body}</body></html>"

    def _token(self) -> str:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return jar[COOKIE].value if COOKIE in jar else ""

    def _logged(self) -> bool:
        with self.state.lock:
            expires = self.state.sessions.get(self._token())
        return expires is not None and (expires == 0 or time.time() < expires)

    def _login_page(self, msg: str = "") -> None:
        body = (f"<h2>Phoenix2 Business (SIMULADOR)</h2><p>{msg}</p>"
                f"<form method='post' action='{BASE}login'>Usuário: <input name='u'><br>"
                "Senha: <input type='password' name='p'><br><input type='submit' value='Entrar'></form>")
        self._send(self._page("Login", body))

    def _menu(self, target: str) -> str:
        t = f" target='{target}'" if target else ""
        return ("<ul class='menu'><li>Pedidos<ul><li>Novo</li></ul></li><li>Consultas<ul><li>Clientes</li></ul></li>"
                "<li>Portabilidade<ul><li>Cadastro</li><li>Acompanhamento<ul>"
                f"<li><a href='{BASE}status'{t}>Status Solicitações</a></li><li>Relatórios</li></ul></li></ul></li>"
                "<li>Pré Cadastro de Pedido<ul><li>Incluir</li></ul></li></ul>")

    # ------------------------------------------------------------- rotas
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if self.path.startswith(BASE + "login"):
            if form.get("u", [""])[0] == USER and form.get("p", [""])[0] == PASSWORD:
                token = secrets.token_hex(8)
                with self.state.lock:
                    self.state.sessions[token] = time.time() + self.state.session_ttl if self.state.session_ttl else 0
                return self._send("", 302, {"Location": BASE, "Set-Cookie": f"{COOKIE}={token}; Path=/"})
            return self._login_page("Usuário ou senha inválidos.")
        self._send("não encontrado", 404)

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if not url.path.startswith(BASE):
            return self._send("", 302, {"Location": BASE})
        if not self._logged():
            if self.state.sso and "form" not in q:
                return self._send(f"<html><head><title>Click Provider Link</title></head><body>"
                                  f"<a href='{BASE}?form=1'>Provedor de identidade</a></body></html>", 401)
            return self._login_page("Sua sessão expirou. Efetue o login novamente." if self._token() else "")
        route = url.path[len(BASE):]
        if route == "":
            if self.state.use_iframe:
                body = self._menu("conteudo") + "<iframe name='conteudo' src='about:blank' style='width:100%;height:600px;border:0'></iframe>"
            else:
                body = self._menu("") + "<p>Bem-vindo ao Phoenix2 Business (simulador).</p>"
            return self._send(self._page("Phoenix2", body))
        if route == "status":
            return self._status(q)
        if route == "detalhe":
            return self._detail(q)
        self._send("não encontrado", 404)

    def _status(self, q: dict[str, str]) -> None:
        menu = "" if self.state.use_iframe else self._menu("")
        form = (f"<h3>Status das Solicitações</h3><form method='get' action='{BASE}status'><b>Consultar Bilhetes</b>"
                "<table><tr><td>Tipo da Consulta:</td><td><select name='tipo'><option value=''>Selecione</option>"
                "<option value='1'>CNPJ</option><option value='2'>Número</option><option value='3'>Bilhete</option></select></td></tr>"
                "<tr><td>Valor:</td><td><input name='valor' size='20'></td></tr>"
                "<tr><td>Período inicial:</td><td><input name='dtini'></td><td>Período final:</td><td><input name='dtfim'></td></tr>"
                "<tr><td colspan='2'><input type='submit' value='Pesquisar'></td></tr></table></form>")
        result = ""
        if q.get("valor") is not None and q.get("tipo"):
            time.sleep(self.state.latency)
            cnpj = "".join(ch for ch in q["valor"] if ch.isdigit())
            with self.state.lock:
                self.state.searches.append(cnpj)
            sols = self.state.data.get(cnpj, []) if q["tipo"] == "1" else []
            if not sols:
                result = "<p class='msg'>Nenhuma Solicitação foi encontrada.</p>"
            else:
                page = int(q.get("pag", "1"))
                pages = (len(sols) + PAGE_SIZE - 1) // PAGE_SIZE
                rows = "".join(
                    f"<tr><td><input type='radio' name='sel' value='{cnpj}:{i}'></td><td>{html.escape(s['razao'])}</td>"
                    f"<td>{fmt_cnpj(cnpj)}</td><td>{s['data']}</td><td>{s['tipo']}</td><td>{s['status']}</td>"
                    f"<td>{s['port_in']}</td></tr>"
                    for i, s in list(enumerate(sols))[(page - 1) * PAGE_SIZE: page * PAGE_SIZE])
                nav = []
                base_q = f"tipo=1&valor={cnpj}"
                if page > 1:
                    nav.append(f"<a href='{BASE}status?{base_q}&pag={page - 1}'>Anterior</a>")
                if page < pages:
                    nav.append(f"<a href='{BASE}status?{base_q}&pag={page + 1}'>Próxima</a>")
                result = (f"<form method='get' action='{BASE}detalhe' onsubmit=\"if(!this.querySelector('input[name=sel]:checked'))"
                          "{alert('Selecione uma solicitação');return false;}\"><input type='hidden' name='pag' value='" + str(page) + "'>"
                          "<div class='scroll'><table class='grid'><tr><th></th><th>Razão Social</th><th>CNPJ</th>"
                          "<th>Data Solicitação</th><th>Tipo</th><th>Status Solicitação</th><th>Tipo Port In</th></tr>"
                          f"{rows}</table></div><p>Página {page} de {pages} {' | '.join(nav)}</p>"
                          "<input type='submit' value='Detalhar'></form>")
        self._send(self._page("Status das Solicitações", menu + form + result))

    def _detail(self, q: dict[str, str]) -> None:
        cnpj, _, idx = q.get("sel", "").partition(":")
        sols = self.state.data.get(cnpj, [])
        if not idx.isdigit() or int(idx) >= len(sols):
            return self._send(self._page("Erro", "<p>Ocorreu um erro: solicitação inválida.</p>"))
        s = sols[int(idx)]
        menu = "" if self.state.use_iframe else self._menu("")
        tickets = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in (
            fmt_phone(t["numero"]), t["bilhete"], fmt_phone(t.get("temp", "")), t["status"], t.get("doadora", "Sim"),
            t.get("ea", "Sim"), t.get("motivo", "-"), t.get("linha", "Ativa"), t.get("falha", "-"))) + "</tr>"
            for t in s["bilhetes"])
        body = (f"<h3>Detalhes Solicitação</h3><h4>Dados Cliente</h4><p>{html.escape(s['razao'])} — {fmt_cnpj(cnpj)}</p>"
                "<h4>Dados Administrador</h4><p>Administrador fictício</p><h4>Janelas</h4><p>Janela 1: 08h-12h</p>"
                f"<h4>Solicitação de Port-In</h4><p>Data: {s['data']} — Tipo: {s['tipo']}</p><h4>Bilhetes</h4>"
                "<div class='scroll'><table class='grid'><tr>" + "".join(f"<th>{c}</th>" for c in TICKET_COLUMNS)
                + f"</tr>{tickets}</table></div>"
                f"<p><a href='{BASE}status?tipo=1&valor={cnpj}&pag={q.get('pag', '1')}'><button type='button'>Voltar</button></a></p>")
        self._send(self._page("Detalhes Solicitação", menu + body))


class PhoenixSimulator:
    def __init__(self, data: dict[str, list[dict[str, Any]]], use_iframe: bool = False, port: int = 0,
                 latency: float = 0.05, session_ttl: float = 0.0):
        self.state = SimState(data, use_iframe, latency, session_ttl)
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{BASE}"

    def start(self) -> "PhoenixSimulator":
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
