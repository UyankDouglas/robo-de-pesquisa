"""Proteção de sessões (DPAPI) e agendamento."""
from __future__ import annotations

import sys

import pytest

from core import scheduler
from core.session import SessionStore, dpapi_available, protect, unprotect

pytestmark = pytest.mark.skipif(not dpapi_available(), reason="DPAPI só existe no Windows")


def test_dpapi_roundtrip():
    secret = b'{"cookies": [{"name": "sessao", "value": "abc"}]}'
    blob = protect(secret)
    assert secret not in blob
    assert unprotect(blob) == secret


def test_session_store_encrypts_on_disk(tmp_home):
    store = SessionStore()
    state = {"cookies": [{"name": "wsb_demo_session", "value": "TOKEN-SECRETO-123"}], "origins": []}
    path = store.save("demo_login", state)
    raw = path.read_bytes()
    assert b"TOKEN-SECRETO-123" not in raw  # nada legível no arquivo
    assert store.load("demo_login") == state
    path.write_bytes(raw[:-5] + b"xxxxx")  # arquivo adulterado
    assert store.load("demo_login") is None
    assert store.clear("demo_login") and not store.exists("demo_login")


def test_schedule_command_and_wrapper(tmp_home, tmp_path):
    config = tmp_path / "cfg.json"
    config.write_text("{}")
    wrapper = scheduler.write_wrapper("consulta diaria", config, tmp_path)
    content = wrapper.read_text(encoding="oem" if sys.platform == "win32" else "utf-8")
    assert wrapper.name == "consulta_diaria.bat"
    assert "run --config" in content and str(config.resolve()) in content and "exit /b %ERRORLEVEL%" in content
    command = scheduler.build_schtasks_command("consulta_diaria", wrapper, "WEEKLY", "07:30", days="MON,FRI")
    assert command[:3] == ["schtasks", "/Create", "/TN"]
    assert "WebSearchBot\\consulta_diaria" in command and "07:30" in command and "MON,FRI" in command
    with pytest.raises(ValueError):
        scheduler.build_schtasks_command("x", wrapper, "SEMPRE")
