// Página de pesquisa da demonstração: carrega o resultado de forma assíncrona,
// como muitos sites reais, para exercitar as esperas automáticas do Playwright.
(function () {
  const form = document.getElementById("search-form");
  const input = document.getElementById("q");
  const result = document.getElementById("result");
  const captcha = document.getElementById("captcha");
  const captchaCheck = document.getElementById("captcha-check");
  const captchaConfirm = document.getElementById("captcha-confirm");

  const LABELS = {
    nome: "Nome / Razão social",
    situacao: "Situação",
    data_registro: "Data de registro",
    municipio: "Município",
    uf: "UF",
    detalhes: "Detalhes",
  };

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function setState(state, query, html, extra) {
    result.dataset.state = state;
    result.dataset.query = query;
    result.innerHTML = html || "";
    Object.entries(extra || {}).forEach(([k, v]) => (result.dataset[k] = v));
    captcha.hidden = state !== "captcha";
  }

  async function search(query) {
    setState("loading", query, '<p class="loading">Carregando…</p>');
    let resp, body;
    try {
      // pequeno atraso no cliente para simular carregamento dinâmico
      await new Promise((r) => setTimeout(r, 200 + Math.random() * 300));
      resp = await fetch("/api/search?q=" + encodeURIComponent(query), { credentials: "same-origin" });
      body = await resp.json().catch(() => ({}));
    } catch (err) {
      setState("error", query, '<p class="error" data-testid="error-message">Falha de comunicação com o servidor.</p>');
      return;
    }
    const message = escapeHtml(body.message || "");
    if (resp.status === 200 && body.status === "found") {
      const rows = Object.entries(body.data)
        .map(([key, value]) => `<dt>${LABELS[key] || key}</dt><dd data-field="${key}">${escapeHtml(value)}</dd>`)
        .join("");
      setState("found", query, `<h2>Resultado para ${escapeHtml(query)}</h2><dl>${rows}</dl>`);
    } else if (resp.status === 200 && body.status === "not_found") {
      setState("not-found", query, `<p class="empty" data-testid="not-found-message">${message}</p>`);
    } else if (resp.status === 401) {
      setState("session-expired", query, '<p class="warn">Sessão expirada. <a href="/login?expired=1">Entrar novamente</a></p>');
    } else if (resp.status === 403 && body.captcha) {
      setState("captcha", query, `<p class="warn">${message}</p>`);
    } else if (resp.status === 429) {
      setState("rate-limited", query, `<p class="warn">${message}</p>`, { retryAfter: String(body.retry_after || "") });
    } else {
      setState("error", query, `<p class="error" data-testid="error-message">${message || "Erro"}</p>`, { httpStatus: String(resp.status) });
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    search(input.value.trim());
  });

  captchaConfirm.addEventListener("click", async () => {
    if (!captchaCheck.checked) return;
    await fetch("/api/human-check", { method: "POST", credentials: "same-origin" });
    captchaCheck.checked = false;
    search(result.dataset.query || input.value.trim());
  });
})();
