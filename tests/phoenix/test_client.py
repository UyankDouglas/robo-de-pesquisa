"""Cliente Playwright do Phoenix2 contra o SIMULADOR local (fluxo e locators por texto).

Estes testes provam a lógica de navegação, paginação, detalhes e esperas — não a integração com o
Phoenix2 real, que depende do HTML real e de sessão autorizada.
"""
from __future__ import annotations

import pytest

from core.browser import BrowserManager
from core.models import SessionExpiredError
from phoenix.client import PhoenixClient
from phoenix.config import load_selectors
from phoenix_simulator.server import PASSWORD, USER, PhoenixSimulator
from tests.phoenix.fixtures import C_MIXED, C_MULTI, C_NONE, C_PAGED, C_SINGLE, C_ZEROS, DATA

pytestmark = pytest.mark.browser


def login(page, base_url: str) -> None:
    """Simula o operador fazendo o login manual (no uso real é a pessoa que digita)."""
    page.goto(base_url)
    page.fill("input[name=u]", USER)
    page.fill("input[name=p]", PASSWORD)
    page.click("input[type=submit]")
    page.wait_for_load_state("domcontentloaded")


@pytest.fixture(params=[False, True], ids=["pagina", "iframe"])
def session(request):
    with PhoenixSimulator(DATA, use_iframe=request.param) as sim, \
            BrowserManager(headless=True, channel="msedge", timeout_seconds=15) as browser:
        client = PhoenixClient(lambda: browser.page, load_selectors(), sim.base_url, timeout_seconds=15)
        assert not client.is_logged_in()
        login(browser.page, sim.base_url)
        assert client.is_logged_in()
        yield sim, browser, client


def tickets(result):
    return [(s["id"], b["numero_portar"], b["status_bilhete"], b["motivo_recusa"])
            for s in result["solicitacoes"] for b in s["bilhetes"]]


def test_single_request_and_menu_navigation(session):
    sim, _, client = session
    result = client.consultar(C_SINGLE)
    assert tickets(result) == [("pag1-sol1", "(81) 99999-0001", "Em andamento", "-")]
    sol = result["solicitacoes"][0]["dados"]
    assert sol["Status Solicitação"] == "Concluída" and sol["Data Solicitação"] == "01/09/2026 10:00"
    assert set(result["solicitacoes"][0]["bilhetes"][0]) == {
        "numero_portar", "bilhete", "numero_temporario", "status_bilhete", "autorizacao_doadora",
        "autorizado_ea", "motivo_recusa", "status_linha", "motivo_falha"}


def test_multiple_requests_and_tickets(session):
    _, _, client = session
    multi = client.consultar(C_MULTI)
    assert [s["id"] for s in multi["solicitacoes"]] == ["pag1-sol1", "pag1-sol2", "pag1-sol3"]
    assert [b[2] for b in tickets(multi)] == ["Portado", "Em conflito", "Falha parcial / portada"]
    mixed = client.consultar(C_MIXED)
    assert len(mixed["solicitacoes"]) == 1 and len(mixed["solicitacoes"][0]["bilhetes"]) == 6
    assert ("pag1-sol1", "(85) 99999-0023", "Em conflito", "Informação disponibilizada pelo sistema.") in tickets(mixed)


def test_pagination_walks_every_page(session):
    sim, _, client = session
    result = client.consultar(C_PAGED)
    assert len(result["solicitacoes"]) == 12
    assert [s["pagina"] for s in result["solicitacoes"]] == [1] * 5 + [2] * 5 + [3] * 2
    assert {b[1] for b in tickets(result)} == {f"(11) 99999-00{d:02d}" for d in range(1, 13)}


def test_not_found_and_leading_zeros(session):
    sim, _, client = session
    assert client.consultar(C_NONE)["solicitacoes"] == []
    zeros = client.consultar(C_ZEROS)
    assert C_ZEROS.startswith("000") and len(zeros["solicitacoes"]) == 1
    assert sim.state.searches[-1] == C_ZEROS  # pesquisado com os 14 dígitos


def test_session_expired_is_detected(session):
    sim, _, client = session
    sim.state.expire_all()
    with pytest.raises(SessionExpiredError):
        client.consultar(C_SINGLE)
    assert not client.is_logged_in()


def test_sso_provider_page_means_expired_session(session):
    """O servidor real responde 401 com a página "Click Provider Link" quando não há sessão."""
    sim, _, client = session
    sim.state.sso = True
    sim.state.expire_all()
    started = __import__("time").monotonic()
    with pytest.raises(SessionExpiredError, match="SSO"):
        client.consultar(C_SINGLE)
    assert __import__("time").monotonic() - started < 10  # detecta na hora, sem esperar o timeout
