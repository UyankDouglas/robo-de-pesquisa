"""Modo agendado (sem interface): configuração salva, códigos de saída e comandos auxiliares."""
from __future__ import annotations

import json

import pytest

import cli
from core.locking import RunLock
from tests.conftest import make_workbook


def _config(tmp_path, demo_server, ids, **execution):
    sheet = make_workbook(tmp_path / "entrada.xlsx", [["ID", "DOCUMENTO"]] + [[i, v] for i, v in enumerate(ids, 1)],
                          sheet="Registros")
    raw = {
        "name": "teste cli",
        "input": {"file": str(sheet), "sheet": "Registros", "column": "DOCUMENTO"},
        "site": {"adapter": "demo_api", "options": {"base_url": demo_server.url, "autostart_server": False}},
        "execution": {"interval_seconds": 0, "max_retries": 1, "backoff_base_seconds": 0.05, "timeout_seconds": 5,
                      **execution},
        "output": {"dir": str(tmp_path / "out"), "filter": "all"},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_run_success_exit_code_and_output(tmp_home, tmp_path, demo_server):
    config = _config(tmp_path, demo_server, ["12345678000190", "98765432000110"])
    assert cli.main(["run", "--config", str(config)]) == cli.EXIT_OK
    outputs = list((tmp_path / "out").glob("*.xlsx"))
    assert len(outputs) == 1
    assert cli.main(["runs"]) == cli.EXIT_OK
    assert cli.main(["show", "--run-id", "1", "--errors"]) == cli.EXIT_OK
    assert cli.main(["export", "--run-id", "1", "--filter", "found", "--output", str(tmp_path / "f.xlsx")]) == cli.EXIT_OK
    assert (tmp_path / "f.xlsx").exists()


def test_run_with_item_errors_returns_1(tmp_home, tmp_path, demo_server):
    config = _config(tmp_path, demo_server, ["12345678000190", "ERRO-1"])
    assert cli.main(["run", "--config", str(config)]) == cli.EXIT_ITEM_ERRORS
    assert cli.main(["retry-errors", "--run-id", "1"]) == cli.EXIT_OK
    assert cli.main(["resume", "--run-id", "1"]) == cli.EXIT_ITEM_ERRORS  # continua falhando: código 1 de novo


def test_captcha_in_scheduled_mode_returns_4(tmp_home, tmp_path, demo_server):
    config = _config(tmp_path, demo_server, ["CAPTCHA-1"])
    assert cli.main(["run", "--config", str(config)]) == cli.EXIT_OPERATOR


def test_invalid_config_returns_2(tmp_home, tmp_path):
    bad = tmp_path / "ruim.json"
    bad.write_text('{"input": {"file": "x.xlsx"}}', encoding="utf-8")
    assert cli.main(["run", "--config", str(bad)]) == cli.EXIT_CONFIG
    assert cli.main(["run", "--config", str(tmp_path / "nao_existe.json")]) == cli.EXIT_CONFIG


def test_already_running_returns_5(tmp_home, tmp_path, demo_server):
    config = _config(tmp_path, demo_server, ["12345678000190"])
    assert cli.main(["run", "--config", str(config)]) == cli.EXIT_OK
    assert cli.main(["retry-errors", "--run-id", "1", "--all"]) == cli.EXIT_OK
    with RunLock(1):
        assert cli.main(["resume", "--run-id", "1"]) == cli.EXIT_ALREADY_RUNNING


def test_inspect_adapters_and_sample(tmp_home, tmp_path, capsys):
    out = tmp_path / "exemplo.xlsx"
    assert cli.main(["sample-input", "--output", str(out), "--rows", "5"]) == cli.EXIT_OK
    assert cli.main(["inspect", "--file", str(out)]) == cli.EXIT_OK
    assert cli.main(["adapters"]) == cli.EXIT_OK
    text = capsys.readouterr().out
    assert "Registros" in text and "demo_declarativo" in text


def test_test_site_command(tmp_home, demo_server, capsys):
    options = json.dumps({"base_url": demo_server.url, "autostart_server": False})
    assert cli.main(["test-site", "--adapter", "demo_api", "--query", "11222333000144", "--options", options]) == cli.EXIT_OK
    assert "Beta Serviços" in capsys.readouterr().out


@pytest.mark.browser
def test_scheduled_run_with_browser(tmp_home, tmp_path, demo_server):
    config = _config(tmp_path, demo_server, ["12345678000190", "NF-1"])
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["site"]["adapter"] = "demo"
    raw["execution"].update(headless=True, browser_channel="msedge")
    config.write_text(json.dumps(raw), encoding="utf-8")
    assert cli.main(["run", "--config", str(config)]) == cli.EXIT_OK
