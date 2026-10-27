"""Normalização e validação de identificadores (genérico; CNPJ/CPF são apenas validações opcionais)."""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Any

_NON_DIGITS = re.compile(r"\D")
_ZERO_FORMAT = re.compile(r"^0+$")


def only_digits(value: str) -> str:
    return _NON_DIGITS.sub("", value or "")


def cell_to_text(value: Any, number_format: str | None = None) -> str:
    """Converte o valor de uma célula do Excel em texto sem perder informação.

    - Texto é mantido como está (zeros à esquerda preservados).
    - Inteiros gravados como número viram texto sem ".0" e sem notação científica.
    - Se a célula usa um formato só de zeros (ex.: "00000000000000"), os zeros exibidos
      no Excel são restaurados.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "VERDADEIRO" if value else "FALSO"
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dt.datetime):
        return value.strftime("%d/%m/%Y %H:%M:%S") if (value.hour or value.minute or value.second) else value.strftime("%d/%m/%Y")
    if isinstance(value, dt.date):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, dt.time):
        return value.strftime("%H:%M:%S")
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, Decimal) and value == value.to_integral_value():
            value = int(value)
        if isinstance(value, int):
            text = str(value)
            fmt = (number_format or "").strip()
            if fmt and _ZERO_FORMAT.match(fmt) and len(text) < len(fmt):
                text = text.zfill(len(fmt))
            return text
        return format(Decimal(str(value)).normalize(), "f")
    return str(value).strip()


def normalize_identifier(value: str, zero_pad: int | None = None) -> str:
    text = (value or "").strip()
    if zero_pad and text.isdigit() and len(text) < zero_pad:
        text = text.zfill(zero_pad)
    return text


def is_valid_cnpj(value: str) -> bool:
    digits = only_digits(value)
    if len(digits) != 14 or digits == digits[0] * 14:
        return False

    def check_digit(base: str, weights: list[int]) -> str:
        total = sum(int(d) * w for d, w in zip(base, weights))
        rest = total % 11
        return "0" if rest < 2 else str(11 - rest)

    first = check_digit(digits[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    second = check_digit(digits[:12] + first, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return digits[12:] == first + second


def is_valid_cpf(value: str) -> bool:
    digits = only_digits(value)
    if len(digits) != 11 or digits == digits[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(d) * w for d, w in zip(digits[:size], range(size + 1, 1, -1)))
        check = (total * 10) % 11 % 10
        if check != int(digits[size]):
            return False
    return True


def validate_identifier(value: str, rule: str) -> str | None:
    """Retorna None se válido ou a mensagem de erro."""
    if rule in ("", "none", None):
        return None
    if rule == "digits":
        return None if value.isdigit() else "Contém caracteres que não são dígitos"
    if rule == "cnpj":
        return None if is_valid_cnpj(value) else "CNPJ inválido"
    if rule == "cpf":
        return None if is_valid_cpf(value) else "CPF inválido"
    if rule == "cpf_cnpj":
        return None if (is_valid_cpf(value) or is_valid_cnpj(value)) else "CPF/CNPJ inválido"
    raise ValueError(f"Regra de validação desconhecida: {rule}")
