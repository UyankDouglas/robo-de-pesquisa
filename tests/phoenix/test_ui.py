"""Interface PHOENIX BOT: carga da planilha, abas ocultas, validação e mapeamento (sem navegador)."""
from __future__ import annotations

import gc
import time

import pytest

from phoenix.config import SheetMapping
from tests.phoenix.fixtures import MAPPINGS, build_workbook

pytestmark = pytest.mark.ui


def pump(app, predicate, timeout=20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def app(tmp_home, monkeypatch):
    from phoenix import jobs
    from ui import phoenix_app

    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(phoenix_app.messagebox, name, lambda *a, **k: None)
    monkeypatch.setattr(phoenix_app, "load_mappings",
                        lambda: {n: SheetMapping(sheet=n, **m) for n, m in MAPPINGS.items()})
    window = phoenix_app.PhoenixApp(db=jobs.open_database())
    yield window
    window._closing = True
    window.worker.shutdown()
    window.worker.join(10)
    window.destroy()
    del window
    gc.collect()


def test_load_validate_and_buttons(app, tmp_path):
    path = build_workbook(tmp_path / "entrada.xlsx")
    app._load_workbook(str(path))
    assert pump(app, lambda: len(app.sheet_vars) == 3)
    assert app.sheet_vars["TLV"].get() and app.sheet_vars["CSTV"].get()
    assert not app.sheet_vars["Oculta"].get()                     # oculta desmarcada por padrão
    app._validate()
    assert pump(app, lambda: "linha(s) em 2 aba(s)" in app.validation.cget("text"))
    assert "deslocamento" in app.validation.cget("text")
    assert app.cards["cnpjs"].value.cget("text") == "7"           # o CNPJ inválido não entra
    assert app.btn_run.cget("state") == "normal" and app.btn_pause.cget("state") == "disabled"
