"""
Auto-dump de DOM pra calibracao barata.

QUANDO UMA ETAPA FALHA (seletor nao encontrado, tela diferente do esperado),
o robo salva em logs/ um .txt compacto com TUDO que importa da tela:
  - URL e titulo de cada frame
  - todos os inputs, selects (com as opcoes), textareas e botoes
  - links e elementos com onclick (as funcoes JS do NPJUR)
  - primeiros ~1200 caracteres do texto visivel

Em vez de mandar print da tela pro Claude (imagem = caro em tokens),
o Lucas cola o conteudo desse .txt — texto puro, poucos KB, e o Claude
ajusta o seletor com precisao.
"""

import time
from pathlib import Path
from playwright.sync_api import Page

_JS_COLETA = r"""
() => {
    const out = [];
    out.push("URL: " + location.href);
    out.push("TITULO: " + document.title);

    for (const f of document.querySelectorAll("form")) {
        out.push("FORM id=" + (f.id || "-") + " name=" + (f.name || "-") +
                 " action=" + (f.getAttribute("action") || "-"));
    }

    const campos = document.querySelectorAll("input, select, textarea, button");
    for (const el of campos) {
        let d = "  " + el.tagName.toLowerCase() +
                " type=" + (el.type || "-") +
                " id=" + (el.id || "-") +
                " name=" + (el.name || "-");
        if (el.tagName === "SELECT") {
            const opts = Array.from(el.options).slice(0, 50)
                .map(o => o.text.trim()).filter(Boolean);
            d += " [" + opts.length + " opcoes] = " + opts.join(" | ");
            if (el.options.length > 50) d += " ...(+" + (el.options.length - 50) + ")";
            const so = el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
            if (so && so.value) d += " SELECIONADO=" + so.text.trim().slice(0, 60);
        } else if (el.type === "password") {
            if (el.value) d += " value=***";
        } else {
            const v = String(el.value || el.innerText || "").trim();
            if (v) d += " value=" + v.slice(0, 80);
        }
        const oc = el.getAttribute("onclick");
        if (oc) d += " onclick=" + oc.slice(0, 150);
        const ph = el.getAttribute("placeholder");
        if (ph) d += " placeholder=" + ph;
        if (el.offsetParent === null && el.type !== "hidden") d += " (INVISIVEL)";
        out.push(d);
    }

    for (const a of document.querySelectorAll("a[onclick], a[href^='javascript'], img[onclick], span[onclick], div[onclick], td[onclick]")) {
        const txt = (a.innerText || a.alt || a.title || "").trim().slice(0, 60);
        const acao = String(a.getAttribute("onclick") || a.getAttribute("href") || "").slice(0, 150);
        out.push("  acao '" + txt + "' -> " + acao);
    }

    for (const fr of document.querySelectorAll("iframe, frame")) {
        out.push("IFRAME id=" + (fr.id || "-") + " src=" + (fr.getAttribute("src") || "-"));
    }

    const texto = (document.body ? document.body.innerText : "")
        .replace(/\s+/g, " ").trim().slice(0, 1200);
    out.push("TEXTO VISIVEL: " + texto);
    return out.join("\n");
}
"""


def dump_pagina(page: Page, pasta_logs: Path, rotulo: str) -> Path | None:
    """Salva o raio-X da pagina (todos os frames) num .txt. Retorna o caminho."""
    pasta_logs = Path(pasta_logs)
    pasta_logs.mkdir(parents=True, exist_ok=True)
    destino = pasta_logs / f"dump_{rotulo}_{time.strftime('%H%M%S')}.txt"

    blocos = []
    try:
        for i, frame in enumerate(page.frames):
            try:
                blocos.append(f"===== FRAME {i} =====\n" + frame.evaluate(_JS_COLETA))
            except Exception as e:
                blocos.append(f"===== FRAME {i} (erro ao coletar: {e}) =====")
    except Exception as e:
        blocos.append(f"(erro geral no dump: {e})")

    destino.write_text("\n\n".join(blocos), encoding="utf-8")
    return destino


def dump_contexto(context, pasta_logs: Path, rotulo: str) -> list[Path]:
    """Dump de TODAS as paginas abertas do navegador (uma falha pode estar
    em qualquer popup aberto)."""
    caminhos = []
    for i, pg in enumerate(context.pages):
        try:
            c = dump_pagina(pg, pasta_logs, f"{rotulo}_pag{i}")
            if c:
                caminhos.append(c)
        except Exception:
            pass
    return caminhos
