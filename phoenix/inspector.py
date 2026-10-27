"""Inspeção do HTML real do Phoenix2 para confirmar os locators (primeira integração).

Grava em logs/inspecao/<data>/ um resumo JSON por página (frames, formulários, selects e opções,
botões, links, tabelas e seus cabeçalhos, elementos de menu) e o HTML de cada frame com sequências
de 6+ dígitos mascaradas (CNPJ/telefone viram 9999...). Os arquivos ficam só nesta máquina.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from core import paths

JS_SUMMARY = r"""
(menuTexts) => {
  const txt = el => (el.innerText || el.value || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const attrs = el => {
    const out = {tag: el.tagName.toLowerCase()};
    for (const a of ['id', 'name', 'type', 'class', 'role', 'href', 'onclick', 'onmouseover', 'target', 'for', 'title', 'alt']) {
      const v = el.getAttribute(a); if (v) out[a] = v.slice(0, 120);
    }
    return out;
  };
  const mask = s => (s || '').replace(/\d{6,}/g, m => '9'.repeat(m.length));
  return {
    url: location.href.replace(/\d{6,}/g, m => '9'.repeat(m.length)),
    title: document.title,
    forms: Array.from(document.forms).map(f => ({...attrs(f), action: f.getAttribute('action'), method: f.getAttribute('method')})),
    inputs: Array.from(document.querySelectorAll('input, textarea')).filter(e => e.type !== 'hidden').map(e => ({...attrs(e), visible: vis(e)})),
    hidden_inputs: Array.from(document.querySelectorAll('input[type=hidden]')).map(e => e.name || e.id),
    selects: Array.from(document.querySelectorAll('select')).map(s => ({...attrs(s), visible: vis(s), options: Array.from(s.options).map(o => o.text.trim())})),
    labels: Array.from(document.querySelectorAll('label')).map(l => ({for: l.htmlFor, text: txt(l)})),
    buttons: Array.from(document.querySelectorAll('button, input[type=submit], input[type=button], input[type=image]')).map(b => ({...attrs(b), text: txt(b), visible: vis(b)})),
    links: Array.from(document.querySelectorAll('a')).filter(vis).slice(0, 300).map(a => ({...attrs(a), text: txt(a)})),
    menu_matches: Array.from(document.querySelectorAll('body *')).filter(el => {
      const own = Array.from(el.childNodes).filter(n => n.nodeType === 3).map(n => n.nodeValue).join(' ').replace(/\s+/g, ' ').trim();
      return own && menuTexts.some(t => own.toLowerCase() === t.toLowerCase());
    }).map(el => ({...attrs(el), text: txt(el), visible: vis(el), parent: attrs(el.parentElement)})),
    tables: Array.from(document.querySelectorAll('table')).map((t, i) => {
      const rows = Array.from(t.rows);
      const head = rows.slice(0, 4).map(r => Array.from(r.cells).map(c => mask(txt(c))));
      return {index: i, ...attrs(t), rows: rows.length, first_rows_masked: head,
              radios: t.querySelectorAll('input[type=radio]').length, nested_tables: t.querySelectorAll('table').length};
    }).filter(t => t.rows > 0),
    iframes: Array.from(document.querySelectorAll('iframe, frame')).map(f => ({...attrs(f), src: (f.getAttribute('src') || '').slice(0, 150)})),
  };
}"""


def capture(page, label: str, menu_texts: list[str], out_dir: Path | None = None) -> Path:
    """Captura a página atual (todos os frames). Devolve a pasta onde gravou."""
    out_dir = out_dir or (paths.LOGS_DIR / "inspecao" / dt.datetime.now().strftime("%Y%m%d"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", label)
    summary: dict[str, Any] = {"etapa": label, "capturado_em": dt.datetime.now().isoformat(timespec="seconds"),
                               "paginas_abertas": len(page.context.pages), "frames": []}
    for i, frame in enumerate(page.frames):
        entry: dict[str, Any] = {"indice": i, "nome": frame.name, "e_principal": frame is page.main_frame}
        try:
            entry.update(frame.evaluate(JS_SUMMARY, menu_texts))
            html = re.sub(r"\d{6,}", lambda m: "9" * len(m.group()), frame.content())
            html_path = out_dir / f"{stamp}_{safe}_frame{i}.html"
            html_path.write_text(html, encoding="utf-8")
            entry["html"] = html_path.name
        except Exception as exc:
            entry["erro"] = str(exc)
        summary["frames"].append(entry)
    path = out_dir / f"{stamp}_{safe}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
