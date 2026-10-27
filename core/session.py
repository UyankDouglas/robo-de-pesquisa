"""Persistência protegida de sessões autenticadas (cookies/localStorage do Playwright).

Os dados são criptografados com a DPAPI do Windows (CryptProtectData), vinculada à conta do
usuário no computador: o arquivo copiado para outra máquina/usuário não pode ser lido.
Nenhuma senha é armazenada — o login é sempre feito manualmente pelo operador.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from core import paths
from utils.logger import get_logger

log = get_logger("session")

MAGIC = b"WSB1"
_ENTROPY = b"WebSearchBot/session/v1"


class DpapiError(RuntimeError):
    pass


def dpapi_available() -> bool:
    return sys.platform == "win32"


if dpapi_available():
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _proto = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p,
              ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.argtypes = _proto
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _unproto = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    _crypt32.CryptUnprotectData.argtypes = _unproto
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _UI_FORBIDDEN = 0x01

    def _to_blob(data: bytes) -> tuple[_Blob, Any]:
        buffer = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    def _from_blob(blob: _Blob) -> bytes:
        try:
            return ctypes.string_at(blob.pbData, blob.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))

    def protect(data: bytes) -> bytes:
        src, _keep1 = _to_blob(data)
        ent, _keep2 = _to_blob(_ENTROPY)
        out = _Blob()
        if not _crypt32.CryptProtectData(ctypes.byref(src), "WebSearchBot", ctypes.byref(ent), None, None,
                                         _UI_FORBIDDEN, ctypes.byref(out)):
            raise DpapiError(f"CryptProtectData falhou (erro {ctypes.get_last_error()})")
        return _from_blob(out)

    def unprotect(data: bytes) -> bytes:
        src, _keep1 = _to_blob(data)
        ent, _keep2 = _to_blob(_ENTROPY)
        out = _Blob()
        if not _crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                                           _UI_FORBIDDEN, ctypes.byref(out)):
            raise DpapiError(f"CryptUnprotectData falhou (erro {ctypes.get_last_error()})")
        return _from_blob(out)

else:  # pragma: no cover - o projeto é voltado ao Windows

    def protect(data: bytes) -> bytes:
        raise DpapiError("Proteção de sessão disponível apenas no Windows (DPAPI).")

    def unprotect(data: bytes) -> bytes:
        raise DpapiError("Proteção de sessão disponível apenas no Windows (DPAPI).")


class SessionStore:
    """Guarda o ``storage_state`` do Playwright criptografado por adaptador."""

    def __init__(self, folder: Path | None = None):
        self.folder = folder or paths.SESSIONS_DIR

    def path_for(self, adapter_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", adapter_id)
        return self.folder / f"{safe}.session.bin"

    def exists(self, adapter_id: str) -> bool:
        return self.path_for(adapter_id).exists()

    def load(self, adapter_id: str) -> dict[str, Any] | None:
        path = self.path_for(adapter_id)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            if not raw.startswith(MAGIC):
                raise DpapiError("formato desconhecido")
            return json.loads(unprotect(raw[len(MAGIC):]).decode("utf-8"))
        except (DpapiError, ValueError, OSError) as exc:
            log.warning("Sessão salva de '%s' não pôde ser lida (%s); será necessário novo login.", adapter_id, exc)
            return None

    def save(self, adapter_id: str, state: dict[str, Any]) -> Path | None:
        try:
            payload = MAGIC + protect(json.dumps(state).encode("utf-8"))
        except DpapiError as exc:
            log.warning("Sessão não foi salva: %s", exc)
            return None
        path = self.path_for(adapter_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, path)
        return path

    def clear(self, adapter_id: str) -> bool:
        path = self.path_for(adapter_id)
        if path.exists():
            path.unlink()
            return True
        return False
