from __future__ import annotations

import datetime as dt

from utils.logger import RedactSecretsFilter, mask_identifier
from utils.validators import cell_to_text, is_valid_cnpj, is_valid_cpf, normalize_identifier, validate_identifier


def test_cnpj_and_cpf():
    assert is_valid_cnpj("11.222.333/0001-81")
    assert not is_valid_cnpj("11222333000182")
    assert not is_valid_cnpj("00000000000000")
    assert is_valid_cpf("529.982.247-25")
    assert not is_valid_cpf("52998224724")
    assert validate_identifier("52998224725", "cpf_cnpj") is None
    assert validate_identifier("12A", "digits") is not None
    assert validate_identifier("qualquer coisa", "none") is None


def test_cell_to_text():
    assert cell_to_text(None) == ""
    assert cell_to_text("  0012 ") == "0012"
    assert cell_to_text(12345678901234) == "12345678901234"
    assert cell_to_text(1.0) == "1"
    assert cell_to_text(0.1) == "0.1"
    assert cell_to_text(123, "000000") == "000123"
    assert cell_to_text(dt.datetime(2024, 5, 1)) == "01/05/2024"
    assert cell_to_text(True) == "VERDADEIRO"


def test_normalize_and_mask():
    assert normalize_identifier(" 191 ", 14) == "00000000000191"
    assert normalize_identifier("ABC", 14) == "ABC"
    assert mask_identifier("12345678000190") == "123********190"
    assert mask_identifier("123") == "***"


def test_log_filter_redacts_secrets():
    import logging

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "login com senha=abc123 e token: xyz", None, None)
    RedactSecretsFilter().filter(record)
    assert "abc123" not in record.getMessage() and "xyz" not in record.getMessage()
