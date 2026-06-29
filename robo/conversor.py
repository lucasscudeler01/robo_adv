"""
Conversao Word -> PDF usando o Microsoft Word instalado (via COM direto).

IMPORTANTE: usa DispatchEx, que abre uma instancia NOVA e INVISIVEL do Word
so pro robo — nao encosta nos documentos que o Lucas estiver editando.
(O docx2pdf usado antes fazia Dispatch + Quit(), que sequestrava o Word
aberto do Lucas e disparava o dialog "Deseja salvar?" — erro Open.SaveAs.)
"""

import re
import time
from pathlib import Path

from rich.console import Console

console = Console()

# Caracteres proibidos em nome de arquivo no Windows
_INVALIDOS = re.compile(r'[<>:"/\\|?*]')

_WD_FORMAT_PDF = 17  # wdFormatPDF
_WD_NAO_SALVAR = 0   # wdDoNotSaveChanges
_WD_WRAP_CONTINUE = 1  # wdFindContinue
_WD_REPLACE_ALL = 2    # wdReplaceAll

# Textos do ajuste de desentranhamento com novo endereco (modelo da imagem
# do Lucas, 12/06/2026)
_TRECHO_ORIGINAL = "endereço constante da inicial"
_TRECHO_NOVO = "endereço a seguir"
_PARAGRAFO_CUSTAS = ("No mais, requer a concessão de 05 (cinco) dias de "
                     "prazo para juntada das respectivas custas.")

# Dilação de prazo (15/06/2026, pedido do Lucas): o modelo "DILAÇÃO DE PRAZO
# - ATIVAS" do gerador vem com o pedido de outro tipo ("...o prazo de 20 dias
# para juntada da minuta do acordo."). O robô troca SO o pedido — tudo que vem
# depois de "REQUERER" ate o fim daquele paragrafo — pelo texto abaixo.
# Cabecalho, processo, partes e enderecamento (preenchidos pelo NPJUR) ficam
# exatamente como baixados.
_PEDIDO_DILACAO = ("a concessão de prazo suplementar de 10 dias para "
                   "prosseguimento no feito.")

# Peticao livre (22/06/2026, pedido do Lucas): mesmo modelo da dilacao de
# prazo (col D), mas em vez do pedido fixo, troca o final do 1o paragrafo
# por "...a fim de expor e requerer o que segue." e insere o texto LIVRE da
# col L (sem negrito) como paragrafo(s) seguinte(s).
_FINAL_PRIMEIRO_PARAGRAFO = "a fim de expor e requerer o que segue."


def _aplicar_peticao_livre(word, doc, texto_pedido: str):
    """Edita a peca pro caso 'peticao livre' (col C == 'PETIÇÃO'):
      1. troca o trecho de 'a fim de REQUERER' ate o fim do 1o paragrafo por
         '...a fim de expor e requerer o que segue.'
      2. insere o texto livre (col L) como paragrafo proprio, SEM negrito.
    Levanta RuntimeError se nao achar 'REQUERER' (modelo errado na planilha).
    """
    rng = doc.Content
    f = rng.Find
    f.ClearFormatting()
    achou = f.Execute("a fim de REQUERER", False, False, False, False, False,
                      True, _WD_WRAP_CONTINUE, False)
    if not achou:
        rng = doc.Content
        f = rng.Find
        f.ClearFormatting()
        achou = f.Execute("REQUERER", False, False, False, False, False,
                          True, _WD_WRAP_CONTINUE, False)
        if not achou:
            raise RuntimeError(
                "A peca de peticao livre nao contem 'REQUERER' — confere se "
                "a tese da planilha (col D) e mesmo o modelo de dilacao de "
                "prazo, usado como base pra esse fluxo."
            )
    para = rng.Paragraphs(1)
    inicio = rng.Start
    fim = para.Range.End - 1
    if fim <= inicio:
        raise RuntimeError("Paragrafo do pedido (peticao livre) vazio/inesperado.")
    alvo = doc.Range(inicio, fim)
    alvo.Text = _FINAL_PRIMEIRO_PARAGRAFO

    # reencontra o fim do paragrafo que agora termina em
    # "...requerer o que segue." pra inserir o pedido livre depois dele
    rng2 = doc.Content
    f2 = rng2.Find
    f2.ClearFormatting()
    if not f2.Execute(_FINAL_PRIMEIRO_PARAGRAFO, False, False, False, False,
                      False, True, _WD_WRAP_CONTINUE, False):
        raise RuntimeError("Troca do final do 1o paragrafo aplicada mas o trecho novo nao foi reencontrado.")
    para_pedido = rng2.Paragraphs(1)
    fim2 = para_pedido.Range.End

    pedido = texto_pedido.strip()
    ins = doc.Range(fim2, fim2)
    ins.Text = "\r" + pedido + "\r"
    ins.Font.Bold = False
    paras = ins.Paragraphs
    p_pedido = paras(2)
    p_pedido.Format = para_pedido.Format  # mesma formatacao (justificado + recuo) do resto da peca
    p_pedido.Range.Font.Bold = False
    console.log(f"[green]   ✓ Pedido livre inserido:[/green] {pedido[:80]}")


def nome_pdf(cnj: str | None, npjur: str) -> str:
    """Nome do PDF: numero CNJ do processo se tiver, senao o NPJUR."""
    base = cnj if cnj else f"NPJUR_{npjur}"
    return _INVALIDOS.sub("-", base).strip() + ".pdf"


def _aplicar_endereco(word, doc, endereco: str):
    """Edita a peca aberta no Word pro caso 'desentranhamento com novo
    endereco' (modelo da imagem do Lucas, 12/06/2026):
      1. troca '...no endereço constante da inicial.' por '...no endereço
         a seguir.'
      2. insere o endereco em NEGRITO, recuado, em paragrafo proprio
      3. insere o paragrafo dos 05 dias pra juntada das custas
    Levanta RuntimeError se o modelo nao tiver o trecho esperado (tese
    errada na planilha, por exemplo).
    """
    find = doc.Content.Find
    find.ClearFormatting()
    find.Replacement.ClearFormatting()
    trocou = find.Execute(
        _TRECHO_ORIGINAL,     # FindText
        False, False, False,  # MatchCase, MatchWholeWord, MatchWildcards
        False, False,         # MatchSoundsLike, MatchAllWordForms
        True, _WD_WRAP_CONTINUE, False,  # Forward, Wrap, Format
        _TRECHO_NOVO, _WD_REPLACE_ALL,   # ReplaceWith, Replace
    )
    if not trocou:
        raise RuntimeError(
            f"A peca nao contem '{_TRECHO_ORIGINAL}' — confere se a tese "
            "da planilha e mesmo o modelo de desentranhamento/expedicao."
        )

    # localiza o paragrafo que terminou em "no endereço a seguir."
    rng = doc.Content
    f2 = rng.Find
    f2.ClearFormatting()
    if not f2.Execute(_TRECHO_NOVO, False, False, False, False, False,
                      True, _WD_WRAP_CONTINUE, False):
        raise RuntimeError("Troca do endereco aplicada mas o trecho novo nao foi reencontrado.")
    para_requerer = rng.Paragraphs(1)
    fim = para_requerer.Range.End

    addr = endereco.strip().rstrip(".").upper() + "."
    ins = doc.Range(fim, fim)
    ins.Text = "\r" + addr + "\r\r" + _PARAGRAFO_CUSTAS + "\r"
    # apos atribuir .Text o range passa a cobrir exatamente o texto inserido:
    # paragrafos = [vazio, ENDERECO, vazio, custas]
    ins.Font.Bold = False
    paras = ins.Paragraphs
    p_addr = paras(2)
    p_custas = paras(4)
    p_custas.Format = para_requerer.Format  # justificado + recuo de 1a linha
    p_addr.Format = para_requerer.Format
    p_addr.Format.FirstLineIndent = 0
    p_addr.Format.LeftIndent = 155.9  # 5,5 cm em pontos (recuo do bloco, como na peca do Lucas)
    p_addr.Format.Alignment = 0  # esquerda
    p_addr.Range.Font.Bold = True
    console.log(f"[green]   ✓ Peca ajustada com o novo endereco:[/green] {addr[:80]}")


def _aplicar_pedido_dilacao(word, doc):
    """Troca SO o pedido da peca de dilacao de prazo: tudo que vem depois de
    'a fim de REQUERER' ate o fim daquele paragrafo passa a ser o pedido
    padrao de dilacao (_PEDIDO_DILACAO). O resto da peca — enderecamento,
    numero do processo, partes — fica como o NPJUR baixou.
    Levanta RuntimeError se nao achar o 'REQUERER' (modelo inesperado)."""
    rng = doc.Content
    f = rng.Find
    f.ClearFormatting()
    achou = f.Execute("a fim de REQUERER", False, False, False, False, False,
                      True, _WD_WRAP_CONTINUE, False)
    if not achou:
        rng = doc.Content
        f = rng.Find
        f.ClearFormatting()
        achou = f.Execute("REQUERER", False, False, False, False, False,
                          True, _WD_WRAP_CONTINUE, False)
        if not achou:
            raise RuntimeError(
                "A peca de dilacao nao contem 'REQUERER' — confere se a tese "
                "da planilha e mesmo o modelo de dilacao de prazo."
            )
    # rng cobre o texto achado; o pedido vai do fim de "REQUERER" ate o fim
    # do paragrafo (antes da marca de paragrafo)
    para = rng.Paragraphs(1)
    inicio = rng.End
    fim = para.Range.End - 1
    if fim <= inicio:
        raise RuntimeError("Paragrafo do pedido de dilacao vazio/inesperado.")
    alvo = doc.Range(inicio, fim)
    alvo.Text = " " + _PEDIDO_DILACAO
    console.log(f"[green]   ✓ Pedido da dilacao trocado:[/green] {_PEDIDO_DILACAO[:70]}")


def word_para_pdf(caminho_word: Path, caminho_pdf: Path,
                  endereco: str | None = None,
                  adaptar_dilacao: bool = False,
                  pedido_livre: str | None = None) -> Path:
    """Converte 1 arquivo Word pra PDF numa instancia isolada do Word.
    Se `endereco` vier preenchido (desentranhamento com novo endereco) OU
    `adaptar_dilacao` for True (dilacao de prazo) OU `pedido_livre` vier
    preenchido (peticao livre, col C == 'PETIÇÃO'), edita a peca ANTES de
    converter — o .docx original fica intacto, so o PDF sai editado.
    Tenta 2x (COM as vezes engasga na primeira chamada)."""
    caminho_word = Path(caminho_word).resolve()
    caminho_pdf = Path(caminho_pdf).resolve()
    if not caminho_word.exists():
        raise FileNotFoundError(f"Word nao encontrado: {caminho_word}")

    import pythoncom
    import win32com.client

    ultima_excecao = None
    for tentativa in (1, 2):
        console.log(f"[blue]→ Convertendo Word → PDF (tentativa {tentativa})...[/blue]")
        word = None
        doc = None
        pythoncom.CoInitialize()
        try:
            # DispatchEx = processo novo do Word, separado de qualquer Word
            # que o usuario esteja usando. Visible=False + DisplayAlerts=0
            # garantem que nenhuma janela/dialog aparece.
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(
                str(caminho_word),
                ReadOnly=True,
                AddToRecentFiles=False,
                ConfirmConversions=False,
            )
            if endereco:
                _aplicar_endereco(word, doc, endereco)
            if adaptar_dilacao:
                _aplicar_pedido_dilacao(word, doc)
            if pedido_livre:
                _aplicar_peticao_livre(word, doc, pedido_livre)
            doc.SaveAs2(str(caminho_pdf), FileFormat=_WD_FORMAT_PDF)
        except Exception as e:
            ultima_excecao = e
        finally:
            try:
                if doc is not None:
                    doc.Close(SaveChanges=_WD_NAO_SALVAR)
            except Exception:
                pass
            try:
                if word is not None:
                    word.Quit()  # fecha SO a instancia do robo
            except Exception:
                pass
            pythoncom.CoUninitialize()

        if caminho_pdf.exists() and caminho_pdf.stat().st_size > 0:
            console.log(f"[green]✓ PDF gerado:[/green] {caminho_pdf}")
            return caminho_pdf
        if ultima_excecao is None:
            ultima_excecao = RuntimeError("conversao terminou mas o PDF ficou vazio/inexistente")
        time.sleep(3)

    raise RuntimeError(
        f"Falha ao converter {caminho_word.name} pra PDF: {ultima_excecao}"
    )
