"""Descoberta de adaptadores: classes Python (pasta ``adapters``) e sites JSON (``config/sites``)."""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import adapters
from adapters.base_adapter import ApiAdapter, BaseAdapter, BrowserAdapter
from adapters.declarative_adapter import DeclarativeSiteAdapter, SiteDefinition, make_adapter_class
from adapters.demo_adapter import DemoApiAdapter, DemoBrowserAdapter, DemoLoginAdapter
from core import paths
from core.models import ConfigError
from utils.logger import get_logger

log = get_logger("registry")

BUILTIN_ADAPTERS: list[type[BaseAdapter]] = [DemoBrowserAdapter, DemoLoginAdapter, DemoApiAdapter]
_ABSTRACT = {BaseAdapter, BrowserAdapter, ApiAdapter, DeclarativeSiteAdapter}


@dataclass
class AdapterInfo:
    id: str
    name: str
    description: str
    kind: str
    fields: list[tuple[str, str]]
    source: str
    cls: type[BaseAdapter]

    def create(self, options: dict[str, Any] | None = None) -> BaseAdapter:
        return self.cls(options)

    def requires_login(self, options: dict[str, Any] | None = None) -> bool:
        return self.create(options).requires_login


def _info(cls: type[BaseAdapter], source: str) -> AdapterInfo:
    return AdapterInfo(cls.id, cls.display_name or cls.id, cls.description, cls.kind, list(cls.fields), source, cls)


def _python_adapters() -> list[type[BaseAdapter]]:
    """Classes de adaptador em módulos ``adapters/*_adapter.py`` (inclui os criados pelo usuário)."""
    found: list[type[BaseAdapter]] = list(BUILTIN_ADAPTERS)
    for module_info in pkgutil.iter_modules(adapters.__path__):
        if not module_info.name.endswith("_adapter") or module_info.name in ("base_adapter", "declarative_adapter"):
            continue
        try:
            module = importlib.import_module(f"adapters.{module_info.name}")
        except Exception as exc:
            log.error("Adaptador %s não pôde ser carregado: %s", module_info.name, exc)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseAdapter) and obj not in _ABSTRACT and obj.id and obj not in found:
                found.append(obj)
    return found


def _site_files() -> list[Path]:
    files: dict[str, Path] = {}
    for folder in (paths.BUNDLED_SITES_DIR, paths.SITES_DIR):  # a pasta do usuário tem prioridade
        if folder.is_dir():
            for path in sorted(folder.glob("*.json")):
                files[path.name] = path
    return list(files.values())


def discover() -> dict[str, AdapterInfo]:
    registry: dict[str, AdapterInfo] = {}
    for cls in _python_adapters():
        registry[cls.id] = _info(cls, f"python:{cls.__module__}")
    for path in _site_files():
        try:
            definition = SiteDefinition.load(path)
        except ConfigError as exc:
            log.error("Site ignorado: %s", exc)
            continue
        if definition.id in registry and registry[definition.id].source.startswith("python:"):
            log.error("Site %s ignorado: id '%s' já usado por um adaptador Python", path.name, definition.id)
            continue
        registry[definition.id] = _info(make_adapter_class(definition), f"json:{path}")
    return registry


def get_adapter_info(adapter_id: str) -> AdapterInfo:
    registry = discover()
    if adapter_id not in registry:
        raise ConfigError(f"Adaptador '{adapter_id}' não encontrado. Disponíveis: {', '.join(sorted(registry))}")
    return registry[adapter_id]


def create_adapter(adapter_id: str, options: dict[str, Any] | None = None) -> BaseAdapter:
    return get_adapter_info(adapter_id).create(options)
