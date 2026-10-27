"""Normalização de CNPJ e números de telefone para a correspondência CNPJ + Número a portar.

Regras (prioridade: nunca relacionar uma linha com o bilhete de outra):
- CNPJ: só dígitos (ou alfanumérico no formato novo), completado com zeros à esquerda até 14 e
  validado pelos dígitos verificadores. Números lidos do Excel nunca passam por notação científica.
- Telefone: só dígitos. DDI 55 e zero de discagem só são removidos se configurado e com o tamanho
  esperado — números diferentes nunca são considerados a mesma linha.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_ALNUM = re.compile(r"[^0-9A-Z]")
_DIGITS = re.compile(r"\D")


def cell_text(value: Any) -> str:
    """Texto de uma célula sem perder dígitos (inteiros gravados como float viram inteiro)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(int(value))
    return str(value).strip()


def _cnpj_check(base: str) -> str:
    """Dígitos verificadores do CNPJ (numérico ou alfanumérico: valor = código ASCII - 48)."""
    def digit(chars: str, weights: list[int]) -> str:
        total = sum((ord(c) - 48) * w for c, w in zip(chars, weights))
        rest = total % 11
        return "0" if rest < 2 else str(11 - rest)

    first = digit(base, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return first + digit(base + first, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])


def is_valid_cnpj(value: str) -> bool:
    if len(value) != 14 or not value[12:].isdigit() or value == value[0] * 14:
        return False
    return _cnpj_check(value[:12]) == value[12:]


def is_valid_cpf(value: str) -> bool:
    if len(value) != 11 or not value.isdigit() or value == value[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(d) * w for d, w in zip(value[:size], range(size + 1, 1, -1)))
        if (total * 10) % 11 % 10 != int(value[size]):
            return False
    return True


@dataclass(frozen=True)
class CnpjInfo:
    raw: str
    value: str          # 14 caracteres quando válido; "" caso contrário
    status: str         # valido | cpf | invalido | vazio
    message: str = ""


def normalize_cnpj(value: Any) -> CnpjInfo:
    raw = cell_text(value)
    if not raw:
        return CnpjInfo(raw, "", "vazio", "CNPJ vazio")
    chars = _ALNUM.sub("", raw.upper())
    if chars.isdigit():
        if len(chars) > 14:
            return CnpjInfo(raw, "", "invalido", "CNPJ com mais de 14 dígitos")
        padded = chars.zfill(14)
        if is_valid_cnpj(padded):
            return CnpjInfo(raw, padded, "valido")
        if is_valid_cpf(chars.zfill(11)) and len(chars) <= 11:
            return CnpjInfo(raw, "", "cpf", "É um CPF; a consulta é por CNPJ")
        return CnpjInfo(raw, "", "invalido", "Dígitos verificadores do CNPJ não conferem")
    if len(chars) == 14 and is_valid_cnpj(chars):  # CNPJ alfanumérico (formato novo)
        return CnpjInfo(raw, chars, "valido")
    return CnpjInfo(raw, "", "invalido", "CNPJ em formato não reconhecido")


@dataclass(frozen=True)
class PhoneRules:
    strip_ddi_55: bool = True        # 55 + DDD + número (12 ou 13 dígitos) -> DDD + número
    strip_trunk_zero: bool = False   # 0 + DDD + número (11 ou 12 dígitos) -> DDD + número
    min_digits: int = 10
    max_digits: int = 11


def normalize_phone(value: Any, rules: PhoneRules = PhoneRules()) -> str:
    """Retorna só os dígitos do telefone (DDD + número) ou "" se não for um número de linha válido."""
    digits = _DIGITS.sub("", cell_text(value))
    if rules.strip_ddi_55 and len(digits) in (12, 13) and digits.startswith("55"):
        digits = digits[2:]
    if rules.strip_trunk_zero and len(digits) in (11, 12) and digits.startswith("0"):
        digits = digits[1:]
    if not rules.min_digits <= len(digits) <= rules.max_digits:
        return ""
    return digits


def normalize_label(text: Any) -> str:
    """Comparação de rótulos/status: sem acentos, sem diferença de maiúsculas e espaços."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.replace(":", " ").split()).casefold()


def mask(value: str, keep: int = 3) -> str:
    """Mascara CNPJ/telefone para logs: 123********190."""
    value = str(value or "")
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]
