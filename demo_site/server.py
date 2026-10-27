"""Site local de demonstração (dados 100% fictícios) para testar o robô sem depender da internet.

Simula os comportamentos que um site real pode ter:

- pesquisa com resultado carregado dinamicamente (JavaScript + atraso);
- registros encontrados e "nenhum resultado encontrado";
- erro interno (identificador contendo ``ERRO``) e erro temporário que se resolve sozinho
  após 2 tentativas (``INSTAVEL``);
- resposta lenta para testar timeout (``LENTO``);
- verificação humana/CAPTCHA (``CAPTCHA``) que o operador resolve manualmente;
- login opcional (``--require-login``) com expiração de sessão (``--session-ttl``);
- limite de requisições por minuto com HTTP 429 (``--max-per-minute``);
- API JSON documentada em ``/api/search?q=...`` (usada pelo adaptador de API).

Uso manual:  python -m demo_site.server --port 8765 [--require-login] [--session-ttl 120]
Credenciais fictícias do login de demonstração: usuário ``demo`` / senha ``demo123``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import paths

DEMO_USER = "demo"
DEMO_PASSWORD = "demo123"
SESSION_COOKIE = "wsb_demo_session"
HUMAN_COOKIE = "wsb_demo_human"

DATASET: dict[str, dict[str, str]] = {
    "12345678000190": {
        "nome": "Empresa Fictícia Alfa LTDA",
        "situacao": "ATIVA",
        "data_registro": "15/03/2010",
        "municipio": "São Paulo",
        "uf": "SP",
        "detalhes": "Comércio varejista de artigos de demonstração",
    },
    "11222333000144": {
        "nome": "Beta Serviços de Teste S.A.",
        "situacao": "ATIVA",
        "data_registro": "02/08/2015",
        "municipio": "Belo Horizonte",
        "uf": "MG",
        "detalhes": "Serviços de exemplo para automação",
    },
    "00012345000101": {
        "nome": "Gama Zeros à Esquerda ME",
        "situacao": "SUSPENSA",
        "data_registro": "20/01/2001",
        "municipio": "Curitiba",
        "uf": "PR",
        "detalhes": "Registro usado para validar a preservação de zeros à esquerda",
    },
    "PROTOCOLO-2024-0001": {
        "nome": "Requerimento de demonstração",
        "situacao": "EM ANÁLISE",
        "data_registro": "10/05/2024",
        "municipio": "Recife",
        "uf": "PE",
        "detalhes": "Protocolo fictício",
    },
}
NOT_FOUND = {"98765432000110", "00000000000000"}

_GENERATED_NAMES = ["Delta", "Épsilon", "Zeta", "Eta", "Teta", "Iota", "Kappa", "Lambda", "Ômicron", "Sigma"]
_GENERATED_STATUS = ["ATIVA", "ATIVA", "ATIVA", "BAIXADA", "SUSPENSA", "INAPTA"]
_GENERATED_CITIES = [("Porto Alegre", "RS"), ("Salvador", "BA"), ("Fortaleza", "CE"), ("Goiânia", "GO"), ("Manaus", "AM"), ("Vitória", "ES")]


def lookup(identifier: str) -> dict[str, str] | None:
    """Base fictícia: alguns registros fixos e os demais gerados de forma determinística."""
    key = identifier.strip().upper()
    if key in DATASET:
        return DATASET[key]
    if key in NOT_FOUND or key.startswith("NF") or "INEXISTENTE" in key:
        return None
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    special = any(token in key for token in ("INSTAVEL", "LENTO", "CAPTCHA"))
    if digest[0] % 4 == 0 and not special:  # ~25% sem resultado
        return None
    city, uf = _GENERATED_CITIES[digest[2] % len(_GENERATED_CITIES)]
    return {
        "nome": f"{_GENERATED_NAMES[digest[1] % len(_GENERATED_NAMES)]} Demonstração {digest[3]:03d} LTDA",
        "situacao": _GENERATED_STATUS[digest[4] % len(_GENERATED_STATUS)],
        "data_registro": f"{1 + digest[5] % 28:02d}/{1 + digest[6] % 12:02d}/{1990 + digest[7] % 34}",
        "municipio": city,
        "uf": uf,
        "detalhes": "Registro gerado automaticamente (fictício)",
    }


@dataclass
class DemoOptions:
    require_login: bool = False
    session_ttl: float = 0.0          # 0 = sessão não expira
    max_per_minute: int = 0           # 0 = sem limite
    latency: float = 0.15             # atraso de toda pesquisa (s)
    slow_seconds: float = 20.0        # atraso de identificadores com "LENTO"
    flaky_failures: int = 2           # falhas antes de "INSTAVEL" funcionar


@dataclass
class DemoState:
    options: DemoOptions
    sessions: dict[str, float] = field(default_factory=dict)   # token -> expira em (0 = nunca)
    humans: set[str] = field(default_factory=set)
    flaky_counts: dict[str, int] = field(default_factory=dict)
    requests: deque = field(default_factory=deque)
    search_count: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "WSBDemo/1.0"
    state: DemoState  # definido na subclasse criada por make_server

    # ---------------------------------------------------------------- utilidades
    def log_message(self, format: str, *args) -> None:  # silencioso
        pass

    def _cookies(self) -> dict[str, str]:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        return {k: v.value for k, v in jar.items()}

    def _session_valid(self) -> bool:
        token = self._cookies().get(SESSION_COOKIE)
        with self.state.lock:
            expires = self.state.sessions.get(token or "")
        if expires is None:
            return False
        return expires == 0 or time.time() < expires

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, headers: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", headers)

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        headers = {"Location": location}
        if cookie:
            headers["Set-Cookie"] = cookie
        self._send(HTTPStatus.FOUND, b"", "text/plain", headers)

    def _page(self, name: str, replacements: dict[str, str] | None = None) -> None:
        html = (paths.DEMO_STATIC_DIR / name).read_text(encoding="utf-8")
        for key, value in (replacements or {}).items():
            html = html.replace("{{" + key + "}}", value)
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")

    # ---------------------------------------------------------------- rotas
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path == "/health":
            return self._send(HTTPStatus.OK, b"ok", "text/plain")
        if url.path in ("/", "/index.html"):
            if self.state.options.require_login and not self._session_valid():
                return self._redirect("/login?expired=1" if SESSION_COOKIE in self._cookies() else "/login")
            user_bar = ""
            if self.state.options.require_login:
                user_bar = (f'<span class="user">Conectado como <b data-testid="logged-user">{DEMO_USER}</b> · '
                            f'<a href="/logout" data-testid="logout">Sair</a></span>')
            return self._page("index.html", {"USER_BAR": user_bar})
        if url.path == "/login":
            message = ""
            if "expired" in query:
                message = '<p class="warn" data-testid="login-message">Sua sessão expirou. Entre novamente.</p>'
            if "invalid" in query:
                message = '<p class="error" data-testid="login-message">Usuário ou senha inválidos.</p>'
            return self._page("login.html", {"MESSAGE": message})
        if url.path == "/logout":
            token = self._cookies().get(SESSION_COOKIE, "")
            with self.state.lock:
                self.state.sessions.pop(token, None)
            return self._redirect("/login", f"{SESSION_COOKIE}=; Path=/; Max-Age=0")
        if url.path == "/api/search":
            return self._api_search(query.get("q", [""])[0])
        if url.path.startswith("/static/"):
            target = (paths.DEMO_STATIC_DIR / url.path.removeprefix("/static/")).resolve()
            if target.is_file() and paths.DEMO_STATIC_DIR.resolve() in target.parents:
                kind = "text/css" if target.suffix == ".css" else "application/javascript" if target.suffix == ".js" else "application/octet-stream"
                return self._send(HTTPStatus.OK, target.read_bytes(), f"{kind}; charset=utf-8")
        self._send(HTTPStatus.NOT_FOUND, b"Pagina nao encontrada", "text/plain")

    def do_POST(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        if url.path == "/login":
            form = urllib.parse.parse_qs(body)
            user = form.get("username", [""])[0]
            password = form.get("password", [""])[0]
            if user == DEMO_USER and password == DEMO_PASSWORD:
                token = secrets.token_urlsafe(16)
                ttl = self.state.options.session_ttl
                with self.state.lock:
                    self.state.sessions[token] = time.time() + ttl if ttl else 0
                return self._redirect("/", f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax")
            return self._redirect("/login?invalid=1")
        if url.path == "/api/human-check":
            token = secrets.token_urlsafe(8)
            with self.state.lock:
                self.state.humans.add(token)
            return self._json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": f"{HUMAN_COOKIE}={token}; Path=/; SameSite=Lax"})
        self._send(HTTPStatus.NOT_FOUND, b"Pagina nao encontrada", "text/plain")

    # ---------------------------------------------------------------- pesquisa
    def _rate_limited(self) -> float | None:
        limit = self.state.options.max_per_minute
        if not limit:
            return None
        now = time.time()
        with self.state.lock:
            window = self.state.requests
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= limit:
                return max(1.0, 60 - (now - window[0]))
            window.append(now)
        return None

    def _api_search(self, raw_query: str) -> None:
        options = self.state.options
        q = raw_query.strip()
        key = q.upper()
        with self.state.lock:
            self.state.search_count += 1
        if options.require_login and not self._session_valid():
            return self._json(HTTPStatus.UNAUTHORIZED, {"status": "session_expired", "message": "Sessão expirada ou inexistente"})
        retry_after = self._rate_limited()
        if retry_after is not None:
            return self._json(HTTPStatus.TOO_MANY_REQUESTS,
                              {"status": "rate_limited", "retry_after": round(retry_after), "message": "Muitas consultas. Aguarde."},
                              {"Retry-After": str(int(round(retry_after)))})
        if not q:
            return self._json(HTTPStatus.BAD_REQUEST, {"status": "invalid", "message": "Informe um identificador"})
        if "CAPTCHA" in key:
            human = self._cookies().get(HUMAN_COOKIE)
            with self.state.lock:
                ok = human in self.state.humans
            if not ok:
                return self._json(HTTPStatus.FORBIDDEN, {"status": "captcha", "captcha": True,
                                                         "message": "Confirme que você é humano para continuar"})
        time.sleep(options.latency)
        if "LENTO" in key:
            time.sleep(options.slow_seconds)
        if "ERRO" in key:
            return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "message": "Erro interno simulado"})
        if "INSTAVEL" in key:
            with self.state.lock:
                count = self.state.flaky_counts.get(key, 0) + 1
                self.state.flaky_counts[key] = count
            if count <= options.flaky_failures:
                return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "error", "message": f"Serviço indisponível (falha simulada {count})"})
        record = lookup(q)
        if record is None:
            return self._json(HTTPStatus.OK, {"status": "not_found", "query": q, "message": f"Nenhum resultado encontrado para {q}"})
        return self._json(HTTPStatus.OK, {"status": "found", "query": q, "data": record})


class DemoServer:
    """Servidor em thread de fundo (usado pelos testes e pelo adaptador de demonstração)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, options: DemoOptions | None = None):
        self.state = DemoState(options or DemoOptions())
        handler = type("BoundDemoHandler", (DemoHandler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.host, self.port = self.httpd.server_address[:2]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> "DemoServer":
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="demo-site", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self) -> "DemoServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


_RUNNING: dict[int, DemoServer] = {}
_RUNNING_LOCK = threading.Lock()


def is_running(base_url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_server(base_url: str, options: DemoOptions | None = None) -> DemoServer | None:
    """Inicia o site de demonstração em segundo plano se ele ainda não estiver no ar."""
    if is_running(base_url):
        return None
    parts = urllib.parse.urlsplit(base_url)
    host, port = parts.hostname or "127.0.0.1", parts.port or 80
    if host not in ("127.0.0.1", "localhost"):
        raise RuntimeError("O servidor de demonstração só pode ser iniciado em 127.0.0.1/localhost")
    with _RUNNING_LOCK:
        if port in _RUNNING:
            return _RUNNING[port]
        server = DemoServer("127.0.0.1", port, options).start()
        _RUNNING[port] = server
        return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Site local de demonstração do Web Search Bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--require-login", action="store_true", help="exige login (demo / demo123)")
    parser.add_argument("--session-ttl", type=float, default=0, help="segundos até a sessão expirar (0 = não expira)")
    parser.add_argument("--max-per-minute", type=int, default=0, help="limite de pesquisas por minuto (0 = sem limite)")
    parser.add_argument("--latency", type=float, default=0.15)
    parser.add_argument("--slow-seconds", type=float, default=20)
    args = parser.parse_args(argv)
    options = DemoOptions(args.require_login, args.session_ttl, args.max_per_minute, args.latency, args.slow_seconds)
    server = DemoServer(args.host, args.port, options)
    print(f"Site de demonstração em {server.url}  (Ctrl+C para encerrar)")
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()


if __name__ == "__main__":
    main()
