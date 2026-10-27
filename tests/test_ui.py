"""Interface gráfica: fluxo completo pelos mesmos métodos que os botões chamam."""
from __future__ import annotations

import gc
import time

import pytest

from adapters.demo_adapter import DemoApiAdapter
from core import paths
from core.engine import EngineState
from database.connection import Database
from tests.conftest import make_workbook

pytestmark = pytest.mark.ui


@pytest.fixture
def app(tmp_home, monkeypatch, demo_server):
    from ui import app as app_module

    answers = {"askyesnocancel": True}
    for name in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(app_module.messagebox, name, lambda *a, **k: None)
    monkeypatch.setattr(app_module.messagebox, "askyesno", lambda *a, **k: False)
    monkeypatch.setattr(app_module.messagebox, "askyesnocancel", lambda *a, **k: answers["askyesnocancel"])
    monkeypatch.setattr(DemoApiAdapter, "default_options",
                        classmethod(lambda cls: {"base_url": demo_server.url, "autostart_server": False}))
    window = app_module.WebSearchBotApp(db=Database(paths.DB_PATH))
    window.answers = answers
    yield window
    if window.engine:
        window.engine.stop()
        window.engine.join(10)
    try:
        window._shutdown()
    except Exception:
        pass
    # Finaliza os objetos Tk da janela destruída na thread principal; se o coletor de lixo rodar
    # depois numa thread de trabalho, o Tcl de uma janela já destruída trava (só ocorre em testes,
    # que criam várias janelas no mesmo processo).
    del window
    gc.collect()


def pump(app, predicate, timeout=20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _prepare(app, tmp_path, ids):
    path = make_workbook(tmp_path / "gui.xlsx", [["ID", "DOCUMENTO", "STATUS"]] + [[i, v, "Pendente"] for i, v in enumerate(ids, 1)],
                         sheet="Registros")
    app._load_workbook(str(path))
    assert pump(app, lambda: app.column_var.get() == "DOCUMENTO")  # coluna sugerida automaticamente
    app.adapter_var.set(DemoApiAdapter.display_name)
    app._on_adapter()
    app.interval_var.set("0")
    return path


def test_full_run_from_gui(app, tmp_path):
    _prepare(app, tmp_path, ["12345678000190", "98765432000110", "12345678000190", "00012345000101"])
    assert "Para consultar: 3" in app.import_info.cget("text")
    assert "Duplicados (não consultados): 1" in app.import_info.cget("text")
    assert app.btn_start.cget("state") == "normal"

    app._on_start()
    assert pump(app, lambda: app.engine is not None)
    assert pump(app, lambda: app.engine is None and "Concluída" in app.status_label.cget("text"))
    assert app.cards["total"].value.cget("text") == "3"
    assert app.cards["found"].value.cget("text") == "2"
    assert app.cards["not_found"].value.cget("text") == "1"
    assert app.cards["percent"].value.cget("text") == "100.0%"
    assert list((paths.OUTPUT_DIR).glob("gui_resultado_exec1.xlsx"))
    assert app.history.tree.get_children() == ("1",)
    log_text = app.log_view.get("1.0", "end")
    assert "12345678000190: concluído" in log_text and "98765432000110: sem resultado" in log_text


def test_pause_continue_stop_and_resume_buttons(app, tmp_path):
    _prepare(app, tmp_path, [f"CODIGO-{i:03d}" for i in range(40)])
    app.interval_var.set("0.1")
    app._on_start()
    assert pump(app, lambda: app.engine_state == EngineState.RUNNING and app.cards["pending"].value.cget("text") not in ("0", "40"))
    assert app.btn_pause.cget("state") == "normal" and app.btn_start.cget("state") == "disabled"

    app._on_pause()
    assert pump(app, lambda: app.engine_state == EngineState.PAUSED)
    assert app.btn_resume.cget("state") == "normal" and app.btn_pause.cget("state") == "disabled"
    app._on_resume()
    assert pump(app, lambda: app.engine_state == EngineState.RUNNING)

    app.engine.stop()  # mesmo efeito do botão PARAR (sem o diálogo de confirmação)
    assert pump(app, lambda: app.engine is None)
    pending = int(app.cards["pending"].value.cget("text"))
    assert 0 < pending < 40

    app.answers["askyesnocancel"] = True  # "continuar de onde parou?" -> Sim
    app._on_start()
    assert pump(app, lambda: app.engine is None and "Concluída" in app.status_label.cget("text"), timeout=30)
    assert app.cards["pending"].value.cget("text") == "0"
    assert app.history.tree.get_children() == ("1",)  # retomou a mesma execução, não criou outra
