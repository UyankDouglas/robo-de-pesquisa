# -*- mode: python ; coding: utf-8 -*-
# Especificação do PyInstaller: gera uma pasta com quatro executáveis que compartilham as bibliotecas.
#   WebSearchBot.exe      robô genérico — interface gráfica
#   WebSearchBotCLI.exe   robô genérico — modo sem interface / Agendador de Tarefas
#   PhoenixBot.exe        PHOENIX BOT (TIM Phoenix2) — interface gráfica
#   PhoenixBotCLI.exe     PHOENIX BOT — linha de comando (inspeção, login, lote)
# Uso: build_exe.bat  (ou: python -m PyInstaller --noconfirm --clean web_search_bot.spec)
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = []
datas += collect_data_files("customtkinter")           # temas e fontes da interface
datas += collect_data_files("playwright")              # driver do Playwright (node + pacote)
datas += [
    ("demo_site/static", "demo_site/static"),
    ("config/sites/demo_declarativo.json", "config/sites"),
    ("config/example_config.json", "config"),
    ("config/phoenix", "config/phoenix"),
]
hiddenimports = collect_submodules("adapters") + collect_submodules("demo_site") + collect_submodules("phoenix")
excludes = ["pytest", "PyInstaller", "IPython", "matplotlib", "scipy", "notebook", "phoenix_simulator"]

common = dict(pathex=["."], datas=datas, hiddenimports=hiddenimports, excludes=excludes, noarchive=False)
gui = Analysis(["main.py"], **common)
cli = Analysis(["cli.py"], **common)
phx_gui = Analysis(["phoenix_main.py"], **common)
phx_cli = Analysis(["phoenix_cli.py"], **common)


def exe(analysis, name, console):
    return EXE(PYZ(analysis.pure), analysis.scripts, [], exclude_binaries=True, name=name, console=console, upx=False)


coll = COLLECT(
    exe(gui, "WebSearchBot", False), gui.binaries, gui.datas,
    exe(cli, "WebSearchBotCLI", True), cli.binaries, cli.datas,
    exe(phx_gui, "PhoenixBot", False), phx_gui.binaries, phx_gui.datas,
    exe(phx_cli, "PhoenixBotCLI", True), phx_cli.binaries, phx_cli.datas,
    name="WebSearchBot", upx=False,
)
