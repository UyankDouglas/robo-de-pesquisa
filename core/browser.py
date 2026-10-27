"""Gerenciamento do navegador com Playwright (API síncrona).

O Playwright síncrono deve ser usado sempre na mesma thread que o criou — por isso o motor
abre o navegador dentro da thread de processamento.

Por padrão é usado o Microsoft Edge já instalado no Windows (``channel="msedge"``), o que
dispensa baixar navegadores. Não há qualquer técnica de disfarce/evasão: o navegador é usado
como um usuário comum o usaria.
"""
from __future__ import annotations

from typing import Any

from utils.logger import get_logger

log = get_logger("browser")

FALLBACK_ORDER = {
    "msedge": ("msedge", "chrome", "chromium"),
    "chrome": ("chrome", "msedge", "chromium"),
    "chromium": ("chromium", "msedge", "chrome"),
}


class BrowserLaunchError(RuntimeError):
    pass


class BrowserManager:
    def __init__(
        self,
        headless: bool = False,
        channel: str = "msedge",
        timeout_seconds: float = 30.0,
        storage_state: dict[str, Any] | None = None,
        locale: str = "pt-BR",
    ):
        self.headless = headless
        self.channel = channel
        self.timeout_ms = int(timeout_seconds * 1000)
        self.storage_state = storage_state
        self.locale = locale
        self._pw = None
        self.browser = None
        self.context = None
        self._page = None
        self.active_channel: str | None = None

    # ------------------------------------------------------------- ciclo de vida
    def start(self) -> "BrowserManager":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._launch()
            self._new_context()
        except Exception:
            self.close()
            raise
        return self

    def _launch(self) -> None:
        errors = []
        for channel in FALLBACK_ORDER.get(self.channel, (self.channel,)):
            try:
                kwargs: dict[str, Any] = {"headless": self.headless}
                if channel != "chromium":
                    kwargs["channel"] = channel
                self.browser = self._pw.chromium.launch(**kwargs)
                self.active_channel = channel
                if channel != self.channel:
                    log.warning("Navegador '%s' indisponível; usando '%s'.", self.channel, channel)
                return
            except Exception as exc:  # navegador não instalado etc.
                errors.append(f"{channel}: {str(exc).splitlines()[0]}")
        raise BrowserLaunchError(
            "Não foi possível abrir nenhum navegador. Instale o Microsoft Edge/Google Chrome "
            "ou execute 'python -m playwright install chromium'.\n" + "\n".join(errors)
        )

    def _new_context(self) -> None:
        self.context = self.browser.new_context(
            storage_state=self.storage_state,
            locale=self.locale,
            viewport={"width": 1280, "height": 860},
        )
        self.context.set_default_timeout(self.timeout_ms)
        self.context.set_default_navigation_timeout(self.timeout_ms)
        self._page = self.context.new_page()

    def close(self) -> None:
        for closer in (
            lambda: self.context and self.context.close(),
            lambda: self.browser and self.browser.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception:  # pragma: no cover - fechamento sempre "melhor esforço"
                pass
        self._pw = self.browser = self.context = self._page = None

    def __enter__(self) -> "BrowserManager":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ página
    @property
    def page(self):
        return self.ensure_page()

    def ensure_page(self):
        """Recria a aba (ou o navegador) se o operador fechou a janela ou ela travou."""
        if self.browser is None or not self.browser.is_connected():
            log.warning("Navegador foi fechado; abrindo novamente.")
            self.storage_state = self._safe_storage_state() or self.storage_state
            self._launch()
            self._new_context()
        elif self._page is None or self._page.is_closed():
            try:
                self._page = self.context.new_page()
            except Exception:
                self._new_context()
        return self._page

    def _safe_storage_state(self) -> dict[str, Any] | None:
        try:
            return self.context.storage_state() if self.context else None
        except Exception:
            return None

    def export_storage_state(self) -> dict[str, Any] | None:
        return self._safe_storage_state()

    def bring_to_front(self) -> None:
        try:
            self.page.bring_to_front()
        except Exception:
            pass


class BorrowedBrowser:
    """Repassa ao motor um navegador já aberto (com o login manual feito) sem fechá-lo no fim do lote.

    Deve ser usado na mesma thread que criou o navegador (exigência do Playwright síncrono).
    """

    def __init__(self, inner: BrowserManager):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        pass
