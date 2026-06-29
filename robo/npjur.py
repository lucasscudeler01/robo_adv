"""
Operacoes no sistema NPJUR.

ARQUITETURA (descoberta via DOM inspection real):
  - A fila usa uma funcao JS `jsRetornaGarantias()` pra buscar (AJAX).
  - Cada linha (tr) tem id = id_agendamento (ex: 16954651). O NPJUR
    fica numa <td>. As acoes da linha sao funcoes JS:
      * fJS_OpenProcesso(npjur)         -> abre o Cadastro de Processos
      * fAbreTese(npjur)                -> atalho que abre o Gerador de Teses
      * fAnexarDocumento(idAgenda)      -> anexa documento
      * jsPeticionamentoEletronico(...) -> Loy Legal
      * fValidaEmendaInicial(...)       -> concluir agendamento
  - O Gerador de Teses tem URL DIRETA:
      /sistema/relatorios/rejugerapeca_juridica/cadastro.php?codigo={NPJUR}
    Campos:
      * #sNomeColunaPesq  -> input "Pesquisar Documento" (filtro do select)
      * #id_judoctese     -> select "Documento" (option.text = nome da tese)
      * #oGerarDoc        -> botao "Gerar Documento DOCX"

ESTRATEGIA: usar URLs diretas onde possivel (mais robusto que cliques em
menus) e funcoes JS diretas onde URLs nao bastam.

CALIBRACAO: os metodos novos (anexar_documento, lancar_acompanhamento,
cumprir_prazo) usam heuristicas genericas — quando nao acharem um campo,
levantam CalibracaoNecessaria e o main salva um dump da tela em logs/
pro Lucas colar no Claude.
"""

import re
import time
import unicodedata
from pathlib import Path
from playwright.sync_api import Page, Download, TimeoutError as PWTimeout
from rich.console import Console

from robo.inspecao import dump_pagina

console = Console()


# URLs diretas dentro do NPJUR (descobertas por inspecao real)
URL_GERADOR_TESES = (
    "https://npjur.paschoalotto.com.br"
    "/sistema/relatorios/rejugerapeca_juridica/cadastro.php?codigo={npjur}"
)
URL_CADASTRO_PROCESSO = (
    "https://npjur.paschoalotto.com.br"
    "/sistema/juprocesso/cadastro.php?codigo={npjur}&sLayer="
)
# Popup "NOVO DOCUMENTO" (Documentos Anexos) — visto no video do Lucas 10/06
URL_NOVO_DOCUMENTO = (
    "https://npjur.paschoalotto.com.br"
    "/sistema/judocdig/cadastro.php?codigo={npjur}&iNovo=1&sTipoChave=A"
)
# Popup "Cadastro de Acompanhamento Processual" — usa o id INTERNO do
# processo (id_juprocesso), nao o NPJUR. Visto no video do Lucas 10/06.
URL_ACOMPANHAMENTO = (
    "https://npjur.paschoalotto.com.br"
    "/sistema/juacompanha/cadastro.php?id_juprocesso={id_juprocesso}&sTipo=P&id_juarea=1"
)
# Mesma tela ja com o tipo selecionado. Selecionar o tipo no <select>
# dispara um reload pra esta URL (confirmado por teste ao vivo em 10/06);
# navegar direto evita o reload no meio da automacao.
URL_ACOMPANHAMENTO_COM_TIPO = (
    "https://npjur.paschoalotto.com.br"
    "/sistema/juacompanha/cadastro.php?codigo=&id_juprocesso={id_juprocesso}"
    "&sTipo=P&producao=&iVlrAcomp=&id_juarea=1&id_juacompanha=&id_juincidentes="
    "&id_jumovimentacao_tj=&sPainel=&id_jutipoacomp={id_tipo}&flag_paineis="
    "&iCtrl=1&flag_resp=1&iCtrlValAcordo=1&id_juinformativoretomado=&ids_publicacoes="
)
# Peticionamento eletronico (fluxo INTERNO do NPJUR/2ADV — mapeado ao vivo
# em 11/06/2026; NAO existe tela do Loy: o backend protocola sozinho)
URL_PETICIONAMENTO = (
    "https://npjur.paschoalotto.com.br"
    "/sistema/jupeticionamento_eletronico/cadastro.php?npjur={npjur}"
)
# Pagina de login — se cair aqui, a sessao expirou
RE_URL_LOGIN = re.compile(r"gelogin\.php", re.I)

RE_CNJ = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")

# Extrai da linha da fila: id_agendamento (id do tr), numero CNJ,
# id_juprocesso (id interno — aparece em jsValidarDepositoGarantia) e os
# titulos das acoes (icones) disponiveis na linha.
_JS_LINHA_FILA = """(ns) => {
    const lista = Array.isArray(ns) ? ns : [ns];
    const res = lista.map(n => new RegExp('\\\\b' + n + '\\\\b'));
    const rows = Array.from(document.querySelectorAll('tr'));
    const row = rows.find(r => r.id && /^\\d+$/.test(r.id)
        && res.some(re => re.test(r.innerText)));
    if (!row) return null;
    const m = row.innerText.match(/\\d{7}-\\d{2}\\.\\d{4}\\.\\d\\.\\d{2}\\.\\d{4}/);
    const jm = row.innerHTML.match(/jsValidarDepositoGarantia\\((\\d+)/);
    const acoes = [];
    for (const el of row.querySelectorAll('[onclick]')) {
        const nome = (el.getAttribute('title') || el.getAttribute('alt') || el.innerText || '').trim();
        if (nome) acoes.push(nome);
    }
    // qual variante do NPJUR casou de fato na linha (ex: '0879948') — as
    // etapas seguintes precisam desse valor canonico, nao o da planilha
    const casou = lista.find(n => new RegExp('\\\\b' + n + '\\\\b').test(row.innerText));
    return {
        id: row.id,
        cnj: m ? m[0] : null,
        id_juprocesso: jm ? jm[1] : null,
        npjur: casou || lista[0],
        acoes: acoes,
    };
}"""


class CalibracaoNecessaria(RuntimeError):
    """A tela nao bateu com o esperado — precisa de dump + ajuste de seletor."""


def _candidatos_npjur(npjur: str) -> list[str]:
    """Variantes do NPJUR pra buscar na fila. O Excel guarda o NPJUR como
    NUMERO e come o zero a esquerda (0879948 -> 879948), mas na fila ele
    aparece com o zero. Regra do Lucas (15/06): NPJUR com menos digitos =
    completar com zero a esquerda ate o padrao (7 digitos). Retorna o valor
    original E o zero-preenchido, sem duplicar."""
    n = str(npjur).strip()
    cands = [n]
    z = n.zfill(7)
    if z != n:
        cands.append(z)
    return cands


def _normalizar(s: str) -> str:
    """maiusculas, sem acento, espacos colapsados — pra comparar textos."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().upper()


def _forte(s: str) -> str:
    """Normalizacao FORTE: alem de maiusculas/sem acento, remove pontuacao
    (hifen, '->', etc.) e colapsa espacos. Assim 'PETIÇÃO - SIMPLES' (planilha)
    casa com 'PETIÇÃO SIMPLES' (Loy). Maiusc/minusc e pontuacao nao importam."""
    s = _normalizar(s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Listas de FALLBACK por campo do Loy (22/06, pedido do Lucas): cada
# tribunal/sistema (ESAJ, eproc, PJe...) nomeia os campos de um jeito. Em vez
# de depender do nome exato da planilha, o robo tenta o valor da planilha
# PRIMEIRO e, se ele nao existir no dropdown daquele tribunal, cai pra estes
# sinonimos na ordem. So da erro se NENHUM existir. (_loy_escolher casa contra
# as opcoes REAIS do dropdown, entao nunca re-protocola nem duplica.)
_FALLBACK_CATEGORIA = [
    "Petições Diversas", "Petição", "Manifestação da Parte", "Juntada -> Petição",
]
_FALLBACK_TIPO_ARQUIVO = [
    "Petição", "Manifestação", "Outros Documentos", "Outro",
]
_FALLBACK_TIPO_PETICAO = [
    "Petições Diversas", "Manifestação do Autor",
]


class CapturaDialogos:
    """Captura dialogs JS (alert/confirm) de uma page.

    decisor(mensagem) -> True pra aceitar (OK/Sim), False pra dispensar
    (Cancelar/Nao). Todas as mensagens ficam em .mensagens.
    """

    def __init__(self, page: Page, decisor=None):
        self.mensagens: list[str] = []
        self._decisor = decisor or (lambda msg: True)

        def _handler(dialog):
            self.mensagens.append(dialog.message)
            console.log(f"[magenta]   dialog ({dialog.type}):[/magenta] {dialog.message[:200]}")
            try:
                if self._decisor(dialog.message):
                    dialog.accept()
                else:
                    dialog.dismiss()
            except Exception:
                pass

        self._handler = _handler
        self._page = page
        page.on("dialog", _handler)

    def desligar(self):
        try:
            self._page.remove_listener("dialog", self._handler)
        except Exception:
            pass


class NPJUR:
    """Camada de automacao do NPJUR."""

    def __init__(self, page: Page, url_minha_fila: str, pasta_downloads: Path,
                 espera_reload: float = 2.0, timeout_pagina: int = 30,
                 pasta_logs: Path | None = None,
                 marcacao_processo: str = "PROCESSUAL",
                 vara_padrao: str = "1ª VARA CÍVEL"):
        self.page = page
        self.url_minha_fila = url_minha_fila
        self.pasta_downloads = pasta_downloads
        self.pasta_logs = Path(pasta_logs) if pasta_logs else None
        self.marcacao_processo = marcacao_processo
        self.vara_padrao = vara_padrao
        self.espera_reload = espera_reload
        self.timeout = timeout_pagina * 1000  # Playwright usa ms
        self._fila_atual: str | None = None  # NPJUR da ultima busca na fila

    def _esperar(self, page: Page | None = None, fator: float = 1.0):
        (page or self.page).wait_for_timeout(int(self.espera_reload * 1000 * fator))

    def _sessao_caiu(self, page: Page) -> bool:
        return bool(RE_URL_LOGIN.search(page.url or ""))

    def _goto_logado(self, page: Page, url: str):
        """goto que detecta queda de sessao (redirect pra gelogin.php) em
        QUALQUER tela — nao so na fila. Causa do erro de 10/06 22:41: o
        redirect pro login acontecia depois da checagem que so existia em
        abrir_minha_fila, e o robo procurava o processo na tela de login.
        Se cair no login, pausa pra login manual e tenta a URL de novo.
        Tambem sobrevive a TIMEOUT/erro de rede transitorio no goto (19/06,
        1344195: 'Page.goto: Timeout 30000ms' abrindo cadastro.php) — espera
        e tenta de novo em vez de derrubar o processo."""
        ultimo_erro = None
        for tentativa in range(1, 5):
            try:
                page.goto(url, timeout=self.timeout, wait_until="domcontentloaded")
            except Exception as e:
                ultimo_erro = e
                console.log(f"[yellow]   ! Falha ao abrir a pagina "
                            f"(tentativa {tentativa}/4): {str(e)[:70]} — "
                            f"esperando e tentando de novo...[/yellow]")
                try:
                    page.wait_for_timeout(2500)
                except Exception:
                    pass
                continue
            if not self._sessao_caiu(page):
                return
            console.print(
                "\n[bold red]SESSAO DO NPJUR CAIU.[/bold red] "
                "Faz login na janela do Chrome do robo e tecla ENTER aqui..."
            )
            input()
        if ultimo_erro is not None:
            raise ultimo_erro
        raise RuntimeError("Nao consegui sair da tela de login do NPJUR.")

    def _eval(self, page: Page, js: str, arg=None, tentativas: int = 3):
        """page.evaluate que sobrevive a navegacoes inesperadas do NPJUR.

        Paginas legadas do NPJUR recarregam/redirecionam em momentos
        imprevisiveis; se o contexto for destruido no meio do evaluate,
        espera a pagina estabilizar e tenta de novo.
        """
        ultima = None
        for tentativa in range(1, tentativas + 1):
            try:
                return page.evaluate(js, arg)
            except Exception as e:
                ultima = e
                if "context was destroyed" not in str(e) and "Cannot find context" not in str(e):
                    raise
                console.log(f"[yellow]   ! Pagina navegou no meio (tentativa {tentativa}) — esperando estabilizar...[/yellow]")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=self.timeout)
                except Exception:
                    pass
                self._esperar(page)
        raise ultima

    # -------------------------------------------------------------------------
    # PASSO 1: ir pra Minha Fila
    # -------------------------------------------------------------------------
    def abrir_minha_fila(self):
        """Navega para a tela Minha Fila de Agendamentos.

        Se a sessao tiver caido (redireciona pra gelogin.php), pausa e pede
        login manual — foi a causa do erro de 10/06 20:16.

        ALEM disso, o NPJUR as vezes redireciona a URL direta pra home
        (index.php) MESMO LOGADO — flagrado 12/06 16:26 no 1534239: a busca
        rodou na home e concluiu errado que o processo "nao esta na fila".
        Por isso confere que chegou MESMO na fila (URL + input #npjur) e
        re-tenta ate 3x.
        """
        console.log(f"[blue]→ Abrindo Minha Fila:[/blue] {self.url_minha_fila}")
        for tentativa in range(1, 4):
            self._goto_logado(self.page, self.url_minha_fila)
            self._esperar(self.page)
            na_fila = False
            if "MinhaFila" in (self.page.url or ""):
                try:
                    na_fila = bool(self._eval(
                        self.page, "() => !!document.getElementById('npjur')"
                    ))
                except Exception:
                    na_fila = False
            if na_fila:
                self._fila_atual = None
                return
            console.log(
                f"[yellow]   ! Nao chegou na Minha Fila (caiu em {self.page.url}) "
                f"— tentativa {tentativa}/3...[/yellow]"
            )
        raise RuntimeError(
            "NPJUR redirecionou a Minha Fila pra outra tela 3x seguidas "
            f"(ultima: {self.page.url}). Tela de busca nunca carregou."
        )

    # -------------------------------------------------------------------------
    # PASSO 2: buscar o NPJUR na fila + capturar id_agendamento e numero CNJ
    # -------------------------------------------------------------------------
    def buscar_npjur_na_fila(self, npjur: str, _tentativa: int = 1) -> dict | None:
        """
        Filtra a fila pelo NPJUR e retorna {'id': id_agendamento, 'cnj': numero}.

        O id_agendamento (id do <tr>) e usado nos passos seguintes
        (anexar, peticionar, concluir). O CNJ vira o nome do PDF.
        Se nao achar, retorna None.
        """
        candidatos = _candidatos_npjur(npjur)
        extra = f" (variantes: {candidatos})" if len(candidatos) > 1 else ""
        console.log(f"[blue]→ Buscando NPJUR {npjur} na fila...{extra}[/blue]")

        # A sessao pode cair DEPOIS de abrir_minha_fila (redirect JS tardio
        # ou no reload do filtro) — foi o erro de 10/06 22:41.
        if self._sessao_caiu(self.page):
            self.abrir_minha_fila()

        # Tenta cada variante do NPJUR (original e zero-preenchido). Pra cada
        # uma: preenche o filtro #npjur, dispara, e procura a linha (o JS casa
        # qualquer variante). O zfill cobre o caso do zero a esquerda comido
        # pelo Excel (15/06: 879948 na planilha = 0879948 na fila).
        for valor in candidatos:
            self._eval(
                self.page,
                """(n) => {
                    const inp = document.getElementById('npjur');
                    if (inp) inp.value = n;
                }""",
                valor,
            )

            # Dispara o filtro. Pode ser AJAX (sem navegar) OU reload da pagina
            # inteira — tratamos os dois cenarios.
            try:
                self.page.evaluate("typeof jsRetornaGarantias === 'function' && jsRetornaGarantias()")
            except Exception:
                # "Execution context destroyed" = a pagina navegou durante o
                # evaluate. Esperado — a funcao executou e a navegacao disparou.
                pass

            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=self.timeout)
            except Exception:
                pass
            self._esperar()

            # O reload do filtro tambem pode cair no login (gelogin.php?origem=
            # ...MinhaFila.php) — relogar e refazer a busca, nunca procurar a
            # linha numa tela de login.
            if self._sessao_caiu(self.page):
                if _tentativa >= 3:
                    raise RuntimeError("Sessao do NPJUR caindo repetidamente durante a busca na fila.")
                self.abrir_minha_fila()
                return self.buscar_npjur_na_fila(npjur, _tentativa + 1)

            resultado = self._eval(self.page, _JS_LINHA_FILA, candidatos)
            if resultado:
                console.log(
                    f"[green]   ✓ Encontrado (filtro '{valor}') — id_agendamento={resultado['id']}"
                    f" cnj={resultado.get('cnj') or '?'}"
                    f" id_juprocesso={resultado.get('id_juprocesso') or '?'}[/green]"
                )
                # guarda o canonico (com zero) pra garantir_na_fila/cumprir_prazo
                self._fila_atual = resultado.get("npjur") or npjur
                return resultado

        console.log(f"[yellow]   ! NPJUR {npjur} nao encontrado na fila{extra}[/yellow]")
        return None

    def garantir_na_fila(self, npjur: str) -> dict:
        """Garante que a aba principal esta na fila com o NPJUR filtrado
        (necessario antes de chamar as funcoes JS da linha)."""
        if self._fila_atual != npjur or "MinhaFila" not in (self.page.url or ""):
            self.abrir_minha_fila()
            info = self.buscar_npjur_na_fila(npjur)
            if not info:
                raise RuntimeError(f"NPJUR {npjur} nao encontrado na fila")
            return info
        # ja filtrado — re-le os dados da linha
        info = self._eval(self.page, _JS_LINHA_FILA, _candidatos_npjur(npjur))
        if not info:
            self.abrir_minha_fila()
            info = self.buscar_npjur_na_fila(npjur)
            if not info:
                raise RuntimeError(f"NPJUR {npjur} nao encontrado na fila")
        return info

    # -------------------------------------------------------------------------
    # PRE-REQUISITO: garantir que VARA esta preenchida no cadastro
    # -------------------------------------------------------------------------
    def garantir_vara_preenchida(self, npjur: str) -> bool:
        """
        Abre o Cadastro de Processos, verifica se a Vara esta preenchida.
        Se nao, preenche usando a sugestao em #sVara (texto vindo do CNJ)
        e salva via botao #gravar. Se a sugestao estiver vazia ou nao casar
        com nenhuma opcao, usa a vara padrao do config (vara_padrao).

        Retorna True se ja estava OK ou se conseguiu corrigir, False se
        nem a sugestao nem a vara padrao casaram com o dropdown.
        """
        url = URL_CADASTRO_PROCESSO.format(npjur=npjur)
        console.log(f"[blue]→ Verificando cadastro do processo:[/blue] {url}")

        cad = self.page.context.new_page()
        try:
            self._goto_logado(cad, url)
            cad.locator("#id_gevara").wait_for(state="attached", timeout=self.timeout)
            self._esperar(cad)

            ja_preenchida = cad.evaluate(
                """() => {
                    const sel = document.getElementById('id_gevara');
                    return sel && sel.value && sel.value !== '';
                }"""
            )
            if ja_preenchida:
                console.log("[green]   ✓ Vara ja preenchida[/green]")
                return True

            sugestao = cad.evaluate(
                """() => {
                    const inp = document.getElementById('sVara');
                    return inp ? inp.value : null;
                }"""
            )
            if sugestao:
                console.log(f"[blue]   → Vara sugerida:[/blue] {sugestao}")
            else:
                console.log(
                    f"[yellow]   ! Sem sugestao em #sVara — tentando vara padrao: "
                    f"{self.vara_padrao}[/yellow]"
                )

            resultado = cad.evaluate(
                """(args) => {
                    const norm = (s) => s
                        .toUpperCase()
                        .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                        .replace(/[ªº°]/g, '')
                        .replace(/\\s+DA\\s+COMARCA.*$/i, '')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const sel = document.getElementById('id_gevara');
                    if (!sel) return { ok: false, motivo: 'select id_gevara nao existe' };
                    const tentar = (texto) => {
                        if (!texto) return null;
                        const alvo = norm(texto);
                        for (const opt of sel.options) {
                            if (opt.value && norm(opt.text) === alvo) return opt;
                        }
                        for (const opt of sel.options) {
                            if (!opt.value) continue;
                            if (norm(opt.text).startsWith(alvo) || alvo.startsWith(norm(opt.text))) {
                                return opt;
                            }
                        }
                        return null;
                    };
                    let origem = 'sugestao';
                    let escolhida = tentar(args.sugestao);
                    if (!escolhida) { origem = 'padrao'; escolhida = tentar(args.padrao); }
                    if (!escolhida) {
                        return { ok: false, motivo: 'nenhuma opcao casa',
                                 alvo: args.sugestao, padrao: args.padrao };
                    }
                    sel.value = escolhida.value;
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    return { ok: true, escolhida: escolhida.text, origem };
                }""",
                {"sugestao": sugestao, "padrao": self.vara_padrao},
            )

            if not resultado.get("ok"):
                console.log(f"[red]   ✗ Nem sugestao nem vara padrao casaram: {resultado}[/red]")
                return False

            if resultado.get("origem") == "padrao":
                console.log(
                    f"[yellow]   ! Sugestao nao casou — usando vara padrao[/yellow]"
                )
            console.log(f"[green]   ✓ Selecionada:[/green] {resultado['escolhida']}")
            console.log("[blue]   → Clicando em Gravar...[/blue]")
            cad.locator("#gravar").click()
            try:
                cad.wait_for_load_state("domcontentloaded", timeout=self.timeout)
            except Exception:
                pass
            self._esperar(cad)
            console.log("[green]   ✓ Vara salva com sucesso[/green]")
            return True
        finally:
            try:
                cad.close()
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # PASSO 3+4: abrir Gerador de Teses (URL direta — pula a etapa do cadastro)
    # -------------------------------------------------------------------------
    def abrir_gerador_teses(self, npjur: str) -> Page:
        """
        Abre o Gerador de Teses do processo numa nova page.

        Se a pagina vier com erro "É necessário ajustar os campos VARA e FORO",
        tenta corrigir a vara automaticamente e reabrir.
        """
        url = URL_GERADOR_TESES.format(npjur=npjur)
        console.log(f"[blue]→ Abrindo Gerador de Teses:[/blue] {url}")

        nova_pagina = self.page.context.new_page()
        self._goto_logado(nova_pagina, url)
        try:
            nova_pagina.locator("#id_judoctese, text=/É necessário ajustar/").first.wait_for(
                state="attached", timeout=self.timeout
            )
        except Exception:
            pass

        tem_erro_vara = nova_pagina.evaluate(
            """() => /É necessário ajustar os campos VARA e FORO|ajustar os campos VARA/i.test(document.body.innerText)"""
        )
        if tem_erro_vara:
            console.log("[yellow]   ! Erro 'ajustar VARA e FORO' detectado — corrigindo cadastro...[/yellow]")
            nova_pagina.close()
            corrigiu = self.garantir_vara_preenchida(npjur)
            if not corrigiu:
                raise RuntimeError(
                    f"NPJUR {npjur}: Gerador de Teses pede vara/foro e nao consegui "
                    "preencher automaticamente. Preenche manualmente no NPJUR e roda de novo."
                )
            console.log("[blue]   → Reabrindo Gerador de Teses apos correcao...[/blue]")
            nova_pagina = self.page.context.new_page()
            self._goto_logado(nova_pagina, url)
            nova_pagina.locator("#id_judoctese").wait_for(state="attached", timeout=self.timeout)

        return nova_pagina

    # -------------------------------------------------------------------------
    # PASSO 5+6: selecionar tese + baixar Word
    # -------------------------------------------------------------------------
    def gerar_e_baixar_word(self, page_teses: Page, nome_peticao: str,
                            prefixo_arquivo: str = "") -> Path:
        """
        Dentro da pagina do Gerador de Teses:
          1. Seleciona a tese no <select#id_judoctese> pelo texto.
          2. Clica em "Gerar Documento DOCX" (#oGerarDoc).
          3. Na aba do relatorio, clica no link de visualizar -> download.

        prefixo_arquivo evita colisao de nome entre processos (ex: o NPJUR).
        """
        console.log(f"[blue]→ Selecionando tese:[/blue] {nome_peticao}")

        selecionada = page_teses.evaluate(
            """(nome) => {
                const sel = document.getElementById('id_judoctese');
                if (!sel) return null;
                const alvo = nome.trim().toLowerCase();
                let escolhida = null;
                for (const opt of sel.options) {
                    if (opt.text.trim().toLowerCase() === alvo) { escolhida = opt; break; }
                }
                if (!escolhida) {
                    for (const opt of sel.options) {
                        if (opt.text.trim().toLowerCase().includes(alvo)) { escolhida = opt; break; }
                    }
                }
                if (!escolhida) return null;
                sel.value = escolhida.value;
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return { value: escolhida.value, text: escolhida.text };
            }""",
            nome_peticao,
        )

        if not selecionada:
            raise CalibracaoNecessaria(
                f"Tese '{nome_peticao}' nao encontrada no dropdown do Gerador de Teses."
            )

        console.log(f"[green]   ✓ Tese:[/green] {selecionada['text']}")
        self._esperar(page_teses)

        # Clicar em "Gerar Documento DOCX" abre uma NOVA aba (relatorio.php)
        # com o link "Clique aqui para visualizar o documento" — e nesse link
        # que o download dispara, nao no botao original.
        console.log("[blue]→ Clicando em 'Gerar Documento DOCX'...[/blue]")
        with page_teses.context.expect_page(timeout=self.timeout) as nova_aba_info:
            page_teses.locator("#oGerarDoc").click()
        page_relatorio = nova_aba_info.value
        page_relatorio.wait_for_load_state("domcontentloaded", timeout=self.timeout)
        console.log("[blue]→ Aba do relatorio aberta — clicando no link do documento...[/blue]")

        try:
            with page_relatorio.expect_download(timeout=self.timeout) as download_info:
                page_relatorio.get_by_role(
                    "link", name=re.compile(r"Clique aqui para visualizar o documento", re.IGNORECASE)
                ).click()
            download: Download = download_info.value
        finally:
            try:
                page_relatorio.close()
            except Exception:
                pass

        nome = download.suggested_filename
        if prefixo_arquivo:
            nome = f"{prefixo_arquivo}_{nome}"
        caminho = self.pasta_downloads / nome
        download.save_as(caminho)
        console.log(f"[green]✓ Word baixado:[/green] {caminho}")
        return caminho

    # -------------------------------------------------------------------------
    # PASSO 9: anexar PDF em Documentos Anexos (popup NOVO DOCUMENTO)
    # -------------------------------------------------------------------------
    def anexar_documento(self, npjur: str, caminho_pdf: Path, tipo_documento: str,
                         observacao: str | None = None) -> int | None:
        """
        Retorna o `seq` (numero NN do nome {npjur}_NN) da peca anexada —
        identificador UNICO usado depois pra marcar EXATAMENTE essa peca no
        peticionamento (sem depender de descricao/data). Pode ser None se o
        NPJUR nao expuser o nome na lista.

        Abre o popup NOVO DOCUMENTO do processo (Documentos Anexos > Novo
        Documento) por URL direta e:
          1. escolhe o arquivo PDF
          2. seleciona "Descrição" = tipo_documento (= coluna C da planilha,
             ex: "DESENTRANHAMENTO DO MANDADO REQUERIDO 1")
          2b. se `observacao` vier (endereco do desentranhamento, col L),
              preenche o campo de observacao com ela
          3. clica Gravar

        Fluxo confirmado no video do Lucas de 10/06/2026: dentro do processo
        > aba Documentos Anexos > NOVO DOCUMENTO > popup judocdig/cadastro.php.
        """
        url = URL_NOVO_DOCUMENTO.format(npjur=npjur)
        console.log(f"[blue]→ Anexando documento:[/blue] {url}")

        # IDENTIDADE DETERMINISTICA DA PECA (25/06, 1493904): "PETIÇÃO" (e
        # outras descricoes) e generico demais — bate com docs antigos do
        # processo (doc 60 "PETIÇÃO", financeiros "GUIA E PETIÇÃO"...). Casar
        # por descricao fazia o robo marcar/peticionar o doc ERRADO. Em vez
        # disso, lista os numeros de sequencia (NN do nome {npjur}_NN) ANTES
        # de gravar; depois do Gravar, o NN que apareceu de novo e EXATAMENTE
        # a peca que o robo criou — imune a nome generico e suporta varias
        # pecas no mesmo dia.
        seqs_antes = self._seqs_documentos_anexos(npjur)
        console.log(f"[dim]   ({len(seqs_antes)} documentos no processo antes de anexar)[/dim]")

        pop = self.page.context.new_page()
        dialogos = CapturaDialogos(pop)
        try:
            self._goto_logado(pop, url)
            pop.locator("input[type=file]").first.wait_for(state="attached", timeout=self.timeout)
            self._esperar(pop)

            # 1. arquivo
            pop.locator("input[type=file]").first.set_input_files(str(caminho_pdf))
            console.log(f"[green]   ✓ Arquivo:[/green] {Path(caminho_pdf).name}")
            self._esperar(pop, 0.5)

            # 2. radio "Tipo Relação" = Acompanhamento (se nenhum estiver marcado)
            self._eval(
                pop,
                """() => {
                    const radios = Array.from(document.querySelectorAll('input[type=radio]'));
                    if (radios.length && !radios.some(r => r.checked)) {
                        const alvo = radios.find(r => {
                            const tr = r.closest('label, td, div, span');
                            return tr && /acompanhamento/i.test(tr.innerText || '');
                        }) || radios[0];
                        alvo.click();
                    }
                }"""
            )

            # 3. select "Descrição" (#id_jutipochave) = coluna C da planilha.
            #    ARMADILHA (flagrada ao vivo 11/06): a pagina REVERTE em ~3s
            #    qualquer selecao feita via JS — por isso o robo logava
            #    "✓ Descrição" e o Gravar reclamava de campo obrigatorio.
            #    Selecao NATIVA (clique + digitacao/type-ahead + Enter)
            #    persiste. Confere o valor DEPOIS da janela de reversao.
            existe = self._eval(
                pop,
                """(tipo) => {
                    const norm = (s) => s.toUpperCase()
                        .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                        .replace(/\\s+/g, ' ').trim();
                    const sel = document.getElementById('id_jutipochave');
                    if (!sel) return { erro: 'select id_jutipochave nao existe' };
                    const alvo = norm(tipo);
                    const opts = Array.from(sel.options).filter(o => o.value);
                    let opt = opts.find(o => norm(o.text) === alvo);
                    if (!opt) opt = opts.find(o => norm(o.text).includes(alvo) || alvo.includes(norm(o.text)));
                    return opt ? { texto: opt.text.trim(), indice: opt.index } : null;
                }""",
                tipo_documento,
            )
            if not existe or existe.get("erro"):
                raise CalibracaoNecessaria(
                    f"NOVO DOCUMENTO: nenhuma opcao do select 'Descrição' casa com "
                    f"'{tipo_documento}' ({(existe or {}).get('erro', 'sem match exato')})."
                )

            escolhido = None
            for tentativa in range(1, 4):
                pop.keyboard.press("Escape")  # fecha dropdown de tentativa anterior
                pop.locator("#id_jutipochave").click()
                if tentativa < 3:
                    pop.keyboard.type(existe["texto"], delay=25)
                else:
                    # FALLBACK (12/06, flagrado no 1534239): o type-ahead caiu
                    # em outra opcao de prefixo parecido ("INTIMAÇÃO DO
                    # EXECUTADO..." em vez de "INTIMAÇÃO P/ IND..."). Com o
                    # dropdown ABERTO, Home + N setas navega ate o indice
                    # exato — interacao nativa, persiste como a digitacao.
                    console.log("[yellow]   ! Fallback: navegando por setas ate o indice "
                                f"{existe['indice']}...[/yellow]")
                    pop.keyboard.press("Home")
                    for _ in range(int(existe["indice"])):
                        pop.keyboard.press("ArrowDown")
                pop.keyboard.press("Enter")
                self._esperar(pop, 1.5)  # espera PASSAR a janela de reversao
                escolhido = self._eval(
                    pop,
                    """() => {
                        const sel = document.getElementById('id_jutipochave');
                        if (!sel || !sel.value || sel.selectedIndex < 0) return null;
                        return sel.options[sel.selectedIndex].text.trim();
                    }"""
                )
                if escolhido and _normalizar(escolhido) == _normalizar(existe["texto"]):
                    break
                console.log(f"[yellow]   ! Selecao nao persistiu (tentativa {tentativa}: {escolhido!r}) — tentando de novo...[/yellow]")
                escolhido = None
            if not escolhido:
                raise CalibracaoNecessaria(
                    f"NOVO DOCUMENTO: a selecao de '{tipo_documento}' no select "
                    "'Descrição' nao persiste nem com interacao nativa."
                )
            console.log(f"[green]   ✓ Descrição (conferida no select):[/green] {escolhido}")

            # 3b. Observacao (endereco do desentranhamento, 12/06)
            if observacao:
                campo_obs = self._eval(pop, """(obs) => {
                    const campo = document.querySelector(
                        "textarea[id*='observ' i], textarea[name*='observ' i], " +
                        "input[type=text][id*='observ' i]");
                    if (!campo) return null;
                    campo.value = obs;
                    campo.dispatchEvent(new Event('input', { bubbles: true }));
                    campo.dispatchEvent(new Event('change', { bubbles: true }));
                    return campo.id || campo.name;
                }""", observacao)
                if not campo_obs:
                    raise CalibracaoNecessaria(
                        "NOVO DOCUMENTO: campo de Observacao nao encontrado "
                        "pra preencher o endereco."
                    )
                console.log(f"[green]   ✓ Observação ({campo_obs}):[/green] {observacao[:80]}")

            # 4. Gravar (botao no topo do popup)
            self._clicar_gravar(pop, "tela NOVO DOCUMENTO")
            try:
                pop.wait_for_load_state("domcontentloaded", timeout=self.timeout)
            except Exception:
                pass
            self._esperar(pop)

            if dialogos.mensagens:
                console.log(f"[blue]   mensagens do sistema:[/blue] {dialogos.mensagens}")
                # prefixos sem acento de proposito: os alerts do NPJUR chegam
                # com encoding quebrado ("obrigatÃ³rio") e o sufixo nao casa
                erro = [m for m in dialogos.mensagens
                        if re.search(r"erro|n[aã]o foi poss|obrigat|necess|favor|selecion", m, re.I)]
                if erro:
                    raise CalibracaoNecessaria(f"NOVO DOCUMENTO: o sistema recusou: {erro}")

            # VERIFICACAO REAL — o Gravar deste popup recarrega SEM validacao
            # nem mensagem (testado ao vivo 11/06: Gravar sem arquivo = reload
            # silencioso). Em 10/06 19:54 o robo marcou 'anexado' mas o
            # documento nunca entrou no processo.
            # VERIFICACAO DETERMINISTICA: o NN novo (que nao existia antes do
            # Gravar) e EXATAMENTE a peca que o robo acabou de criar. Sem NN
            # novo = o Gravar nao criou doc nenhum (falha silenciosa do NPJUR,
            # ou — 25/06, 1493904 — o robo achava que tinha anexado mas nao
            # tinha). Isso NAO depende da descricao, entao nunca confunde com
            # um doc antigo de mesmo nome.
            seqs_depois = self._seqs_documentos_anexos(npjur)
            novos = sorted(seqs_depois - seqs_antes)
            if not novos:
                raise CalibracaoNecessaria(
                    f"Gravar nao deu erro, mas NENHUM documento novo apareceu nos "
                    f"Documentos Anexos (antes: {len(seqs_antes)}, depois: "
                    f"{len(seqs_depois)}). O anexo NAO foi criado (falha silenciosa "
                    "do NPJUR) — NAO marco 'anexado'."
                )
            seq = novos[-1]  # o mais recente, caso o NPJUR crie mais de um
            if len(novos) > 1:
                console.log(f"[yellow]   ! Apareceram {len(novos)} docs novos {novos} "
                            f"— uso o mais recente (seq {seq}).[/yellow]")
            console.log(f"[green]✓ Documento anexado e CONFERIDO (peca nova seq {seq}).[/green]")
            # so fecha em SUCESSO — em erro o main fotografa o popup antes
            pop.close()
            # devolve o seq (NN) da peca recem-anexada: identificador UNICO
            # usado pra marcar EXATAMENTE essa peca no peticionamento.
            return seq
        finally:
            dialogos.desligar()

    def _clicar_gravar(self, page: Page, contexto: str):
        """Acha e clica o botao Gravar/Salvar de uma tela do NPJUR."""
        for seletor in ("#gravar",
                        "input[type=button][value*='Gravar' i]",
                        "input[type=submit][value*='Gravar' i]",
                        "button:has-text('Gravar')",
                        "input[type=button][value*='Salvar' i]",
                        "button:has-text('Salvar')"):
            loc = page.locator(seletor)
            if loc.count() > 0 and loc.first.is_visible():
                console.log(f"[blue]   → Clicando Gravar ({seletor})...[/blue]")
                loc.first.click()
                return
        raise CalibracaoNecessaria(f"Botao Gravar nao encontrado na {contexto}.")

    # -------------------------------------------------------------------------
    # PASSO 10: Acompanhamento Processual (com pre-requisitos automaticos)
    # -------------------------------------------------------------------------
    def lancar_acompanhamento(self, npjur: str, id_juprocesso: str, tipo: str,
                              observacao: str | None = None,
                              _profundidade: int = 0):
        """
        Abre o Cadastro de Acompanhamento Processual (popup juacompanha,
        que usa o id INTERNO do processo) e lanca o acompanhamento `tipo`.

        Fluxo do video do Lucas (10/06/2026): selecionar o tipo, ESPERAR
        a tela carregar, preencher "Data Real" = mesmo valor de "Data",
        e Gravar.

        Se o NPJUR reclamar de pre-requisitos ("falta lancar os seguintes
        acompanhamentos: ..."), parseia a lista, lanca cada um EM ORDEM e
        tenta de novo (no maximo 5 niveis, pra nunca entrar em loop).
        """
        if _profundidade > 5:
            raise RuntimeError(
                f"Pre-requisitos de acompanhamento em loop (>{_profundidade} niveis) — abortando."
            )
        if not id_juprocesso:
            raise CalibracaoNecessaria(
                "id_juprocesso nao foi extraido da linha da fila — nao da pra "
                "abrir o Cadastro de Acompanhamento."
            )

        console.log(f"[blue]→ Lancando acompanhamento:[/blue] {tipo}")

        # Idempotencia: se ja consta na lista (ex: o Gravar da execucao
        # anterior salvou mas a conferencia falhou), NAO relanca.
        ja_existe = self._acompanhamento_na_lista(npjur, tipo, dump_se_nao_achar=False)
        if ja_existe:
            console.log(f"[green]✓ Ja estava na lista — nao relanca:[/green] {ja_existe[:90]}")
            return

        pg = self.page.context.new_page()
        dialogos = CapturaDialogos(pg)
        try:
            # 1. Abre a tela base so pra LER o value da opcao do tipo no
            #    select #id_jutipoacomp (663 opcoes; ids confirmados ao vivo)
            self._goto_logado(pg, URL_ACOMPANHAMENTO.format(id_juprocesso=id_juprocesso))
            pg.locator("#id_jutipoacomp").wait_for(state="attached", timeout=self.timeout)
            self._esperar(pg)

            opcao = self._eval(
                pg,
                """(tipo) => {
                    const norm = (s) => s.toUpperCase()
                        .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                        .replace(/\\s+/g, ' ').trim();
                    const alvo = norm(tipo);
                    const sel = document.getElementById('id_jutipoacomp');
                    if (!sel) return { erro: 'select id_jutipoacomp nao existe' };
                    let escolhida = null;
                    for (const opt of sel.options) {
                        if (opt.value && norm(opt.text) === alvo) { escolhida = opt; break; }
                    }
                    if (!escolhida) {
                        for (const opt of sel.options) {
                            const t = norm(opt.text);
                            if (opt.value && t && (t.includes(alvo) || alvo.includes(t))) {
                                escolhida = opt; break;
                            }
                        }
                    }
                    if (!escolhida) return null;
                    return { value: escolhida.value, texto: escolhida.text };
                }""",
                tipo,
            )
            if not opcao or opcao.get("erro"):
                raise CalibracaoNecessaria(
                    f"Tipo '{tipo}' nao existe no select #id_jutipoacomp "
                    f"({(opcao or {}).get('erro', 'nenhuma opcao casou')})."
                )
            console.log(f"[green]   ✓ Tipo:[/green] {opcao['texto']} (value={opcao['value']})")

            # 2. Navega DIRETO pra URL com o tipo ja aplicado. Selecionar no
            #    <select> dispara reload da pagina inteira (teste ao vivo
            #    10/06) — navegar direto elimina essa fonte de erro.
            self._goto_logado(
                pg,
                URL_ACOMPANHAMENTO_COM_TIPO.format(
                    id_juprocesso=id_juprocesso, id_tipo=opcao["value"]
                ),
            )
            pg.locator("#dDtReal").wait_for(state="attached", timeout=self.timeout)
            self._esperar(pg)

            # 3. Data Real = valor de Data (regra do Lucas; Data e Hora ja
            #    vem preenchidos pelo sistema) + Marcacao do processo quando
            #    vier em "Selecione..." (processo antigo ja vem marcado; em
            #    processo novo o NPJUR recusa o Gravar com alert — 11/06 07:08)
            data_real = self._eval(
                pg,
                """(args) => {
                    const [marcacao, observacao] = args;
                    const norm = (s) => s.toUpperCase()
                        .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                        .replace(/\\s+/g, ' ').trim();
                    const sel = document.getElementById('id_jutipoacomp');
                    const real = document.getElementById('dDtReal');
                    const data = document.getElementById('dDtAcomp');
                    if (!sel || !sel.value) return { ok: false, motivo: 'tipo nao veio selecionado pela URL' };
                    if (!real || !data) return { ok: false, motivo: 'campos dDtReal/dDtAcomp nao encontrados' };
                    if (!data.value) return { ok: false, motivo: 'dDtAcomp veio vazio' };
                    // ao carregar com o tipo na URL a pagina se re-navega com
                    // uma query propria MAL-MONTADA (iCtrl=1id_juincidentes=)
                    // que perde id_juarea e flag_resp — restaura antes do Gravar
                    // (dump 10/06 23:01; suspeita da falha silenciosa do gravar)
                    const area = document.getElementById('id_juarea');
                    if (area && !area.value) area.value = '1';
                    const fresp = document.getElementById('flag_resp');
                    if (fresp && !fresp.value) fresp.value = '1';
                    let marcou = null;
                    const marc = document.getElementById('id_jumarcacao_processual');
                    if (marc && !marc.value) {
                        for (const opt of marc.options) {
                            if (opt.value && norm(opt.text) === norm(marcacao)) {
                                marc.value = opt.value;
                                marc.dispatchEvent(new Event('change', { bubbles: true }));
                                marcou = opt.text.trim();
                                break;
                            }
                        }
                        if (!marcou) return { ok: false, motivo: 'marcacao "' + marcacao + '" nao existe no select id_jumarcacao_processual' };
                    }
                    if (!real.value) {
                        real.value = data.value;
                        real.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    let obs_ok = null;
                    if (observacao) {
                        const campo = document.getElementById('sObservacoes');
                        if (!campo) return { ok: false, motivo: 'textarea sObservacoes nao encontrada pra observacao' };
                        campo.value = observacao;
                        campo.dispatchEvent(new Event('input', { bubbles: true }));
                        campo.dispatchEvent(new Event('change', { bubbles: true }));
                        obs_ok = true;
                    }
                    return { ok: true, valor: real.value, marcou, obs_ok };
                }""",
                [self.marcacao_processo, observacao],
            )
            if not data_real.get("ok"):
                raise CalibracaoNecessaria(
                    f"Acompanhamento: {data_real.get('motivo')}."
                )
            console.log(f"[green]   ✓ Data Real:[/green] {data_real['valor']}")
            if data_real.get("marcou"):
                console.log(f"[green]   ✓ Marcação do processo:[/green] {data_real['marcou']}")
            if data_real.get("obs_ok"):
                console.log(f"[green]   ✓ Observação:[/green] {observacao[:80]}")

            # 4. Gravar
            pg.locator("#gravar").click()
            try:
                pg.wait_for_load_state("domcontentloaded", timeout=self.timeout)
            except Exception:
                pass
            self._esperar(pg, 2.0)

            # Pre-requisitos faltando? (pode vir em dialog JS ou no corpo da pagina)
            texto_erro = ""
            for msg in dialogos.mensagens:
                if re.search(r"falta lan[cç]ar", msg, re.I):
                    texto_erro = msg
                    break
            if not texto_erro:
                corpo = self._eval(pg, "() => document.body ? document.body.innerText : ''")
                m = re.search(r"N[aã]o foi poss[ií]vel lan[cç]ar o Acompanhamento.*", corpo, re.I | re.S)
                if m:
                    texto_erro = m.group(0)[:1000]

            if texto_erro:
                faltantes = self._parsear_prerequisitos(texto_erro)
                if not faltantes:
                    raise CalibracaoNecessaria(
                        f"NPJUR reclamou de pre-requisitos mas nao consegui parsear a lista: {texto_erro[:300]}"
                    )
                console.log(f"[yellow]   ! Pre-requisitos faltando: {faltantes}[/yellow]")
                pg.close()
                dialogos.desligar()
                for falta in faltantes:
                    self.lancar_acompanhamento(npjur, id_juprocesso, falta,
                                               _profundidade=_profundidade + 1)
                # agora tenta o original de novo (com a observacao original)
                return self.lancar_acompanhamento(npjur, id_juprocesso, tipo,
                                                  observacao=observacao,
                                                  _profundidade=_profundidade + 1)

            # Alert de campo obrigatorio = Gravar RECUSADO (em 11/06 07:08 o
            # alert "necessario preencher o campo Marcacao do processo" era
            # engolido e o robo seguia pra conferencia com erro enganoso)
            recusas = [m for m in dialogos.mensagens
                       if re.search(r"necess|obrigat", m, re.I)]
            if recusas:
                raise CalibracaoNecessaria(f"Gravar recusado pelo NPJUR: {recusas}")

            # 5. VERIFICACAO REAL — em teste ao vivo o Gravar falhou SEM
            #    mostrar nenhum erro. Nao da pra confiar em "sem dialog =
            #    salvou": confere na lista de acompanhamentos do processo.
            linha = self._acompanhamento_na_lista(npjur, tipo)
            if not linha:
                raise CalibracaoNecessaria(
                    f"Gravar nao deu erro, mas '{tipo}' NAO apareceu na lista de "
                    "acompanhamentos do processo (falha silenciosa do NPJUR)."
                )
            console.log(f"[green]✓ Acompanhamento lancado e CONFERIDO na lista:[/green] {linha[:90]}")
            # so fecha em SUCESSO — em erro o main fotografa o popup antes
            pg.close()
        finally:
            dialogos.desligar()

    def _acompanhamento_na_lista(self, npjur: str, tipo: str,
                                 dump_se_nao_achar: bool = True) -> str | None:
        """Abre o processo, vai na aba Acompanhamento Processual e procura
        uma linha com o tipo. Retorna o texto da linha ou None.

        A lista carrega via AJAX/layer e pode vir em iframe — varre TODOS
        os frames com retentativas (a versao antiga so olhava o frame
        principal uma vez, e em 10/06 23:02 reprovou um gravar que pode
        ter dado certo). Se nao achar, salva um raio-X DESTA tela antes de
        fechar, senao a evidencia se perde."""
        hoje = time.strftime("%d/%m/%Y")
        console.log("[blue]   → Conferindo na lista de acompanhamentos...[/blue]")
        # Exige a DATA DE HOJE na linha: o acompanhamento que o robo lanca tem
        # Data = hoje. Sem isso, um agendamento/providencia com o mesmo termo
        # (ex: "DILAÇÃO DE PRAZO" no 1353564, 15/06) dava falso-positivo e a
        # pre-checagem idempotente PULAVA o lancamento, marcando a etapa sem
        # ter lancado nada.
        js = """(args) => {
            const [tipo, hoje] = args;
            const norm = (s) => s.toUpperCase()
                .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                .replace(/\\s+/g, ' ').trim();
            const alvo = norm(tipo);
            for (const tr of document.querySelectorAll('tr')) {
                const t = tr.innerText.replace(/\\s+/g, ' ').trim();
                if (t.length > 5 && t.length < 400 && norm(t).includes(alvo)
                    && t.includes(hoje)) {
                    return t;
                }
            }
            return null;
        }"""
        pg = self.page.context.new_page()
        try:
            self._goto_logado(pg, URL_CADASTRO_PROCESSO.format(npjur=npjur))
            self._esperar(pg)
            # menu superior "Acompanhamento Processual" (chama jsAbreLayer(2))
            aba = pg.get_by_text(re.compile(r"Acompanhamento\s+Processual", re.I)).first
            aba.click(timeout=self.timeout)

            for _ in range(4):
                self._esperar(pg, 1.5)
                for frame in pg.frames:
                    try:
                        achou = frame.evaluate(js, [tipo, hoje])
                    except Exception:
                        achou = None
                    if achou:
                        return achou

            if dump_se_nao_achar and self.pasta_logs:
                d = dump_pagina(pg, self.pasta_logs, f"{npjur}_lista_acomp")
                console.log(f"[yellow]   ! Tipo nao consta — raio-X da LISTA salvo: {d}[/yellow]")
            return None
        finally:
            try:
                pg.close()
            except Exception:
                pass

    def _seqs_documentos_anexos(self, npjur: str) -> set[int]:
        """Retorna o conjunto de numeros de sequencia (NN do nome
        {npjur}_NN.ext) de TODOS os documentos anexos do processo, sem filtro
        de descricao nem de data. Usado pra identificar, por diferenca
        (antes/depois do Gravar), EXATAMENTE a peca que o robo acabou de criar
        — determinístico e imune a descricao generica (25/06, 1493904).

        Le os NN do innerText da pagina inteira (a lista de Documentos Anexos
        mostra os nomes {npjur}_NN). Tolerante: se a aba nao carregar, retorna
        o que conseguir — o diff continua valido (so precisa do conjunto
        ANTES e DEPOIS serem lidos do mesmo jeito)."""
        js = r"""(npjur) => {
            const txt = document.body ? document.body.innerText : '';
            const re = new RegExp(npjur + "_(\\d+)\\.[a-z0-9]+", "gi");
            const seqs = new Set();
            let m;
            while ((m = re.exec(txt)) !== null) seqs.add(parseInt(m[1], 10));
            return Array.from(seqs);
        }"""
        pg = self.page.context.new_page()
        try:
            self._goto_logado(pg, URL_CADASTRO_PROCESSO.format(npjur=npjur))
            self._esperar(pg)
            try:
                aba = pg.get_by_text(re.compile(r"Documentos\s+Anexos", re.I)).first
                aba.click(timeout=self.timeout)
            except Exception:
                pass
            seqs: set[int] = set()
            for _ in range(4):
                self._esperar(pg, 1.5)
                for frame in pg.frames:
                    try:
                        achou = frame.evaluate(js, str(npjur))
                    except Exception:
                        achou = None
                    if achou:
                        seqs.update(int(s) for s in achou)
                if seqs:
                    break
            return seqs
        finally:
            try:
                pg.close()
            except Exception:
                pass

    def _documento_na_lista(self, npjur: str, descricao: str,
                            dump_se_nao_achar: bool = True) -> dict | None:
        """Abre o processo, clica no menu 'Documentos Anexos' e procura uma
        linha com a descricao (o NPJUR renomeia o arquivo pra {npjur}_NN.pdf,
        entao o nome original nao serve pra conferir). A data de HOJE na
        linha e PREFERENCIAL, nao obrigatoria — e o NPJUR TRUNCA descricoes
        longas na lista ("...REQU...", flagrado 12/06 no 1534239), entao o
        casamento tolera prefixo truncado e olha tambem o title dos links.
        Retorna {"linha": texto, "seq": NN} ou None (com raio-X da lista
        salvo). O `seq` (numero NN do nome {npjur}_NN) e o identificador UNICO
        da peca anexada — usado depois pra marcar EXATAMENTE essa peca no
        peticionamento, sem depender de descricao/data (25/06)."""
        hoje = time.strftime("%d/%m/%Y")
        console.log("[blue]   → Conferindo na lista de Documentos Anexos...[/blue]")
        js = """(args) => {
            const [descricao, hoje] = args;
            const norm = (s) => s.toUpperCase()
                .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                .replace(/\\s+/g, ' ').trim();
            const alvo = norm(descricao);
            const casa = (txt) => {
                if (!txt) return false;
                const s = norm(txt).replace(/(\\.{3}|\\u2026)$/, '').trim();
                if (s.length < 10) return false;
                return s.includes(alvo) || alvo.startsWith(s);
            };
            const pegaSeq = (linha) => {
                const m = linha.match(/_(\\d+)\\.[a-z0-9]+/i);
                return m ? parseInt(m[1], 10) : null;
            };
            // ESCOPO: as linhas de Documentos Anexos tem um link com
            // onclick jsAlteraDescricao(...) cujo texto/title eh a descricao
            // completa. Buscar SO nelas evita o falso-positivo de pegar
            // 'acompanhamento'/'agendamento' com o mesmo termo (15/06,
            // dilacao no 1353564 — "DILAÇÃO DE PRAZO" aparece em varias
            // secoes). Exige tambem a DATA DE HOJE na linha. Entre as que
            // casam, fica com a de MAIOR seq (a recem-criada).
            const els = document.querySelectorAll("[onclick*='jsAlteraDescricao']");
            let melhor = null;
            for (const el of els) {
                const tr = el.closest('tr');
                const linha = tr ? tr.innerText.replace(/\\s+/g, ' ').trim() : '';
                const desc = (el.getAttribute && el.getAttribute('title')) || el.innerText || '';
                if (!(casa(desc) || casa(linha))) continue;
                if (!(linha && linha.includes(hoje))) continue;
                const seq = pegaSeq(linha);
                if (!melhor || (seq || 0) > (melhor.seq || 0)) {
                    melhor = { linha, seq };
                }
            }
            return melhor;
        }"""
        pg = self.page.context.new_page()
        try:
            self._goto_logado(pg, URL_CADASTRO_PROCESSO.format(npjur=npjur))
            self._esperar(pg)
            aba = pg.get_by_text(re.compile(r"Documentos\s+Anexos", re.I)).first
            aba.click(timeout=self.timeout)

            for _ in range(4):
                self._esperar(pg, 1.5)
                for frame in pg.frames:
                    try:
                        achou = frame.evaluate(js, [descricao, hoje])
                    except Exception:
                        achou = None
                    if achou:
                        return achou

            if dump_se_nao_achar and self.pasta_logs:
                d = dump_pagina(pg, self.pasta_logs, f"{npjur}_lista_docs")
                console.log(f"[yellow]   ! Documento nao consta — raio-X da LISTA salvo: {d}[/yellow]")
            return None
        finally:
            try:
                pg.close()
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # PASSO 11: peticionamento eletronico (fluxo interno NPJUR/2ADV)
    # -------------------------------------------------------------------------
    def preparar_peticionamento(self, npjur: str, id_agendamento: str,
                                tipo_peticao: str, descricao_doc: str,
                                data_doc: str | None = None,
                                seq_doc: int | None = None) -> Page:
        """
        Fluxo REAL mapeado ao vivo em 11/06/2026 — NAO existe tela do Loy;
        o protocolo e todo interno do NPJUR (backend 2ADV):
          1. jupeticionamento_eletronico/cadastro.php?npjur=X
          2. garante o CNJ sanitizado (#id_juenvio_processo_2adv com opcao)
          3. seleciona processo + tipo de peticao (#id_jutipoacomp_2adv =
             coluna E da planilha) + radio do agendamento
          4. 'Enviar para peticionamento' (jsPeticionamentoEletronico gera
             codigo via POST e abre popup documentos_peticionamento.php;
             cada clique gera codigo NOVO, sem residuo — testado ao vivo)
          5. marca o checkbox do PDF anexado (linha com a descricao e
             'Enviado 2ADV?' = NAO)

        NAO envia: o main pausa pra conferencia e chama
        enviar_peticionamento() — esse sim e IRREVERSIVEL.
        """
        console.log(f"[blue]→ Preparando peticionamento:[/blue] {tipo_peticao}")
        pet = self.page.context.new_page()
        dialogos = CapturaDialogos(pet)
        try:
            self._goto_logado(pet, URL_PETICIONAMENTO.format(npjur=npjur))
            pet.locator("#id_juenvio_processo_2adv").wait_for(state="attached", timeout=self.timeout)
            self._esperar(pet)

            # 2. sanitizacao (uma vez por processo; pode demorar pra processar)
            for _ in range(3):
                sanitizado = self._eval(pet, """() => {
                    const sel = document.getElementById('id_juenvio_processo_2adv');
                    return !!Array.from(sel.options).find(o => o.value);
                }""")
                if sanitizado:
                    break
                console.log("[yellow]   ! CNJ ainda nao sanitizado — enviando pra sanitizacao...[/yellow]")
                pet.get_by_text(re.compile(r"^\s*Enviar para sanitiza", re.I)).first.click()
                self._esperar(pet, 5.0)
                pet.reload(timeout=self.timeout, wait_until="domcontentloaded")
                self._esperar(pet)
            else:
                raise CalibracaoNecessaria(
                    "CNJ nao aparece como sanitizado mesmo apos enviar pra "
                    "sanitizacao (processamento pode demorar — roda de novo mais tarde)."
                )

            # 3. processo + tipo + agendamento
            r = self._eval(pet, """(args) => {
                const [tipo, idAgenda] = args;
                const norm = (s) => s.toUpperCase()
                    .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                    .replace(/\\s+/g, ' ').trim();
                const selP = document.getElementById('id_juenvio_processo_2adv');
                // O CNJ a peticionar e o que consta NA PETICAO = campo num_cnj
                // (CNJ principal do processo). Processos com varios CNJs
                // vinculados (15/06, NPJUR 1353564) listavam a 1a opcao como
                // CNJ INVALIDO; pegar a 1a fazia o NPJUR recusar o envio
                // ("Numero do CNJ invalido"). Casa pelo CNJ alvo.
                const soDig = (s) => (s || '').replace(/\\D/g, '');
                const inpCnj = document.getElementById('num_cnj');
                const cnjAlvo = soDig(inpCnj ? inpCnj.value : '');
                const opcoes = Array.from(selP.options).filter(o => o.value);
                let optP = cnjAlvo ? opcoes.find(o => soDig(o.text).startsWith(cnjAlvo)) : null;
                if (!optP) return { erro: "no envio do peticionamento nao achei o CNJ da "
                    + "peticao (" + (inpCnj ? inpCnj.value : '?') + "). Opcoes: "
                    + opcoes.map(o => o.text.split(' - ')[0]).join(' | ') };
                if (/inv[a\\u00e1]lid/i.test(optP.text)) return { erro: "o CNJ da peticao ("
                    + (inpCnj ? inpCnj.value : '?') + ") esta marcado como INVALIDO no NPJUR "
                    + "— reenviar pra saneamento manualmente e rodar de novo." };
                selP.value = optP.value;
                selP.dispatchEvent(new Event('change', { bubbles: true }));
                const selT = document.getElementById('id_jutipoacomp_2adv');
                if (!selT) return { erro: 'select id_jutipoacomp_2adv nao existe' };
                let optT = Array.from(selT.options).find(o => o.value && norm(o.text) === norm(tipo));
                if (!optT) optT = Array.from(selT.options).find(o => o.value && norm(o.text).includes(norm(tipo)));
                if (!optT) return { erro: "tipo '" + tipo + "' nao existe no select do peticionamento" };
                selT.value = optT.value;
                selT.dispatchEvent(new Event('change', { bubbles: true }));
                const radios = Array.from(document.querySelectorAll("input[id^='id_juagenda_vincular']"));
                let radio = radios.find(x => String(x.value) === String(idAgenda))
                    || (radios.length === 1 ? radios[0] : null);
                if (radio) { radio.checked = true; radio.dispatchEvent(new Event('change', { bubbles: true })); }
                return { ok: true, processo: optP.text.trim().slice(0, 60), tipo: optT.text.trim(),
                         agendamento: radio ? radio.value : null, radios: radios.length };
            }""", [tipo_peticao, str(id_agendamento)])
            if not r.get("ok"):
                raise CalibracaoNecessaria(f"Peticionamento: {r.get('erro')}.")
            console.log(f"[green]   ✓ Processo:[/green] {r['processo']}")
            console.log(f"[green]   ✓ Tipo:[/green] {r['tipo']}  (agendamento {r['agendamento']} de {r['radios']})")

            # 4. Enviar para peticionamento -> popup com a lista de documentos
            console.log("[blue]   → 'Enviar para peticionamento' (gera o codigo e abre a lista de documentos)...[/blue]")
            try:
                with pet.context.expect_page(timeout=self.timeout) as pop_info:
                    pet.get_by_text(re.compile(r"^\s*Enviar para peticionamento", re.I)).first.click()
                docs = pop_info.value
            except PWTimeout:
                raise CalibracaoNecessaria(
                    "Cliquei em 'Enviar para peticionamento' e o popup da lista de "
                    f"documentos nao abriu. Mensagens do sistema: {dialogos.mensagens or 'nenhuma'}"
                )
            docs.wait_for_load_state("domcontentloaded", timeout=self.timeout)
            self._esperar(docs)
            if "documentos_peticionamento" not in (docs.url or ""):
                if self.pasta_logs:
                    dump_pagina(docs, self.pasta_logs, f"{npjur}_peticionamento")
                raise CalibracaoNecessaria(
                    f"Apos 'Enviar para peticionamento' abriu tela inesperada: {docs.url[:90]}"
                )

            # 5. marca o checkbox do documento anexado (o da DATA do anexo).
            #    CRITICO (15/06, 1353564): havia varios documentos com a mesma
            #    descricao "DILAÇÃO DE PRAZO" de datas diferentes; o robo marcou
            #    um ANTIGO e protocolou o documento errado no tribunal. Por isso
            #    EXIGE a Data de Criacao = a data em que o doc foi anexado e, no
            #    desempate, pega o de maior sequencia (NPJUR_NN.ext = ultimo).
            #    `data_doc` vem do main (timestamp da etapa 'anexado' no estado):
            #    multi-dia (anexa num dia, peticiona noutro) o doc tem a data do
            #    ANEXO, nao a de hoje (19/06, 1351493). Sem data_doc = hoje.
            hoje = data_doc or time.strftime("%d/%m/%Y")
            # IDENTIDADE DA PECA (25/06): se temos o `seq` (NN) capturado no
            # anexo, marca EXATAMENTE essa peca pelo numero do arquivo
            # {npjur}_NN — determinístico, e a mesma peca que foi anexada. So
            # quando nao ha seq (entradas legadas) cai no match por
            # descricao+data de antes.
            marcado = self._eval(docs, """(args) => {
                const [descricao, hoje, seqAlvo] = args;
                const norm = (s) => s.toUpperCase()
                    .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                    .replace(/\\s+/g, ' ').trim();
                const alvo = norm(descricao);
                // NPJUR trunca descricoes longas com '...' (12/06, 1534239):
                // casa tambem se um prefixo do alvo (>=15 chars) termina no '...'
                const casa = (t) => {
                    if (t.includes(alvo)) return true;
                    let pos = t.indexOf('...');
                    while (pos !== -1) {
                        const antes = t.slice(Math.max(0, pos - alvo.length), pos);
                        for (let k = Math.min(alvo.length - 1, antes.length); k >= 15; k--) {
                            if (antes.slice(-k) === alvo.slice(0, k)) return true;
                        }
                        pos = t.indexOf('...', pos + 3);
                    }
                    return false;
                };
                const pegaSeq = (b) => {
                    const m = b.match(/_(\\d+)\\.[a-z0-9]+/i);
                    return m ? parseInt(m[1], 10) : 0;
                };
                // --- caminho determinístico: por seq exato (a mesma peca) ---
                if (seqAlvo) {
                    for (const cb of document.querySelectorAll("input[name='docs_anexo[]']")) {
                        const tr = cb.closest('tr');
                        if (!tr) continue;
                        const bruto = tr.innerText.replace(/\\s+/g, ' ').trim();
                        if (pegaSeq(bruto) !== seqAlvo) continue;
                        const enviado = /\\bSIM\\b/.test(norm(bruto));
                        cb.checked = true;
                        cb.dispatchEvent(new Event('change', { bubbles: true }));
                        return { txt: bruto.slice(0, 120), seq: seqAlvo,
                                 enviado, total: 1, por: 'seq' };
                    }
                    return { erro: 'seq_nao_encontrado' };
                }
                // --- legado: por descricao + data de hoje ---
                const candidatas = [];
                for (const cb of document.querySelectorAll("input[name='docs_anexo[]']")) {
                    const tr = cb.closest('tr');
                    if (!tr) continue;
                    const bruto = tr.innerText.replace(/\\s+/g, ' ').trim();
                    const t = norm(bruto);
                    if (!casa(t)) continue;
                    // SO documentos criados HOJE (Data Criacao na linha)
                    if (!bruto.includes(hoje)) continue;
                    candidatas.push({ cb, txt: bruto.slice(0, 120), seq: pegaSeq(bruto),
                                      enviado: /\\bSIM\\b/.test(t) });
                }
                if (!candidatas.length) return { erro: 'sem_doc_hoje' };
                // prefere o ainda nao enviado; desempate pelo mais recente
                candidatas.sort((a, b) => (a.enviado - b.enviado) || (b.seq - a.seq));
                const esc = candidatas[0];
                esc.cb.checked = true;
                esc.cb.dispatchEvent(new Event('change', { bubbles: true }));
                return { txt: esc.txt, seq: esc.seq, enviado: esc.enviado,
                         total: candidatas.length, por: 'descricao+data' };
            }""", [descricao_doc, hoje, seq_doc])
            if not marcado or marcado.get("erro"):
                if self.pasta_logs:
                    dump_pagina(docs, self.pasta_logs, f"{npjur}_docs_peticionamento")
                if marcado and marcado.get("erro") == "seq_nao_encontrado":
                    raise CalibracaoNecessaria(
                        f"A peca anexada (arquivo {npjur}_{seq_doc}) NAO esta na lista "
                        "do peticionamento — confere se a etapa 'anexado' criou o doc "
                        "nesse processo. (Match por identidade da peca, nao por data.)"
                    )
                raise CalibracaoNecessaria(
                    f"Nenhum documento '{descricao_doc}' com data {hoje} (data do anexo) "
                    "na lista do peticionamento — confere se a etapa 'anexado' criou o doc. "
                    "(NAO protocolo documento de outra data, pra evitar enviar a peca errada.)"
                )
            if marcado.get("enviado"):
                # a peca exata ja consta 'Enviado 2ADV? SIM' — ja foi enviada
                # antes (provavel rodada anterior). NAO reenvia: evita duplicar.
                raise CalibracaoNecessaria(
                    f"A peca {npjur}_{marcado.get('seq')} ja esta como 'Enviado 2ADV? SIM' "
                    "no peticionamento — ja foi enviada antes. NAO reenvio pra nao "
                    "duplicar; confere no Loy se o protocolo foi concluido."
                )
            if marcado.get("total", 0) > 1:
                console.log(f"[yellow]   ! {marcado['total']} docs de hoje com essa descricao "
                            f"— marquei o mais recente (seq {marcado['seq']}).[/yellow]")
            console.log(f"[green]   ✓ Documento marcado (hoje, seq {marcado.get('seq')}):[/green] "
                        f"{marcado['txt'][:90]}")
            pet.close()
            return docs
        finally:
            dialogos.desligar()

    def enviar_peticionamento(self, docs: Page, descricao_doc: str,
                              data_doc: str | None = None,
                              seq_doc: int | None = None):
        """Clica 'Enviar documentos selecionados' — IRREVERSIVEL (o backend
        2ADV protocola sozinho). Depois confere que a linha do documento
        virou 'Enviado 2ADV? SIM'. Quando `seq_doc` (NN da peca) vem, confere
        pela peca EXATA; senao por descricao+data. `data_doc` = data do anexo
        (multi-dia); sem ela, usa hoje."""
        dialogos = CapturaDialogos(docs)
        try:
            console.log("[bold blue]→ PROTOCOLANDO (Enviar documentos selecionados)...[/bold blue]")
            docs.get_by_text(re.compile(r"Enviar documentos selecionados", re.I)).first.click()
            try:
                docs.wait_for_load_state("domcontentloaded", timeout=self.timeout)
            except Exception:
                pass

            hoje = data_doc or time.strftime("%d/%m/%Y")
            js = """(args) => {
                const [descricao, hoje, seqAlvo] = args;
                const norm = (s) => s.toUpperCase()
                    .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                    .replace(/\\s+/g, ' ').trim();
                const alvo = norm(descricao);
                const pegaSeq = (b) => {
                    const m = b.match(/_(\\d+)\\.[a-z0-9]+/i);
                    return m ? parseInt(m[1], 10) : 0;
                };
                // confirmacao por peca EXATA (seq) quando disponivel
                if (seqAlvo) {
                    for (const tr of document.querySelectorAll('tr')) {
                        const bruto = tr.innerText.replace(/\\s+/g, ' ').trim();
                        if (pegaSeq(bruto) !== seqAlvo) continue;
                        if (/\\bSIM\\b/.test(norm(bruto))) return bruto.slice(0, 120);
                        return null;
                    }
                    return null;
                }
                // mesma tolerancia a descricao truncada do preparar (12/06)
                const casa = (t) => {
                    if (t.includes(alvo)) return true;
                    let pos = t.indexOf('...');
                    while (pos !== -1) {
                        const antes = t.slice(Math.max(0, pos - alvo.length), pos);
                        for (let k = Math.min(alvo.length - 1, antes.length); k >= 15; k--) {
                            if (antes.slice(-k) === alvo.slice(0, k)) return true;
                        }
                        pos = t.indexOf('...', pos + 3);
                    }
                    return false;
                };
                for (const tr of document.querySelectorAll('tr')) {
                    const bruto = tr.innerText.replace(/\\s+/g, ' ').trim();
                    const t = norm(bruto);
                    // confere a linha do doc de HOJE que virou Enviado SIM
                    if (casa(t) && bruto.includes(hoje) && /\\bSIM\\b/.test(t)) {
                        return bruto.slice(0, 120);
                    }
                }
                return null;
            }"""
            linha = None
            for _ in range(5):
                self._esperar(docs, 1.5)
                try:
                    linha = self._eval(docs, js, [descricao_doc, hoje, seq_doc])
                except Exception:
                    linha = None
                if linha:
                    break

            if dialogos.mensagens:
                console.log(f"[blue]   mensagens do sistema:[/blue] {dialogos.mensagens}")
            if not linha:
                if self.pasta_logs:
                    dump_pagina(docs, self.pasta_logs, "envio_peticionamento")
                raise CalibracaoNecessaria(
                    "Cliquei em 'Enviar documentos selecionados' mas a linha do "
                    "documento nao virou 'Enviado 2ADV? SIM'. CONFERE MANUALMENTE "
                    "no NPJUR antes de rodar de novo — risco de protocolo duplicado."
                )
            console.log(f"[green]✓ Documento enviado pro 2ADV:[/green] {linha[:90]}")
        finally:
            dialogos.desligar()

    # -------------------------------------------------------------------------
    # PASSO 11b: protocolo REAL no Loy (mapeado ao vivo + video do Lucas 11/06)
    # -------------------------------------------------------------------------
    # O envio 2ADV NAO protocola nada: depois dele a pagina ganha o botao
    # "Abrir Peticionamento" (link assinado pro app.loylegal.com). E LA que
    # a peticao e protocolada de verdade. Se pular essa parte, o NPJUR deixa
    # concluir a fila mesmo sem peticao no tribunal (aconteceu em 11/06 com
    # o 1570755 — o robo "concluiu" sem protocolar).
    #
    # O Loy e um app Ember: os selects sao "power-select" (.ember-power-
    # select-trigger abre um dropdown com li[role=option]); setar value via
    # JS nao funciona — tem que clicar no trigger e na opcao.

    def abrir_loy(self, docs: Page) -> Page:
        """Na tela de Envio de documentos (pos-envio 2ADV), abre o link
        'Abrir Peticionamento' numa page nova e espera o Loy renderizar."""
        console.log("[blue]→ Abrindo o Loy ('Abrir Peticionamento')...[/blue]")
        link = docs.get_by_text(re.compile(r"Abrir Peticionamento", re.I)).first
        link.wait_for(state="visible", timeout=self.timeout)
        href = link.get_attribute("href")
        if not href or "loylegal" not in href:
            if self.pasta_logs:
                dump_pagina(docs, self.pasta_logs, "abrir_peticionamento")
            raise CalibracaoNecessaria(
                "Botao 'Abrir Peticionamento' sem link pro Loy (href inesperado)."
            )
        loy = docs.context.new_page()
        loy.goto(href, timeout=self.timeout, wait_until="domcontentloaded")
        self._esperar_loy_carregar(loy, href)
        self._esperar(loy, 1.5)
        console.log(f"[green]   ✓ Loy aberto:[/green] {loy.url.split('?')[0]}")
        return loy

    def _esperar_loy_carregar(self, loy: Page, href: str):
        """Espera o Loy (Ember) terminar de carregar o processo e renderizar os
        power-selects. O Loy as vezes fica preso em 'Processo Carregando...' por
        mais de 30s (22/06: 1586943/1501837 estouraram o timeout). Estrategia:
        espera os triggers com timeout generoso; se nao aparecerem e a tela
        ainda mostrar 'Carregando', recarrega o Loy 1x e espera de novo."""
        # ate 90s por tentativa (Loy lento), recarrega 1x se travar no Carregando
        for tentativa in (1, 2):
            try:
                loy.locator(".ember-power-select-trigger").first.wait_for(
                    state="attached", timeout=90_000
                )
                return
            except PWTimeout:
                ainda_carregando = loy.get_by_text(
                    re.compile(r"Carregando", re.I)
                ).count() > 0
                if tentativa == 1 and ainda_carregando:
                    console.log("[yellow]   Loy preso em 'Carregando' — recarregando...[/yellow]")
                    loy.goto(href, timeout=self.timeout, wait_until="domcontentloaded")
                    continue
                raise

    def _loy_triggers(self, loy: Page) -> list[dict]:
        """Lista os power-selects do Loy com o contexto (label proximo) e o
        valor atual — base pra achar Instancia/Categoria/Envolvimento/Tipo."""
        # CRITICO (15/06, 1634882/TJRS): o `i` retornado tem que ser o indice
        # REAL no DOM, porque _loy_escolher clica com .nth(i) sobre TODOS os
        # triggers (visiveis ou nao). Filtrar ANTES de numerar desalinhava os
        # indices quando o tribunal tinha um power-select invisivel/desabilitado
        # a mais — o robo clicava num trigger aria-disabled (timeout 30s).
        return self._eval(loy, """() => {
            return Array.from(document.querySelectorAll('.ember-power-select-trigger'))
                .map((t, i) => ({ t, i }))
                .filter(o => o.t.offsetParent !== null)
                .map(({ t, i }) => {
                    let ctx = '';
                    let p = t.parentElement;
                    let card = '';
                    for (let k = 0; k < 6 && p; k++) {
                        const txt = (p.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!ctx && txt.length > 8) ctx = txt;
                        card = txt;  // ancestral mais alto visitado (bloco amplo)
                        p = p.parentElement;
                    }
                    return {
                        i,
                        contexto: ctx.slice(0, 150),
                        card: card.slice(0, 300),
                        valor: (t.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
                    };
                });
        }""")

    def _loy_escolher(self, loy: Page, indice: int, valor, contexto: str,
                      pular_se_travado: bool = False) -> bool:
        """Clica o power-select de indice `indice` e escolhe a opcao cujo
        texto casa com `valor`.

        `valor` pode ser uma string (1 valor) OU uma lista de candidatos em
        ordem de PRIORIDADE (valor da planilha primeiro, depois sinonimos —
        22/06). O robo le as opcoes REAIS do dropdown daquele tribunal e
        escolhe a 1a que casa: match EXATO (normalizado) tem prioridade sobre
        'contem'; dentro do mesmo tipo de match vale a ordem dos candidatos.
        So levanta erro se NENHUM candidato existir no dropdown.

        Retorna True se selecionou, False se PULOU por estar travado (so
        quando pular_se_travado=True)."""
        candidatos = [valor] if isinstance(valor, str) else list(valor)
        # tira vazios e duplicados (por _forte), preservando a ordem
        _vistos, _lista = set(), []
        for c in candidatos:
            if not c or not str(c).strip():
                continue
            cf = _forte(str(c))
            if cf and cf not in _vistos:
                _vistos.add(cf)
                _lista.append(str(c))
        candidatos = _lista
        if not candidatos:
            raise CalibracaoNecessaria(
                f"Loy: nenhum valor pra preencher o campo {contexto} "
                "(planilha vazia e sem fallback)."
            )
        trig = loy.locator(".ember-power-select-trigger").nth(indice)
        if trig.get_attribute("aria-disabled") == "true":
            # campo desabilitado = nao da pra preencher. Para campos que ja vem
            # fixados pelo tribunal (TIPO no eproc) isso e NORMAL — pular e
            # deixar o PROTOCOLAR validar. So vira erro onde travado e
            # inesperado (pular_se_travado=False).
            if pular_se_travado:
                console.log(f"[green]   ✓ {contexto}: já fixado pelo tribunal "
                            f"(campo travado) — seguindo.[/green]")
                return False
            if self.pasta_logs:
                dump_pagina(loy, self.pasta_logs, "loy_desabilitado")
            raise CalibracaoNecessaria(
                f"Loy: power-select do campo {contexto} (indice {indice}) esta "
                "DESABILITADO. Layout novo do tribunal ou dependencia nao "
                "preenchida — dump 'loy_desabilitado' salvo pra calibrar."
            )
        trig.click()
        opcoes = loy.locator("li.ember-power-select-option")
        # as opcoes do power-select carregam via AJAX (Ember) — esperar ate
        # aparecerem. CRITICO (15/06, 1139100): o TIPO do documento vinha com
        # 0 opcoes e o robo errava "opcao nao existe (0 opcoes)". Ate ~10s; se
        # vazio, reabre o dropdown uma vez.
        total = 0
        for tent in range(20):
            self._esperar(loy, 0.5)
            total = opcoes.count()
            if total > 0:
                break
            if tent == 9:  # na metade, reabre o dropdown (pode nao ter aberto)
                try:
                    loy.keyboard.press("Escape")
                    loy.locator(".ember-power-select-trigger").nth(indice).click()
                except Exception:
                    pass
        # le TODAS as opcoes uma vez (texto bruto + normalizado FORTE) e depois
        # casa contra os candidatos. Match EXATO (em todos os candidatos, na
        # ordem) tem prioridade sobre 'contem'; so depois tenta o parcial.
        # Assim "Petições Diversas" exato vence "Petição"->"Petição Simples"
        # parcial, mesmo se "Petição" vier antes na lista.
        textos = []
        textos_forte = []
        for i in range(min(total, 400)):
            bruto = opcoes.nth(i).inner_text()
            textos.append(bruto.strip())
            textos_forte.append(_forte(bruto))

        idx = None
        escolhido = None
        # passo 1: match EXATO, na ordem de prioridade dos candidatos
        for c in candidatos:
            a = _forte(c)
            for i, t in enumerate(textos_forte):
                if t == a:
                    idx, escolhido = i, c
                    break
            if idx is not None:
                break
        # passo 2: match parcial (contem), tambem na ordem dos candidatos
        if idx is None:
            for c in candidatos:
                a = _forte(c)
                if not a:
                    continue
                for i, t in enumerate(textos_forte):
                    if a in t or t in a:
                        idx, escolhido = i, c
                        break
                if idx is not None:
                    break

        if idx is None:
            loy.keyboard.press("Escape")
            if self.pasta_logs:
                dump_pagina(loy, self.pasta_logs, "loy_opcoes")
            amostra = " | ".join(t for t in textos[:20] if t)
            raise CalibracaoNecessaria(
                f"Loy: nenhum dos valores {candidatos} existe no campo "
                f"{contexto} ({total} opcoes). Opcoes: {amostra}"
            )
        texto = opcoes.nth(idx).inner_text().strip()
        opcoes.nth(idx).click()
        self._esperar(loy, 0.5)
        nota = "" if _forte(escolhido) == _forte(texto) else f" (candidato: {escolhido})"
        console.log(f"[green]   ✓ {contexto}:[/green] {texto}{nota}")
        return True

    def _loy_achar(self, triggers: list[dict], padrao: str) -> dict | None:
        rx = re.compile(padrao, re.I)
        for t in triggers:
            if rx.search(_normalizar(t["contexto"])):
                return t
        return None

    def _loy_trigger_travado(self, loy: Page, indice: int) -> bool:
        """True se o power-select de indice `indice` esta DESABILITADO
        (aria-disabled). Um select travado nao pode ser preenchido por
        ninguem — quando vazio e travado, o valor ja foi fixado pelo tribunal
        (TIPO no TJRS/EPROC; Envolvimento no TJSP/ESAJ) e deve ser PULADO,
        nao virar erro."""
        return (loy.locator(".ember-power-select-trigger").nth(indice)
                   .get_attribute("aria-disabled") == "true")

    def _loy_tipo_do_documento(self, loy: Page) -> str:
        """Le o TIPO ja atribuido ao documento da peticao no card 'Documentos'.
        Alguns tribunais (TJRS/EPROC, 15/06 1634882) ja vem com 'TIPO: PETIÇÃO'
        e o power-select TRAVADO — nesse caso nao ha (nem da pra) preencher.
        Retorna '' se nao houver TIPO definido ou se for so placeholder."""
        valor = self._eval(loy, r"""() => {
            const txt = (document.body ? document.body.innerText : '')
                .replace(/\s+/g, ' ');
            const m = txt.match(/\bTIPO:\s*(.*?)\s*(?:TAMANHO:|SIGILO:|ARQUIVO:|$)/i);
            return m ? m[1].trim() : '';
        }""") or ""
        # descarta placeholders/vazios ("Selecione...", etc)
        if re.search(r"^(selecion|escolh|\.\.\.|-)$", _normalizar(valor)):
            return ""
        return valor

    def preencher_loy(self, loy: Page, instancia: str, categoria: str,
                      tipo_arquivo: str, envolvimento: str = "Solicitante",
                      tipo_peticao: str = ""):
        """Preenche o Loy ate a tela final (NAO clica PROTOCOLAR):
        Instancia (col F) -> Categoria (col H) -> Tipo de Petição (col J, so
        existe em alguns tribunais ex. TJSP/ESAJ) -> Envolvimento da parte
        ativa = 'Solicitante' (quando a secao Partes existir; varia por
        tribunal) -> NOVA PETICAO -> TIPO do documento (col I); Sigilo,
        advogado e assinatura ja vem preenchidos pelo NPJUR."""
        # 1. Instancia
        t = self._loy_achar(self._loy_triggers(loy), r"INSTANCIA")
        if not t:
            if self.pasta_logs:
                dump_pagina(loy, self.pasta_logs, "loy_dados_basicos")
            raise CalibracaoNecessaria("Loy: campo Instancia nao encontrado.")
        if not t["valor"]:
            self._loy_escolher(loy, t["i"], instancia, "Instância")

        # 2. Categoria
        t = self._loy_achar(self._loy_triggers(loy), r"CATEGORIA")
        if not t:
            raise CalibracaoNecessaria("Loy: campo Categoria nao encontrado.")
        if not t["valor"]:
            self._loy_escolher(loy, t["i"], [categoria] + _FALLBACK_CATEGORIA, "Categoria")
        self._esperar(loy)

        # 2b. Tipo de Petição (Dados Básicos) — campo OBRIGATÓRIO que só existe
        #     em alguns tribunais (TJSP/ESAJ, 16/06 1494618/1436077). Vem da
        #     col J. Sem ele preenchido o Loy NÃO avança no NOVA PETIÇÃO e a
        #     seção Documentos não aparece (era a causa do "Documentos nao
        #     apareceu"). Selecionado DEPOIS da Categoria porque as opções
        #     costumam depender dela (cascata).
        # pega o trigger VAZIO cujo contexto fala em "Tipo de Petição" — a
        # Categoria já está preenchida (valor != ''), então fica naturalmente
        # de fora mesmo que os dois dividam o texto da linha em Dados Básicos.
        cand = [x for x in self._loy_triggers(loy)
                if not x["valor"]
                and re.search(r"TIPO DE PETI", _normalizar(x["contexto"]))]
        t = cand[0] if cand else None
        if t:
            # se a col J estiver vazia, NAO e mais erro: o robo cai nos
            # fallbacks (Petições Diversas -> Manifestação do Autor) e escolhe
            # o que existir no dropdown deste tribunal (22/06).
            self._loy_escolher(loy, t["i"],
                               [tipo_peticao] + _FALLBACK_TIPO_PETICAO,
                               "Tipo de Petição")
            self._esperar(loy)

        # 3. Envolvimento (secao Partes e Representantes — nem todo tribunal
        #    tem; regra do Lucas: parte ativa sempre 'Solicitante'). A parte
        #    ativa vem antes no DOM; preenche apenas a PRIMEIRA vazia.
        #    ESAJ (TJSP/ESAJ e TJMS, 16/06): o Envolvimento é OBRIGATÓRIO mas
        #    só HABILITA depois do Tipo de Petição (cascata) — por isso espera
        #    ele destravar antes de decidir. Sistemas eproc (TJRS, TJSP/eproc)
        #    não têm/travam o campo → continua pulando.
        envolvimentos = [x for x in self._loy_triggers(loy)
                         if re.search(r"ENVOLVIMENTO", _normalizar(x["contexto"]))]
        if envolvimentos and not envolvimentos[0]["valor"]:
            idx = envolvimentos[0]["i"]
            for _ in range(12):  # ~6s aguardando a cascata habilitar
                if not self._loy_trigger_travado(loy, idx):
                    break
                self._esperar(loy, 0.5)
            if self._loy_trigger_travado(loy, idx):
                console.log("[yellow]   ! Envolvimento da parte ativa DESABILITADO "
                            "neste tribunal — pulando (não é editável aqui).[/yellow]")
            else:
                self._loy_escolher(loy, idx, [envolvimento, "Solicitante"],
                                   "Envolvimento (parte ativa)")

        # 4. NOVA PETICAO -> aparece a secao Documentos com o PDF do 2ADV
        console.log("[blue]   → NOVA PETIÇÃO...[/blue]")
        loy.get_by_text(re.compile(r"NOVA PETI", re.I)).first.click()
        try:
            loy.get_by_text(re.compile(r"Documento da Peti", re.I)).first.wait_for(
                state="visible", timeout=self.timeout
            )
        except PWTimeout:
            if self.pasta_logs:
                dump_pagina(loy, self.pasta_logs, "loy_nova_peticao")
            raise CalibracaoNecessaria(
                "Loy: cliquei NOVA PETIÇÃO mas a secao Documentos nao apareceu "
                "(o PDF do 2ADV veio na tela?)."
            )
        self._esperar(loy)

        # 5. TIPO do documento — apos NOVA PETICAO e o select VAZIO da secao
        #    Documentos (Instancia/Categoria/Sigilo/Advogado ja vem). CRITICO
        #    (15/06, 1139100): se o TIPO nao for EFETIVADO, o Loy recusa no
        #    PROTOCOLAR com "Preencha o tipo de todos os Documentos". O
        #    power-select do Ember as vezes nao efetiva no 1o clique — por isso
        #    seleciona, CONFERE que o vazio sumiu e re-tenta ate 3x. Exclui
        #    Envolvimento/Instancia/Categoria pra nao confundir com outro vazio.
        def _achar_tipo_doc():
            vaz = [x for x in self._loy_triggers(loy) if not x["valor"]
                   and not re.search(r"ENVOLVIMENTO|INSTANCIA|CATEGORIA",
                                     _normalizar(x["contexto"]))]
            comtipo = sorted(
                [x for x in vaz
                 if re.search(r"\bTIPO\b", _normalizar(x["contexto"] + " " + x["card"]))],
                key=lambda x: 0 if re.search(r"\bTIPO\b", _normalizar(x["contexto"])) else 1,
            )
            if comtipo:
                return comtipo[0]
            return vaz[0] if len(vaz) == 1 else None

        # PRINCÍPIO (16/06, depois de erros repetidos): TIPO do documento NUNCA
        # vira erro fatal aqui. Em eproc (TJRS) o tribunal ja fixa o TIPO e o
        # select fica DESABILITADO — nao da pra preencher. Em ESAJ/outros pode
        # vir vazio+editavel e ai o robo preenche (col I). Em ambos, quem dá a
        # palavra final é o PROTOCOLAR: protocolar_loy ja detecta 'Preencha o
        # tipo' e recusa sem duplicar. Entao: deixa o doc estabilizar, tenta
        # preencher SE estiver editavel, e segue de qualquer forma.
        self._esperar(loy, 2.0)  # deixa o Ember popular/travar o TIPO (assíncrono)
        for tentativa in range(1, 4):
            alvo = _achar_tipo_doc()
            if alvo is None:
                break  # nao ha select vazio de TIPO = ja resolvido pelo tribunal
            # pular_se_travado=True: se o tribunal travou o TIPO, NAO levanta
            # erro — apenas para de tentar e segue pro PROTOCOLAR.
            ok = self._loy_escolher(loy, alvo["i"],
                                    [tipo_arquivo] + _FALLBACK_TIPO_ARQUIVO,
                                    "Tipo do documento", pular_se_travado=True)
            if not ok:
                break  # travado pelo tribunal = ja fixado
            self._esperar(loy, 1.0)
            if _achar_tipo_doc() is None:
                break  # efetivou (o select vazio sumiu)
            console.log(f"[yellow]   ! TIPO do documento nao persistiu "
                        f"(tentativa {tentativa}/3) — repetindo...[/yellow]")
        console.log("[green]   ✓ TIPO do documento resolvido (ou já fixado pelo "
                    "tribunal) — o PROTOCOLAR valida.[/green]")

        # 6. Sigilo deve estar 'Sem Sigilo' (ja vem por padrao)
        sigilo = [x for x in self._loy_triggers(loy) if re.search(r"SIGILO", _normalizar(x["valor"]))]
        if not sigilo:
            console.log("[yellow]   ! Sigilo nao parece preenchido — confere na pausa[/yellow]")

        # 7. advogado + assinatura vem preenchidos; so avisa se nao vierem
        assinatura_ok = self._eval(loy, """() => {
            const inp = Array.from(document.querySelectorAll('input[type=password], input[type=text]'))
                .find(x => /assinatura/i.test(((x.closest('div')||{}).innerText)||''));
            return inp ? !!inp.value : null;
        }""")
        if assinatura_ok is False:
            console.log("[yellow]   ! Assinatura Eletrônica VAZIA — preenche na pausa antes de protocolar[/yellow]")
        console.log("[green]   ✓ Loy preenchido — pronto pra PROTOCOLAR[/green]")

    def protocolar_loy(self, loy: Page):
        """Clica PROTOCOLAR e espera a confirmacao REAL: modal 'Petição
        enviada para a fila com sucesso!' e/ou redirect de volta pro NPJUR
        (retorno_peticionamento.php) com 'finalizado com sucesso'. Sem essa
        confirmacao, NAO considere peticionado."""
        console.log("[bold blue]→ PROTOCOLANDO no Loy...[/bold blue]")
        loy.get_by_text(re.compile(r"^\s*PROTOCOLAR\s*$", re.I)).first.click()

        confirmacao = None
        erro_loy = None
        for _ in range(30):  # ate ~45s — o Loy processa e redireciona
            self._esperar(loy, 1.5)
            try:
                url = loy.url or ""
                corpo = self._eval(loy, "() => document.body ? document.body.innerText : ''")
            except Exception:
                continue
            if re.search(r"enviada para a fila com sucesso", corpo, re.I):
                confirmacao = "modal do Loy"
            if "retorno_peticionamento" in url or re.search(r"finalizado com sucesso", corpo, re.I):
                confirmacao = "retorno no NPJUR (finalizado com sucesso)"
                break
            # erro explicito do Loy (ex: "Preencha o tipo de todos os Documentos
            # antes de enviar") — para na hora, nao adianta esperar 45s
            m = re.search(r"(Preencha[^.!]*|Erro[^.!]{0,80})(antes de enviar|obrigat[^.!]*)?", corpo, re.I)
            if re.search(r"Preencha o tipo|antes de enviar", corpo, re.I):
                erro_loy = m.group(0).strip() if m else "erro do Loy"
                break
        if not confirmacao:
            if self.pasta_logs:
                dump_pagina(loy, self.pasta_logs, "loy_protocolar")
            if erro_loy:
                raise CalibracaoNecessaria(
                    f"Loy RECUSOU o protocolo: '{erro_loy}'. Nao protocolou (pode "
                    "rodar de novo sem risco de duplicar)."
                )
            raise CalibracaoNecessaria(
                "Cliquei PROTOCOLAR no Loy mas nao veio a confirmacao "
                "('Petição enviada para a fila com sucesso' / 'finalizado com "
                "sucesso'). CONFERE MANUALMENTE antes de rodar de novo — "
                "risco de protocolo duplicado."
            )
        console.log(f"[green]✓ PETICIONADO — confirmacao: {confirmacao}[/green]")

    @staticmethod
    def _parsear_prerequisitos(mensagem: str) -> list[str]:
        """Extrai a lista de acompanhamentos faltantes da mensagem de erro.

        Formato esperado: "Não foi possível lançar o Acompanhamento pois falta
        lançar os seguintes acompanhamentos ou está com data real menor que a
        que está sendo lançada: <item1> <quebra> <item2> ..."
        """
        m = re.search(
            r"falta lan[cç]ar os seguintes acompanhamentos[^:]*:\s*(.+)",
            mensagem, re.I | re.S,
        )
        if not m:
            return []
        bruto = m.group(1)
        itens = [
            i.strip(" \t-•*;,.")
            for i in re.split(r"[\n;]+|,(?=\S)", bruto)
        ]
        return [i for i in itens if len(i) > 3]

    # -------------------------------------------------------------------------
    # PASSO 12+13: cumprir prazo na fila + responder NAO ao novo agendamento
    # -------------------------------------------------------------------------
    def cumprir_prazo(self, npjur: str, id_agendamento: str, descricao_peticao: str):
        """
        Na fila: botao verde "cumprir prazo" (fValidaEmendaInicial), seleciona
        a peticao anexada e conclui. Ao dialog "Deseja realizar novo
        agendamento?" responde NAO (Lucas agenda custas manualmente).
        """
        self.garantir_na_fila(npjur)
        console.log(f"[blue]→ Cumprindo prazo (fValidaEmendaInicial({id_agendamento}))...[/blue]")

        def decisor(msg: str) -> bool:
            # "Deseja realizar novo agendamento?" -> NAO (dismiss/cancel)
            if re.search(r"novo agendamento", msg, re.I):
                console.log("[blue]   → Respondendo NAO ao novo agendamento[/blue]")
                return False
            return True  # demais confirms: OK

        # Clica o ICONE "Concluir Agendamento" da propria linha (em vez de
        # chamar a funcao JS — o onclick real e composto:
        # fValidaEmendaInicial(id,'339'); jsValidarDepositoGarantia(...)).
        botao = self.page.locator(
            f"tr[id='{id_agendamento}'] [title*='Concluir' i], "
            f"tr[id='{id_agendamento}'] [alt*='Concluir' i]"
        )
        if botao.count() == 0:
            raise CalibracaoNecessaria(
                "Icone 'Concluir Agendamento' nao encontrado na linha da fila."
            )

        # Pode abrir popup com a selecao da peticao, ou agir na propria pagina
        alvo: Page = self.page
        dialogos_fila = CapturaDialogos(self.page, decisor)
        dialogos_pop = None
        try:
            try:
                with self.page.context.expect_page(timeout=6000) as pop_info:
                    botao.first.click()
                alvo = pop_info.value
                alvo.wait_for_load_state("domcontentloaded", timeout=self.timeout)
                dialogos_pop = CapturaDialogos(alvo, decisor)
                console.log("[blue]   → Abriu em popup[/blue]")
            except PWTimeout:
                console.log("[blue]   → Sem popup — continuando na propria pagina[/blue]")
            self._esperar(alvo)

            # Seleciona a peticao anexada: radio/checkbox na linha cujo texto
            # casa com a descricao (ou o primeiro disponivel se houver so um)
            marcado = alvo.evaluate(
                """(descricao) => {
                    const norm = (s) => s.toUpperCase()
                        .normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                        .replace(/\\s+/g, ' ').trim();
                    const alvo = norm(descricao);
                    const caixas = Array.from(
                        document.querySelectorAll("input[type=radio], input[type=checkbox]")
                    ).filter(c => c.offsetParent !== null);
                    if (caixas.length === 0) return { ok: false, motivo: 'nenhum radio/checkbox visivel' };
                    // tenta achar a caixa cuja linha (tr) menciona a descricao
                    let escolhida = caixas.find(c => {
                        const tr = c.closest('tr');
                        return tr && norm(tr.innerText).includes(alvo);
                    });
                    if (!escolhida && caixas.length === 1) escolhida = caixas[0];
                    if (!escolhida) return { ok: false, motivo: 'varias caixas e nenhuma casa com a descricao', total: caixas.length };
                    escolhida.click();
                    return { ok: true };
                }""",
                descricao_peticao,
            )
            if not marcado.get("ok"):
                # Pode nao haver selecao nenhuma (fluxo direto) — segue e tenta concluir
                console.log(f"[yellow]   ! Selecao de peticao: {marcado.get('motivo')} — tentando concluir direto[/yellow]")
            self._esperar(alvo, 0.5)

            # Botao de conclusao
            concluiu = False
            for seletor in ("button:has-text('Concluir')",
                            "input[type=button][value*='Concluir' i]",
                            "input[type=submit][value*='Concluir' i]",
                            "button:has-text('Confirmar')",
                            "input[type=button][value*='Confirmar' i]",
                            "#gravar",
                            "input[type=button][value*='Gravar' i]"):
                loc = alvo.locator(seletor)
                if loc.count() > 0 and loc.first.is_visible():
                    console.log(f"[blue]   → Clicando ({seletor})...[/blue]")
                    loc.first.click()
                    concluiu = True
                    break
            if not concluiu:
                raise CalibracaoNecessaria(
                    "Botao Concluir/Confirmar nao encontrado na tela de cumprir prazo."
                )

            self._esperar(alvo, 2.0)
            msgs = dialogos_fila.mensagens + (dialogos_pop.mensagens if dialogos_pop else [])
            if msgs:
                console.log(f"[blue]   mensagens do sistema:[/blue] {msgs}")
            console.log("[green]✓ Prazo cumprido na fila[/green]")
        finally:
            dialogos_fila.desligar()
            if dialogos_pop:
                dialogos_pop.desligar()
            if alvo is not self.page:
                try:
                    alvo.close()
                except Exception:
                    pass
            self._fila_atual = None  # forca rebusca no proximo processo
