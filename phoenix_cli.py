"""PHOENIX BOT — modo sem interface e ferramentas da primeira integração.

    python phoenix_cli.py abas --file "ESTEIRA.xlsx"                 abas, visibilidade e mapeamento
    python phoenix_cli.py validar --file "ESTEIRA.xlsx" --abas "AGOSTO TLVS;OUTUBRO TLV"
    python phoenix_cli.py inspecionar                                 1ª integração guiada (navegador visível)
    python phoenix_cli.py login                                       login manual + sessão protegida (DPAPI)
    python phoenix_cli.py testar --cnpj 12345678000190                uma consulta real, sem gravar nada
    python phoenix_cli.py executar --file "ESTEIRA.xlsx" --abas "..." lote (usa a sessão salva)
    python phoenix_cli.py exportar --run-id 3
    python phoenix_cli.py execucoes

Códigos de saída: 0 ok · 1 concluída com erros de consulta · 2 configuração · 3 falha ·
4 precisa de operador (login/CAPTCHA) · 5 já em execução · 6 parada antes do fim.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from core import paths


def _settings():
    from phoenix.config import load_settings

    return load_settings()


def _mappings(source: str, abas: str | None):
    from phoenix.config import load_mappings
    from phoenix.workbook import detect_mappings, list_sheets

    saved = load_mappings()
    if abas:
        names = [a.strip() for a in abas.split(";") if a.strip()]
    else:  # padrão: abas visíveis que já têm mapeamento salvo
        names = [s.name for s in list_sheets(source) if not s.hidden and s.name in saved]
    missing = [n for n in names if n not in saved]
    if missing:
        saved.update(detect_mappings(source, missing))
    return {n: saved[n] for n in names}


def cmd_abas(args) -> int:
    from phoenix.config import load_mappings
    from phoenix.workbook import list_sheets

    saved = load_mappings()
    for s in list_sheets(args.file):
        m = saved.get(s.name)
        mapping = (f"CNPJ={m.cnpj} Acesso={m.acesso} Fila={m.fila_atual or '(criar)'} Temp={m.numero_temporario}"
                   if m else "sem mapeamento salvo")
        print(f"{s.name:<26} {'OCULTA ' if s.hidden else 'visível'} {s.data_rows:>6} linhas  cabeçalho L{s.header_row}  {mapping}")
    return 0


def cmd_validar(args) -> int:
    from phoenix import jobs
    from phoenix.workbook import load_sheets

    sheets = load_sheets(args.file, _mappings(args.file, args.abas), jobs.phone_rules(_settings()))
    total = 0
    for s in sheets:
        cnpjs = {r.cnpj.value for r in s.rows if r.cnpj.value}
        total += len(s.rows)
        print(f"{s.name}: {len(s.rows)} linhas, {len(cnpjs)} CNPJs distintos")
        for issue in s.issues:
            print(f"   - {issue}")
    print(f"Total: {total} linhas.")
    return 0


def _browser(headless: bool = False):
    from core.browser import BrowserManager
    from core.session import SessionStore

    s = _settings()
    state = SessionStore().load("phoenix2") if s.persist_session else None
    return BrowserManager(headless=headless, channel=s.browser_channel, timeout_seconds=s.timeout_seconds,
                          storage_state=state).start()


def _client(browser):
    from phoenix.client import PhoenixClient
    from phoenix.config import load_selectors

    s = _settings()
    return PhoenixClient(lambda: browser.page, load_selectors(), s.base_url, s.timeout_seconds, s.max_paginas)


def _save_session(browser) -> None:
    from core.session import SessionStore

    if _settings().persist_session and SessionStore().save("phoenix2", browser.export_storage_state() or {}):
        print("Sessão salva de forma protegida (DPAPI).")


def cmd_login(args) -> int:
    browser = _browser()
    try:
        client = _client(browser)
        client.open_home()
        input("Faça o login no Phoenix2 na janela do navegador e pressione ENTER aqui... ")
        if not client.is_logged_in():
            print("Login não confirmado (menu 'Portabilidade' não encontrado).")
            return 4
        _save_session(browser)
        return 0
    finally:
        browser.close()


def cmd_inspecionar(args) -> int:
    """Primeira integração: captura as telas reais para confirmar os locators."""
    from phoenix.config import load_selectors
    from phoenix.inspector import capture

    menu = load_selectors()["menu"]
    texts = [menu["portabilidade"], menu["acompanhamento"], menu["status_solicitacoes"]]
    browser = _browser()
    try:
        client = _client(browser)
        client.open_home()
        input("1) Faça o login no Phoenix2 na janela aberta e pressione ENTER... ")
        print("   ", capture(browser.page, "01_inicio", texts))
        try:
            client.go_to_status_page()
            print("2) Menu Portabilidade > Acompanhamento > Status Solicitações aberto automaticamente.")
        except Exception as exc:
            print(f"2) Não consegui abrir pelo menu ({exc}).")
            input("   Abra a tela 'Status das Solicitações' manualmente e pressione ENTER... ")
        print("   ", capture(browser.page, "02_status_solicitacoes", texts))
        input("3) Faça manualmente uma pesquisa por CNPJ (com resultado) e pressione ENTER... ")
        print("   ", capture(browser.page, "03_resultados", texts))
        input("4) Se houver paginação, vá para a página 2 (senão só pressione ENTER)... ")
        print("   ", capture(browser.page, "04_paginacao", texts))
        input("5) Selecione uma solicitação, clique em Detalhar e pressione ENTER na tela de detalhes... ")
        print("   ", capture(browser.page, "05_detalhes", texts))
        input("6) Se existir, pesquise um CNPJ SEM solicitações e pressione ENTER ao ver a mensagem... ")
        print("   ", capture(browser.page, "06_sem_resultado", texts))
        _save_session(browser)
        print(f"Capturas em {paths.LOGS_DIR / 'inspecao'}. Elas ficam só nesta máquina (dígitos longos mascarados).")
        return 0
    finally:
        browser.close()


def cmd_testar(args) -> int:
    from phoenix.normalize import normalize_cnpj

    info = normalize_cnpj(args.cnpj)
    if info.status != "valido":
        print(f"CNPJ inválido: {info.message}")
        return 2
    browser = _browser()
    try:
        client = _client(browser)
        if not client.is_logged_in():
            input("Faça o login no Phoenix2 na janela do navegador e pressione ENTER... ")
        result = client.consultar(info.value)
        _save_session(browser)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.mostrar_dados else
              f"{len(result['solicitacoes'])} solicitação(ões); bilhetes por status: "
              + json.dumps(_status_counts(result), ensure_ascii=False))
        return 0
    finally:
        browser.close()


def _status_counts(result) -> dict[str, int]:
    out: dict[str, int] = {}
    for sol in result["solicitacoes"]:
        for t in sol.get("bilhetes", []):
            out[t.get("status_bilhete") or "(vazio)"] = out.get(t.get("status_bilhete") or "(vazio)", 0) + 1
    return out


def cmd_executar(args) -> int:
    from core.models import RunStatus
    from phoenix import jobs

    db = jobs.open_database()
    jobs.mark_stale_runs(db)
    settings = _settings()
    run_id, resumed = jobs.create_job(db, args.file, _mappings(args.file, args.abas), settings, resume=not args.novo)
    if resumed:
        jobs.apply_settings(db, run_id, settings)
    print(f"Execução {run_id} {'retomada' if resumed else 'criada'}.")
    engine = jobs.make_engine(db, run_id, interactive=False)
    try:
        status = engine.run()
    except KeyboardInterrupt:
        engine.stop()
        return 6
    files = jobs.export(db, run_id)
    for path in files.values():
        print("Arquivo:", path)
    summary = jobs.report(db, run_id).summary()
    print(json.dumps(summary, ensure_ascii=False))
    if status == RunStatus.COMPLETED:
        return 1 if summary["erro_consulta"] else 0
    return {RunStatus.NEEDS_OPERATOR: 4, RunStatus.STOPPED: 6}.get(status, 3)


def cmd_exportar(args) -> int:
    from phoenix import jobs

    for path in jobs.export(jobs.open_database(), args.run_id).values():
        print("Arquivo:", path)
    return 0


def cmd_execucoes(args) -> int:
    from database.repository import Repository
    from phoenix import jobs
    from phoenix.repository import PhoenixRepository

    db = jobs.open_database()
    jobs.mark_stale_runs(db)
    repo = Repository(db.connect())
    for run_id in PhoenixRepository(repo).jobs():
        run, s = repo.get_run(run_id), repo.stats(run_id)
        print(f"#{run_id:<4} {run.created_at} {run.status.label:<20} CNPJs {s.done}/{s.total} erros={s.error}  {run.name}")
    repo.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(prog="phoenix_cli.py", description="PHOENIX BOT — modo sem interface")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("abas"); p.add_argument("--file", required=True); p.set_defaults(func=cmd_abas)
    p = sub.add_parser("validar"); p.add_argument("--file", required=True); p.add_argument("--abas", help="nomes separados por ;")
    p.set_defaults(func=cmd_validar)
    sub.add_parser("inspecionar").set_defaults(func=cmd_inspecionar)
    sub.add_parser("login").set_defaults(func=cmd_login)
    p = sub.add_parser("testar"); p.add_argument("--cnpj", required=True)
    p.add_argument("--mostrar-dados", action="store_true", help="imprime solicitações e bilhetes completos")
    p.set_defaults(func=cmd_testar)
    p = sub.add_parser("executar"); p.add_argument("--file", required=True); p.add_argument("--abas", help="nomes separados por ;")
    p.add_argument("--novo", action="store_true", help="não retomar execução incompleta"); p.set_defaults(func=cmd_executar)
    p = sub.add_parser("exportar"); p.add_argument("--run-id", type=int, required=True); p.set_defaults(func=cmd_exportar)
    sub.add_parser("execucoes").set_defaults(func=cmd_execucoes)
    args = parser.parse_args(argv)

    paths.ensure_dirs()
    from core.models import AlreadyRunningError, ConfigError
    from utils.logger import setup_logging

    setup_logging(logging.INFO)
    log = logging.getLogger("wsb.phoenix.cli")
    try:
        return args.func(args)
    except ConfigError as exc:
        log.error("Configuração inválida: %s", exc)
        return 2
    except AlreadyRunningError as exc:
        log.error("%s", exc)
        return 5
    except Exception as exc:
        log.exception("Falha: %s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
