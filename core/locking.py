"""Trava de execução: garante uma única instância processando cada execução.

Usa trava de arquivo do sistema operacional; se o processo morrer, o Windows libera a trava
automaticamente, então não existem travas "presas".
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from core import paths
from core.models import AlreadyRunningError

if sys.platform == "win32":
    import msvcrt
else:  # pragma: no cover
    import fcntl


class RunLock:
    def __init__(self, run_id: int, folder: Path | None = None):
        self.run_id = run_id
        self.path = (folder or paths.LOCKS_DIR) / f"run_{run_id}.lock"
        self._fd: int | None = None

    def acquire(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunningError(
                f"A execução {self.run_id} já está sendo processada por outra instância."
            ) from exc
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

    @staticmethod
    def is_locked(run_id: int, folder: Path | None = None) -> bool:
        probe = RunLock(run_id, folder)
        try:
            probe.acquire()
        except AlreadyRunningError:
            return True
        probe.release()
        return False
