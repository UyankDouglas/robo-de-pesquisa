# -*- mode: python ; coding: utf-8 -*-
# Especificação do PyInstaller: gera uma pasta com dois executáveis que compartilham as bibliotecas.
#   WebSearchBot.exe     interface gráfica (sem console)
#   WebSearchBotCLI.exe  modo sem interface / Agendador de Tarefas (com console e códigos de saída)
# Uso: build_exe.bat  (ou: python -m PyInstaller --noconfirm --clean web_search_bot.spec)
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = []
datas += collect_data_files("customtkinter")           # temas e fontes da interface
datas += collect_data_files("playwright")              # driver do Playwright (node + pacote)
datas += [
    ("demo_site/static", "demo_site/static"),
    ("config/sites/demo_declarativo.json", "config/sites"),
    ("config/example_config.json", "config"),
]
hiddenimports = collect_submodules("adapters") + collect_submodules("demo_site")
excludes = ["pytest", "PyInstaller", "IPython", "matplotlib", "scipy", "notebook"]

common = dict(pathex=["."], datas=datas, hiddenimports=hiddenimports, excludes=excludes, noarchive=False)
gui = Analysis(["main.py"], **common)
cli = Analysis(["cli.py"], **common)

gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="WebSearchBot",
              console=False, upx=False)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="WebSearchBotCLI",
              console=True, upx=False)

coll = COLLECT(gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas,
               name="WebSearchBot", upx=False)
