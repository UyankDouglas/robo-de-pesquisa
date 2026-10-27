"""Configuração do Phoenix Bot: parâmetros, regras de status/correspondência, mapeamento das abas
e textos/locators da tela do Phoenix2 (config/phoenix/*.json)."""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from core import paths
from core.models import ConfigError

CONFIG_SUBDIR = "phoenix"
MULTIPLE_TICKET_RULES = ("mais_recente", "revisar")


def config_dir() -> Path:
    return paths.CONFIG_DIR / CONFIG_SUBDIR


def bundled_dir() -> Path:
    return paths.RESOURCE_DIR / "config" / CONFIG_SUBDIR


def _read_json(name: str) -> dict[str, Any]:
    for folder in (config_dir(), bundled_dir()):
        path = folder / name
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"JSON inválido em {path}: {exc}") from exc
    return {}


def _write_json(name: str, data: dict[str, Any]) -> Path:
    path = config_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def _from_dict(cls, data: dict[str, Any] | None):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in names})


# --------------------------------------------------------------------- regras
@dataclass
class StatusRules:
    """Quais textos de "Status do Bilhete" pertencem a cada categoria (comparação sem acentos/maiúsculas).

    "Falha parcial / portada" vem configurado só com o texto literal informado. Confirme no Phoenix2
    se é um único status ou dois (ex.: "Falha parcial" e "Portada") e ajuste — nada é deduzido por
    semelhança de nome.
    """

    em_andamento: list[str] = field(default_factory=lambda: ["Em andamento"])
    falha_parcial_portada: list[str] = field(default_factory=lambda: ["Falha parcial / portada"])
    em_conflito: list[str] = field(default_factory=lambda: ["Em conflito"])
    motivo_recusa_vazio: list[str] = field(default_factory=lambda: ["", "-", "--", "—"])
    texto_fila_em_andamento: str = "Em andamento"


@dataclass
class MatchRules:
    strip_ddi_55: bool = True              # 55 + DDD + número -> DDD + número (só com 12/13 dígitos)
    strip_trunk_zero: bool = False         # 0 + DDD + número -> DDD + número
    usar_numero_temporario: bool = False   # Temporário (Excel) x Número Temporário (bilhete), se inequívoco
    multiplos_bilhetes: str = "mais_recente"  # mais_recente (se a data comprovar) | revisar


@dataclass
class PhoenixSettings:
    base_url: str = "https://phoenix2businesspcs.timbrasil.com.br/phoenix2Business/"
    browser_channel: str = "msedge"        # Chromium do Edge instalado; "chromium" usa o do Playwright
    headless: bool = False                 # a 1ª integração e o login são sempre com navegador visível
    interval_seconds: float = 3.0
    max_per_minute: int = 0
    timeout_seconds: float = 45.0
    max_retries: int = 2
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 60.0
    max_paginas: int = 50
    persist_session: bool = True           # cookies da sessão protegidos com DPAPI
    autosave_every: int = 5
    output_dir: str = "output/phoenix"
    status: StatusRules = field(default_factory=StatusRules)
    match: MatchRules = field(default_factory=MatchRules)

    def validate(self) -> "PhoenixSettings":
        if self.match.multiplos_bilhetes not in MULTIPLE_TICKET_RULES:
            raise ConfigError(f"multiplos_bilhetes deve ser {' ou '.join(MULTIPLE_TICKET_RULES)}")
        if self.timeout_seconds <= 0 or self.interval_seconds < 0 or self.max_retries < 0:
            raise ConfigError("Timeout, intervalo e tentativas precisam ser positivos.")
        if not self.status.em_andamento or not self.status.em_conflito:
            raise ConfigError("As listas de status 'em_andamento' e 'em_conflito' não podem ficar vazias.")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PhoenixSettings":
        data = dict(data or {})
        status = _from_dict(StatusRules, data.pop("status", None))
        match = _from_dict(MatchRules, data.pop("match", None))
        settings = _from_dict(cls, data)
        settings.status, settings.match = status, match
        return settings


def load_settings() -> PhoenixSettings:
    return PhoenixSettings.from_dict(_read_json("settings.json")).validate()


def save_settings(settings: PhoenixSettings) -> Path:
    return _write_json("settings.json", settings.validate().to_dict())


# ------------------------------------------------------------------ mapeamento
@dataclass
class SheetMapping:
    sheet: str
    header_row: int = 1
    cnpj: str | None = None
    acesso: str | None = None
    fila_atual: str | None = None            # None: a exportação cria a coluna "Fila Atual"
    numero_temporario: str | None = None
    expected_headers: dict[str, str] = field(default_factory=dict)  # letra -> cabeçalho esperado
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("sheet")
        return data


def load_mappings() -> dict[str, SheetMapping]:
    raw = _read_json("columns_mapping.json").get("abas", {})
    return {name: SheetMapping(sheet=name, **{k: v for k, v in m.items() if k in {f.name for f in fields(SheetMapping)}})
            for name, m in raw.items()}


def save_mappings(mappings: dict[str, SheetMapping], reference: str = "") -> Path:
    current = _read_json("columns_mapping.json")
    current["abas"] = {name: m.to_dict() for name, m in sorted(mappings.items())}
    if reference:
        current["planilha_referencia"] = reference
    return _write_json("columns_mapping.json", current)


# -------------------------------------------------------------------- seletores
DEFAULT_SELECTORS: dict[str, Any] = {
    "_status": "Textos informados pelo operador; NÃO confirmados no HTML real. Confirme com "
               "'phoenix_cli.py inspecionar' e registre o resultado em docs/PHOENIX.md.",
    "login": {
        "indicador_logado_texto": "Portabilidade",
        "indicadores_pagina_login": ["input[type=password]"],
        # Observado no servidor real (27/10/2026): sem sessão ele responde HTTP 401 (Bearer) com a
        # página de SSO "Click Provider Link".
        "titulos_pagina_login": ["Click Provider Link"],
        "textos_sessao_expirada": ["sessão expirada", "sessao expirada", "efetue o login", "faça o login novamente"],
        "textos_captcha": ["captcha", "não sou um robô", "nao sou um robo"],
    },
    "menu": {"portabilidade": "Portabilidade", "acompanhamento": "Acompanhamento",
             "status_solicitacoes": "Status Solicitações", "espera_hover_ms": 400},
    "consulta": {
        "titulo_tela": "Status das Solicitações",
        "rotulo_tipo_consulta": "Tipo da Consulta",
        "opcao_cnpj": "CNPJ",
        "rotulo_valor": "Valor",
        "rotulos_periodo": ["Período inicial", "Período final"],
        "botao_pesquisar": "Pesquisar",
        "texto_sem_resultado": "Nenhuma Solicitação foi encontrada.",
        "textos_erro": ["ocorreu um erro", "erro inesperado"],
    },
    "resultados": {
        "colunas_identificacao": ["Razão Social", "Status Solicitação"],
        "colunas": ["Razão Social", "CNPJ", "Data Solicitação", "Tipo", "Status Solicitação", "Tipo Port In"],
        "botao_detalhar": "Detalhar",
        "textos_proxima_pagina": ["Próxima", "Próximo", "Próxima página", "»", ">"],
    },
    "detalhe": {
        "titulo": "Detalhes Solicitação",
        "secao_bilhetes": "Bilhetes",
        "colunas_identificacao": ["Número a portar", "Status do Bilhete"],
        "colunas_bilhetes": {
            "numero_portar": "Número a portar",
            "bilhete": "Bilhete",
            "numero_temporario": "Número Temporário",
            "status_bilhete": "Status do Bilhete",
            "autorizacao_doadora": "Autorização Doadora",
            "autorizado_ea": "Autorizado pela EA",
            "motivo_recusa": "Motivo Recusa",
            "status_linha": "Status da Linha",
            "motivo_falha": "Motivo da Falha",
        },
        "botao_voltar": "Voltar",
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_selectors() -> dict[str, Any]:
    return _merge(DEFAULT_SELECTORS, _read_json("selectors.json"))
