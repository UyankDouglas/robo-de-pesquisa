"""Configurações de execução e do modo agendado (arquivos JSON)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from core import paths
from core.models import ConfigError

BROWSER_CHANNELS = ("msedge", "chrome", "chromium")
EXPORT_FILTERS = ("all", "found", "not_found", "errors", "processed", "pending")
VALIDATIONS = ("none", "digits", "cnpj", "cpf", "cpf_cnpj")


@dataclass
class RunSettings:
    """Parâmetros de uma execução. São gravados no banco para que a retomada use os mesmos valores."""

    adapter_id: str = "demo"
    adapter_options: dict[str, Any] = field(default_factory=dict)
    interval_seconds: float = 1.0        # espera mínima entre consultas (definida pelo operador)
    max_per_minute: int = 0              # limite adicional por minuto (0 = sem limite extra)
    timeout_seconds: float = 30.0        # timeout de cada operação no site
    max_retries: int = 3                 # novas tentativas para erros temporários
    backoff_base_seconds: float = 2.0    # espera progressiva: base * 2^(tentativa-1)
    backoff_max_seconds: float = 60.0
    headless: bool = False               # False = navegador visível (necessário para login/CAPTCHA)
    browser_channel: str = "msedge"      # msedge/chrome usam o navegador instalado; chromium usa o do Playwright
    persist_session: bool = False        # grava a sessão (cookies) protegida com DPAPI
    autosave_every: int = 10             # regrava o Excel a cada N registros
    autosave_seconds: float = 60.0       # ... ou a cada N segundos
    output_dir: str = ""
    mask_identifiers_in_logs: bool = False

    def validate(self) -> "RunSettings":
        if not self.adapter_id:
            raise ConfigError("Selecione o site/adaptador.")
        if self.interval_seconds < 0:
            raise ConfigError("O intervalo entre consultas não pode ser negativo.")
        if self.max_per_minute < 0:
            raise ConfigError("O limite por minuto não pode ser negativo.")
        if self.timeout_seconds <= 0:
            raise ConfigError("O timeout deve ser maior que zero.")
        if not 0 <= self.max_retries <= 20:
            raise ConfigError("Número de tentativas deve estar entre 0 e 20.")
        if self.backoff_base_seconds < 0 or self.backoff_max_seconds < 0:
            raise ConfigError("Os tempos de espera progressiva não podem ser negativos.")
        if self.browser_channel not in BROWSER_CHANNELS:
            raise ConfigError(f"Navegador inválido: {self.browser_channel}. Use {', '.join(BROWSER_CHANNELS)}.")
        if self.autosave_every < 1:
            raise ConfigError("O salvamento automático deve ocorrer a cada 1 registro ou mais.")
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RunSettings":
        data = data or {}
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)


@dataclass
class ImportOptions:
    file: str
    column: str
    sheet: str | None = None        # None = primeira aba
    header_row: int | None = None   # None = detecta a primeira linha preenchida
    start: int | None = None        # nº do registro inicial (1 = primeira linha de dados)
    end: int | None = None          # nº do registro final (inclusive)
    zero_pad: int | None = None     # completa com zeros à esquerda até N dígitos (ex.: 14 para CNPJ)
    validation: str = "none"

    def validate(self) -> "ImportOptions":
        if not self.file:
            raise ConfigError("Informe a planilha de entrada.")
        if not self.column:
            raise ConfigError("Informe a coluna de pesquisa.")
        if self.validation not in VALIDATIONS:
            raise ConfigError(f"Validação inválida: {self.validation}. Use {', '.join(VALIDATIONS)}.")
        if self.start is not None and self.start < 1:
            raise ConfigError("O registro inicial deve ser 1 ou maior.")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ConfigError("O registro final deve ser maior ou igual ao inicial.")
        return self


@dataclass
class JobConfig:
    """Configuração completa usada pelo modo sem interface (cli.py / Agendador de Tarefas)."""

    name: str
    input: ImportOptions
    settings: RunSettings
    export_filter: str = "all"
    resume: bool = True                 # retoma execução incompleta da mesma planilha/coluna/site
    retry_errors_on_resume: bool = False

    def validate(self) -> "JobConfig":
        self.input.validate()
        self.settings.validate()
        if self.export_filter not in EXPORT_FILTERS:
            raise ConfigError(f"Filtro de exportação inválido: {self.export_filter}.")
        return self


def resolve_path(value: str | Path, base: Path | None = None) -> Path:
    """Caminhos relativos são resolvidos a partir da pasta de dados do aplicativo."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base or paths.DATA_ROOT) / path
    return path.resolve()


def output_dir_for(settings: RunSettings) -> Path:
    return resolve_path(settings.output_dir) if settings.output_dir else paths.OUTPUT_DIR


def load_job_config(path: str | Path) -> JobConfig:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Arquivo de configuração não encontrado: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON inválido em {path}: {exc}") from exc
    return job_config_from_dict(raw)


def job_config_from_dict(raw: dict[str, Any]) -> JobConfig:
    try:
        inp = raw["input"]
        site = raw.get("site", {})
        execution = raw.get("execution", {})
        output = raw.get("output", {})
    except (KeyError, TypeError) as exc:
        raise ConfigError("A configuração precisa da seção 'input'.") from exc

    settings = RunSettings.from_dict(execution)
    settings.adapter_id = site.get("adapter", settings.adapter_id)
    settings.adapter_options = dict(site.get("options", {}))
    settings.output_dir = output.get("dir", settings.output_dir)

    config = JobConfig(
        name=raw.get("name", "Execução agendada"),
        input=ImportOptions(
            file=inp.get("file", ""),
            column=inp.get("column", ""),
            sheet=inp.get("sheet"),
            header_row=inp.get("header_row"),
            start=inp.get("start"),
            end=inp.get("end"),
            zero_pad=inp.get("zero_pad"),
            validation=inp.get("validation", "none"),
        ),
        settings=settings,
        export_filter=output.get("filter", "all"),
        resume=bool(raw.get("resume", True)),
        retry_errors_on_resume=bool(raw.get("retry_errors_on_resume", False)),
    )
    return config.validate()


def job_config_to_dict(config: JobConfig) -> dict[str, Any]:
    execution = config.settings.to_dict()
    adapter_id = execution.pop("adapter_id")
    options = execution.pop("adapter_options")
    output_dir = execution.pop("output_dir")
    return {
        "name": config.name,
        "input": asdict(config.input),
        "site": {"adapter": adapter_id, "options": options},
        "execution": execution,
        "output": {"dir": output_dir, "filter": config.export_filter},
        "resume": config.resume,
        "retry_errors_on_resume": config.retry_errors_on_resume,
    }


def save_job_config(config: JobConfig, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(job_config_to_dict(config), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
