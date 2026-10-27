"""Site local de demonstração e adaptador de API (sem navegador)."""
from __future__ import annotations

import pytest

from adapters.base_adapter import AdapterContext
from adapters.demo_adapter import DemoApiAdapter
from core.models import HumanVerificationRequired, RateLimitedError, TransientError
from demo_site.server import DemoOptions, DemoServer


def _api(server) -> DemoApiAdapter:
    adapter = DemoApiAdapter({"base_url": server.url, "autostart_server": False})
    adapter.open(AdapterContext(timeout_seconds=5))
    return adapter


def test_api_found_and_not_found(demo_server):
    api = _api(demo_server)
    found = api.search("12345678000190")
    assert found.found and found.data["nome"] == "Empresa Fictícia Alfa LTDA"
    assert api.search("00012345000101").data["uf"] == "PR"
    missing = api.search("98765432000110")
    assert not missing.found and "Nenhum resultado" in missing.message


def test_api_errors_are_classified(demo_server):
    api = _api(demo_server)
    with pytest.raises(TransientError):
        api.search("ERRO-1")
    with pytest.raises(TransientError):  # instável: falha 2x e depois responde
        api.search("INSTAVEL-9")
    with pytest.raises(TransientError):
        api.search("INSTAVEL-9")
    assert api.search("INSTAVEL-9").found
    with pytest.raises(HumanVerificationRequired):
        api.search("CAPTCHA-1")


def test_api_rate_limit_returns_retry_after():
    with DemoServer("127.0.0.1", 0, DemoOptions(max_per_minute=2, latency=0)) as server:
        api = _api(server)
        api.search("A1")
        api.search("A2")
        with pytest.raises(RateLimitedError) as info:
            api.search("A3")
        assert info.value.retry_after and info.value.retry_after > 0


def test_api_timeout_is_transient():
    with DemoServer("127.0.0.1", 0, DemoOptions(latency=0, slow_seconds=2)) as server:
        api = DemoApiAdapter({"base_url": server.url, "autostart_server": False})
        api.open(AdapterContext(timeout_seconds=0.5))
        with pytest.raises(TransientError):
            api.search("LENTO-1")


def test_network_down_is_transient():
    api = DemoApiAdapter({"base_url": "http://127.0.0.1:9", "autostart_server": False})
    api.open(AdapterContext(timeout_seconds=1))
    with pytest.raises(TransientError):
        api.search("X")
