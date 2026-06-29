"""
Peticionamento eletronico via Loy Legal (app.loylegal.com).

COMO CHEGA LA: a fila do NPJUR tem a funcao jsPeticionamentoEletronico(...)
na linha do agendamento — ela abre o Loy ja autenticado e apontando pro
processo certo (URL /delivery/?signature=...&intermediate=...).

O QUE PREENCHE (visto nos videos do Lucas):
  - Categoria: "Petição"  (varia por estado — vem da planilha, coluna H)
  - TIPO: "Petição"       (coluna I)
  - SIGILO: "Sem Sigilo - Nível 0"
  - Upload do PDF
  - Botao Protocolar

SEGURANCA: protocolar e IRREVERSIVEL (peticao juridica real). Por padrao
(config confirmar_antes_protocolar: true) o robo preenche tudo, PAUSA, e
so protocola depois que o Lucas conferir a tela e der ENTER.

O Loy e um app moderno (SPA) — os campos podem ser selects nativos ou
componentes customizados. Usamos heuristicas em camadas; o que nao for
encontrado gera CalibracaoNecessaria + dump pra ajustar com 1 iteracao.
"""

import re
from pathlib import Path
from playwright.sync_api import Page, TimeoutError as PWTimeout
from rich.console import Console

from .npjur import CalibracaoNecessaria, _normalizar

console = Console()


class LoyLegal:
    def __init__(self, espera_reload: float = 2.0, timeout_pagina: int = 30):
        self.espera_reload = espera_reload
        self.timeout = timeout_pagina * 1000

    # -------------------------------------------------------------------------
    def abrir_pelo_processo(self, context, npjur: str) -> Page:
        """
        Abre o Cadastro de Processos e clica em "PETICIONAMENTO ELETRÔNICO"
        no menu lateral esquerdo -> abre o Loy em nova aba.

        Fluxo confirmado pelo Lucas: o Loy se abre de DENTRO do processo.
        """
        from .npjur import URL_CADASTRO_PROCESSO

        url = URL_CADASTRO_PROCESSO.format(npjur=npjur)
        console.log(f"[blue]→ Abrindo processo pra peticionar:[/blue] {url}")
        cad = context.new_page()
        try:
            cad.goto(url, timeout=self.timeout, wait_until="domcontentloaded")
            cad.wait_for_timeout(int(self.espera_reload * 1000))

            botao = cad.get_by_text(
                re.compile(r"PETICIONAMENTO ELETR", re.I)
            ).first
            if botao.count() == 0:
                raise CalibracaoNecessaria(
                    "Botao 'PETICIONAMENTO ELETRÔNICO' nao encontrado no menu "
                    "lateral do Cadastro de Processos."
                )
            console.log("[blue]   → Clicando em PETICIONAMENTO ELETRÔNICO...[/blue]")
            try:
                with context.expect_page(timeout=self.timeout) as pop_info:
                    botao.click()
                loy = pop_info.value
            except PWTimeout:
                raise CalibracaoNecessaria(
                    "Cliquei em PETICIONAMENTO ELETRÔNICO mas nenhuma aba nova "
                    "abriu (pode ter painel intermediario nessa tela)."
                )
            loy.wait_for_load_state("domcontentloaded", timeout=self.timeout)
        finally:
            # so fecha o cadastro DEPOIS que o Loy carregou (ou deu erro)
            try:
                cad.close()
            except Exception:
                pass
        # SPA: espera renderizar de verdade
        try:
            loy.wait_for_load_state("networkidle", timeout=self.timeout)
        except Exception:
            pass
        loy.wait_for_timeout(int(self.espera_reload * 1000))
        console.log(f"[green]   ✓ Loy aberto:[/green] {loy.url[:90]}")
        return loy

    # -------------------------------------------------------------------------
    def preencher(self, loy: Page, caminho_pdf: Path, categoria: str,
                  tipo_arquivo: str, tipo_peticao: str, envolvimento: str):
        """Preenche o formulario de peticionamento (NAO protocola ainda)."""

        # 1. Upload do PDF — input[type=file] (mesmo oculto, set_input_files funciona)
        arquivos = loy.locator("input[type=file]")
        if arquivos.count() == 0:
            raise CalibracaoNecessaria("Loy: nenhum input[type=file] encontrado pra upload.")
        arquivos.first.set_input_files(str(caminho_pdf))
        console.log(f"[green]   ✓ PDF enviado:[/green] {Path(caminho_pdf).name}")
        loy.wait_for_timeout(int(self.espera_reload * 1000))

        # 2. Campos de classificacao. "Não utilizado" na planilha = pula o campo.
        campos = [
            ("Categoria", categoria),
            ("Tipo", tipo_arquivo),
            ("Sigilo", "Sem Sigilo"),
            ("Tipo de Petição", tipo_peticao),
            ("Envolvimento", envolvimento),
        ]
        pendentes = []
        for rotulo, valor in campos:
            if not valor or _normalizar(valor).startswith("NAO UTILIZADO"):
                continue
            if not self._preencher_campo(loy, rotulo, valor):
                pendentes.append((rotulo, valor))

        if pendentes:
            console.log(f"[yellow]   ! Campos que NAO consegui preencher no Loy: {pendentes}[/yellow]")
            console.log("[yellow]     (confira/preencha manualmente na tela antes de protocolar)[/yellow]")
        return pendentes

    def _preencher_campo(self, loy: Page, rotulo: str, valor: str) -> bool:
        """Tenta preencher um campo do Loy em camadas:
        1) <select> nativo associado ao label
        2) <select> nativo que contenha o valor entre as opcoes
        3) combobox customizado: clica no elemento com o rotulo e depois na opcao
        """
        alvo_norm = _normalizar(valor)

        # camada 1: get_by_label com select_option
        try:
            campo = loy.get_by_label(re.compile(rotulo, re.I)).first
            if campo.count() > 0:
                campo.select_option(label=re.compile(re.escape(valor), re.I), timeout=3000)
                console.log(f"[green]   ✓ {rotulo} = {valor}[/green] (label)")
                return True
        except Exception:
            pass

        # camada 2: qualquer select nativo cujas opcoes contenham o valor
        try:
            ok = loy.evaluate(
                """(args) => {
                    const [alvo] = args;
                    const norm = (s) => s.toUpperCase()
                        .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                        .replace(/\\s+/g, ' ').trim();
                    for (const sel of document.querySelectorAll('select')) {
                        for (const opt of sel.options) {
                            const t = norm(opt.text);
                            if (t && (t === alvo || t.includes(alvo))) {
                                sel.value = opt.value;
                                sel.dispatchEvent(new Event('change', { bubbles: true }));
                                sel.dispatchEvent(new Event('input', { bubbles: true }));
                                return true;
                            }
                        }
                    }
                    return false;
                }""",
                [alvo_norm],
            )
            if ok:
                console.log(f"[green]   ✓ {rotulo} = {valor}[/green] (select)")
                return True
        except Exception:
            pass

        # camada 3: combobox customizado (div clicavel + lista de opcoes)
        try:
            gatilho = loy.get_by_text(re.compile(rf"^\s*{rotulo}", re.I)).first
            gatilho.click(timeout=3000)
            loy.wait_for_timeout(800)
            opcao = loy.get_by_text(re.compile(re.escape(valor), re.I)).first
            opcao.click(timeout=3000)
            console.log(f"[green]   ✓ {rotulo} = {valor}[/green] (combobox)")
            return True
        except Exception:
            pass

        console.log(f"[yellow]   ✗ Nao preenchi: {rotulo} = {valor}[/yellow]")
        return False

    # -------------------------------------------------------------------------
    def protocolar(self, loy: Page) -> bool:
        """Clica o botao Protocolar. Chamar SO depois da confirmacao."""
        for tentativa in (
            loy.get_by_role("button", name=re.compile(r"protocolar", re.I)),
            loy.locator("button:has-text('Protocolar')"),
            loy.locator("input[type=button][value*='Protocolar' i], input[type=submit][value*='Protocolar' i]"),
        ):
            try:
                if tentativa.count() > 0 and tentativa.first.is_visible():
                    tentativa.first.click()
                    console.log("[green]✓ Protocolar clicado[/green]")
                    loy.wait_for_timeout(int(self.espera_reload * 1000 * 2))
                    return True
            except Exception:
                continue
        raise CalibracaoNecessaria("Loy: botao Protocolar nao encontrado.")
