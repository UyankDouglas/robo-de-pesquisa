"""Modo sem interface gráfica (linha de comando / Agendador de Tarefas do Windows).

Exemplos:
    python cli.py run --config config/minha_consulta.json
    python cli.py runs
    python cli.py export --run-id 3 --filter errors
    python cli.py login --adapter demo_login
    python cli.py test-site --adapter demo --query 12345678000190
    python cli.py schedule --config config/minha_consulta.json --name consulta_diaria --time 07:30

Códigos de saída (úteis no Agendador de Tarefas):
    0  concluída sem erros            1  concluída, mas há registros com erro
    2  configuração/uso inválido      3  falha de execução (navegador, site, banco)
    4  precisa de operador (login/CAPTCHA)
    5  execução já em andamento em outra instância
    6  execução parada antes do fim (ainda há pendentes)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from core import paths

EXIT_OK = 0
EXIT_ITEM_ERRORS = 1
EXIT_CONFIG = 2
EXIT_FAILURE = 3
EXIT_OPERATOR = 4
EXIT_ALREADY_RUNNING = 5
EXIT_INCOMPLETE = 6


def _console_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _exit_code_for(status, stats) -> int:
    from core.models import RunStatus

    if status == RunStatus.COMPLETED:
        return EXIT_ITEM_ERRORS if stats and stats.error else EXIT_OK
    if status == RunStatus.NEEDS_OPERATOR:
        return EXIT_OPERATOR
    if status == RunStatus.STOPPED:
        return EXIT_INCOMPLETE
    return EXIT_FAILURE


def _run_engine(db, run_id: int, export_filter: str, override_headless: bool | None = None) -> int:
    from core.engine import ProcessingEngine
    from database.repository import Repository
    from utils.logger import get_logger

    log = get_logger("cli")
    if override_headless is not None:
        repo = Repository(db.connect())
        run = repo.get_run(run_id)
        run.settings.headless = override_headless
        repo.update_run(run_id, settings=run.settings)
        repo.close()

    engine = ProcessingEngine(db, run_id, interactive=False, export_filter=export_filter)
    try:
        status = engine.run()
    except KeyboardInterrupt:
        engine.stop()
        log.warning("Interrompido pelo usuário (Ctrl+C).")
        return EXIT_INCOMPLETE
    repo = Repository(db.connect())
    stats = repo.stats(run_id)
    repo.close()
    log.info("Resultado: %d encontrado(s), %d sem resultado, %d erro(s), %d pendente(s).",
             stats.found, stats.not_found, stats.error, stats.remaining)
    if engine.output_path:
        log.info("Planilha de saída: %s", engine.output_path)
    return _exit_code_for(status, stats)


# ---------------------------------------------------------------------- comandos
def cmd_run(args) -> int:
    from core.config import load_job_config
    from core.jobs import mark_stale_runs, open_database, prepare_job_run

    job = load_job_config(args.config)
    if args.visible:
        job.settings.headless = False
    db = open_database()
    mark_stale_runs(db)
    run_id, resumed = prepare_job_run(db, job)
    print(f"Execução {run_id} {'retomada' if resumed else 'criada'}.")
    return _run_engine(db, run_id, job.export_filter)


def cmd_resume(args) -> int:
    from core.jobs import mark_stale_runs, open_database
    from database.repository import Repository
    from core.models import ItemStatus

    db = open_database()
    mark_stale_runs(db)
    if args.retry_errors:
        repo = Repository(db.connect())
        print(f"{repo.reset_items(args.run_id, [ItemStatus.ERROR])} registro(s) com erro voltaram para a fila.")
        repo.close()
    headless = True if args.headless else (False if args.visible else None)
    return _run_engine(db, args.run_id, args.filter, headless)


def cmd_runs(args) -> int:
    from core.jobs import mark_stale_runs, open_database
    from database.repository import Repository

    db = open_database()
    mark_stale_runs(db)
    repo = Repository(db.connect())
    runs = repo.list_runs(args.limit)
    if not runs:
        print("Nenhuma execução registrada.")
    for run in runs:
        s = repo.stats(run.id)
        print(f"#{run.id:<4} {run.created_at}  {run.status.label:<20} {run.adapter_id:<18} "
              f"{s.done}/{s.total} ({s.percent:5.1f}%)  ok={s.found} sem={s.not_found} erro={s.error}  "
              f"{Path(run.source_file).name} [{run.sheet}/{run.search_column}]")
    repo.close()
    return EXIT_OK


def cmd_show(args) -> int:
    from core.jobs import open_database
    from database.repository import Repository

    repo = Repository(open_database().connect())
    run = repo.get_run(args.run_id)
    s = repo.stats(run.id)
    print(f"Execução #{run.id} - {run.name}\n  Situação: {run.status.label}  {run.message or ''}")
    print(f"  Site: {run.adapter_id}\n  Planilha: {run.source_file} [{run.sheet} / {run.search_column}]")
    print(f"  Saída: {run.output_file or '-'}")
    print(f"  Total {s.total} | encontrados {s.found} | sem resultado {s.not_found} | erros {s.error} | "
          f"pendentes {s.remaining} | duplicados {s.duplicate} | inválidos {s.invalid}")
    print("  Eventos:")
    for ev in repo.events(run.id)[-args.events:]:
        print(f"    {ev['created_at']}  {ev['kind']:<16} {ev['message'] or ''}")
    if args.errors:
        from core.models import ItemStatus
        print("  Registros com erro:")
        for item in repo.items(run.id, [ItemStatus.ERROR]):
            print(f"    linha {item.row_number}: {item.identifier} - {item.message} ({item.attempts} tentativa(s))")
    repo.close()
    return EXIT_OK


def cmd_export(args) -> int:
    from core.jobs import export, open_database

    path = export(open_database(), args.run_id, args.filter, args.output)
    print(f"Planilha gerada: {path}")
    return EXIT_OK


def cmd_retry_errors(args) -> int:
    from core.jobs import open_database
    from core.models import ItemStatus
    from database.repository import Repository

    repo = Repository(open_database().connect())
    statuses = [ItemStatus.ERROR] + ([ItemStatus.FOUND, ItemStatus.NOT_FOUND] if args.all else [])
    print(f"{repo.reset_items(args.run_id, statuses)} registro(s) voltaram para a fila. "
          f"Use 'cli.py resume --run-id {args.run_id}' para processar.")
    repo.close()
    return EXIT_OK


def cmd_adapters(args) -> int:
    from adapters.registry import discover

    for info in discover().values():
        login = " (exige login)" if info.cls(None).requires_login else ""
        print(f"{info.id:<20} [{info.kind}] {info.name}{login}\n{'':<21}{info.description}\n{'':<21}origem: {info.source}")
    return EXIT_OK


def cmd_inspect(args) -> int:
    from excel.importer import inspect_workbook

    for sheet in inspect_workbook(args.file):
        print(f"Aba '{sheet.name}': {sheet.data_rows} linha(s) de dados, cabeçalho na linha {sheet.header_row}")
        print(f"  Colunas: {', '.join(sheet.columns)}")
    return EXIT_OK


def cmd_login(args) -> int:
    from adapters.base_adapter import AdapterContext
    from adapters.registry import create_adapter
    from core.browser import BrowserManager
    from core.session import SessionStore

    adapter = create_adapter(args.adapter, json.loads(args.options) if args.options else None)
    if not adapter.requires_browser:
        print("Este adaptador não usa navegador; não há sessão para salvar.")
        return EXIT_CONFIG
    store = SessionStore()
    with BrowserManager(headless=False, channel=args.channel, timeout_seconds=60,
                        storage_state=store.load(adapter.id)) as browser:
        adapter.open(AdapterContext(timeout_seconds=60, browser=browser))
        if adapter.is_logged_in():
            print("A sessão salva ainda é válida.")
        else:
            print(adapter.begin_login())
            input("Depois de entrar no site, pressione ENTER aqui... ")
            if not adapter.is_logged_in():
                print("Login não confirmado. Nada foi salvo.")
                return EXIT_OPERATOR
        path = store.save(adapter.id, adapter.storage_state() or {})
        print(f"Sessão salva (criptografada com DPAPI): {path}" if path else "Não foi possível salvar a sessão.")
        adapter.close()
    return EXIT_OK if path else EXIT_FAILURE


def cmd_test_site(args) -> int:
    """Faz uma única consulta e mostra o resultado: útil ao configurar um site novo."""
    from adapters.base_adapter import AdapterContext
    from adapters.registry import create_adapter
    from core.browser import BrowserManager
    from core.session import SessionStore

    adapter = create_adapter(args.adapter, json.loads(args.options) if args.options else None)
    browser = None
    try:
        if adapter.requires_browser:
            browser = BrowserManager(headless=not args.visible, channel=args.channel, timeout_seconds=args.timeout,
                                     storage_state=SessionStore().load(adapter.id)).start()
        adapter.open(AdapterContext(timeout_seconds=args.timeout, browser=browser))
        result = adapter.search(args.query)
        print(json.dumps({"encontrado": result.found, "dados": result.data, "mensagem": result.message},
                         ensure_ascii=False, indent=2))
        return EXIT_OK
    except Exception as exc:
        print(f"Falha: {type(exc).__name__}: {exc}")
        return EXIT_FAILURE
    finally:
        adapter.close()
        if browser:
            browser.close()


def cmd_schedule(args) -> int:
    from core import scheduler
    from core.config import load_job_config

    config_path = Path(args.config).resolve()
    load_job_config(config_path)  # valida antes de agendar
    wrapper = scheduler.write_wrapper(args.name, config_path)
    command = scheduler.build_schtasks_command(args.name, wrapper, args.schedule, args.time, args.modifier, args.days)
    print(f"Script criado: {wrapper}")
    print("Comando do Agendador de Tarefas:")
    print("  " + " ".join(f'"{c}"' if " " in c else c for c in command))
    if not args.apply:
        print("Execute novamente com --apply para registrar a tarefa (ou rode o comando acima).")
        return EXIT_OK
    result = scheduler.apply(command)
    print(result.stdout or result.stderr)
    return EXIT_OK if result.returncode == 0 else EXIT_FAILURE


def cmd_sample_input(args) -> int:
    from utils.sample_data import create_sample_workbook

    path = create_sample_workbook(Path(args.output), args.rows)
    print(f"Planilha de exemplo criada: {path}")
    return EXIT_OK


def cmd_install_browser(args) -> int:
    import subprocess

    from playwright._impl._driver import compute_driver_executable, get_driver_env

    driver = compute_driver_executable()
    command = [*driver, "install", args.browser] if isinstance(driver, tuple) else [str(driver), "install", args.browser]
    return subprocess.call(command, env=get_driver_env())


def cmd_demo_server(args) -> int:
    from demo_site.server import main as demo_main

    argv = ["--port", str(args.port)] + (["--require-login"] if args.require_login else [])
    demo_main(argv)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py", description="Web Search Bot - modo sem interface gráfica")
    parser.add_argument("--debug", action="store_true", help="mostra mensagens detalhadas")
    sub = parser.add_subparsers(dest="command", required=True)
    filters = ["all", "found", "not_found", "errors", "processed", "pending"]

    p = sub.add_parser("run", help="importa a planilha e processa conforme a configuração salva")
    p.add_argument("--config", required=True, help="arquivo JSON (veja config/example_config.json)")
    p.add_argument("--visible", action="store_true", help="mostra o navegador (ignora headless)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resume", help="retoma uma execução interrompida")
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--retry-errors", action="store_true", help="recoloca os erros na fila antes de retomar")
    p.add_argument("--filter", choices=filters, default="all")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--headless", action="store_true")
    group.add_argument("--visible", action="store_true")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("runs", help="lista execuções anteriores")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("show", help="detalhes/auditoria de uma execução")
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--events", type=int, default=20)
    p.add_argument("--errors", action="store_true", help="lista os registros com erro")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("export", help="gera a planilha Excel de uma execução")
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--filter", choices=filters, default="all")
    p.add_argument("--output", help="caminho do .xlsx (padrão: pasta output)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("retry-errors", help="recoloca registros com erro na fila")
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--all", action="store_true", help="recoloca também os concluídos (reconsulta tudo)")
    p.set_defaults(func=cmd_retry_errors)

    p = sub.add_parser("adapters", help="lista os sites/adaptadores disponíveis")
    p.set_defaults(func=cmd_adapters)

    p = sub.add_parser("inspect", help="mostra abas e colunas de uma planilha")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("login", help="login manual assistido e gravação protegida da sessão")
    p.add_argument("--adapter", required=True)
    p.add_argument("--channel", default="msedge", choices=["msedge", "chrome", "chromium"])
    p.add_argument("--options", help="opções do adaptador em JSON")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("test-site", help="faz uma consulta de teste em um adaptador")
    p.add_argument("--adapter", required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--visible", action="store_true")
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--channel", default="msedge", choices=["msedge", "chrome", "chromium"])
    p.add_argument("--options", help="opções do adaptador em JSON")
    p.set_defaults(func=cmd_test_site)

    p = sub.add_parser("schedule", help="prepara/registra a tarefa no Agendador do Windows")
    p.add_argument("--config", required=True)
    p.add_argument("--name", required=True, help="nome da tarefa")
    p.add_argument("--time", default="08:00", help="HH:MM")
    p.add_argument("--schedule", default="DAILY", choices=["ONCE", "DAILY", "WEEKLY", "HOURLY", "MINUTE"])
    p.add_argument("--modifier", type=int, help="a cada N unidades (ex.: a cada 2 dias)")
    p.add_argument("--days", help="dias para WEEKLY (ex.: MON,WED,FRI)")
    p.add_argument("--apply", action="store_true", help="registra a tarefa no Windows")
    p.set_defaults(func=cmd_schedule)

    p = sub.add_parser("sample-input", help="cria uma planilha de exemplo")
    p.add_argument("--output", default=str(paths.INPUT_DIR / "exemplo_entrada.xlsx"))
    p.add_argument("--rows", type=int, default=200, help="linhas da aba 'Lote' (volume)")
    p.set_defaults(func=cmd_sample_input)

    p = sub.add_parser("install-browser", help="baixa o Chromium do Playwright (opcional)")
    p.add_argument("--browser", default="chromium")
    p.set_defaults(func=cmd_install_browser)

    p = sub.add_parser("demo-server", help="sobe o site local de demonstração")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--require-login", action="store_true")
    p.set_defaults(func=cmd_demo_server)
    return parser


def main(argv: list[str] | None = None) -> int:
    _console_utf8()
    args = build_parser().parse_args(argv)
    paths.ensure_dirs()
    from core.models import AlreadyRunningError, ConfigError
    from utils.logger import setup_logging

    setup_logging(logging.DEBUG if args.debug else logging.INFO, console=True)
    log = logging.getLogger("wsb.cli")
    try:
        return args.func(args)
    except ConfigError as exc:
        log.error("Configuração inválida: %s", exc)
        return EXIT_CONFIG
    except AlreadyRunningError as exc:
        log.error("%s", exc)
        return EXIT_ALREADY_RUNNING
    except KeyError as exc:
        log.error("Não encontrado: %s", exc)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        log.warning("Interrompido pelo usuário.")
        return EXIT_INCOMPLETE
    except Exception as exc:
        log.exception("Falha inesperada: %s", exc)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
