"""Ajuste de leitura do openpyxl para não perder dados de planilhas reais.

Células com formato de data que guardam números fora do intervalo de datas (ex.: nº de contrato
"2026671220003" numa coluna formatada como data) são convertidas pelo openpyxl em "#VALUE!" — o
número original se perderia na exportação. Com este ajuste, a conversão continua igual para datas
válidas e, quando a data é impossível, o número original é preservado.
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore", category=UserWarning, module=r"openpyxl\..*")


def install() -> None:
    from openpyxl.worksheet import _reader

    if getattr(_reader.from_excel, "_wsb_safe", False):
        return
    original = _reader.from_excel

    def from_excel_keeping_numbers(value, *args, **kwargs):
        try:
            return original(value, *args, **kwargs)
        except (OverflowError, ValueError):
            return value

    from_excel_keeping_numbers._wsb_safe = True  # type: ignore[attr-defined]
    _reader.from_excel = from_excel_keeping_numbers


install()
