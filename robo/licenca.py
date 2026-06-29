"""
Trava mensal do robo (licenca) — versao SIMPLES (edicao direta no Gist).

O admin (Lucas) mantem um Gist secreto com UMA LINHA no formato:

    AAAA-MM senha

Exemplo:
    2026-06 bauru123

Ao abrir, o robo le essa linha. Se o mes da linha for o mes ATUAL e a senha
digitada bater, roda.
  - Trocar a senha   = editar so o texto DEPOIS do espaco, no Gist.
  - Virar o mes      = trocar a data tambem (ex.: 2026-07 ...).
  - Esvaziar o Gist  = ninguem roda (botao de desligar).
Mudar a senha NAO precisa de ferramenta nenhuma: edita o Gist na mao.

SEGURANCA (honesta): o robo roda na maquina do usuario e a senha vai no Gist
em texto. Isso DISSUADE compartilhamento casual e da um kill-switch remoto —
nao e trava inviolavel. Como a senha ja e compartilhada (WhatsApp) e o codigo
e aberto, guardar em texto e o mais pratico e nao perde seguranca real.
"""

import re
import sys
import time
import urllib.request

# o "mes" tem que ser AAAA-MM (4 digitos, hifen, 2 digitos). Assim conteudo
# antigo em JSON ({"2026-06": ...}) NAO passa por engano — cai na mensagem de
# "fora do formato", que e clara.
_MES_RX = re.compile(r"^\d{4}-\d{2}$")


def mes_atual() -> str:
    return time.strftime("%Y-%m")


def _baixar_texto(url: str, timeout: float = 15.0) -> str:
    """Baixa o conteudo cru do Gist (texto). no-cache pra sempre pegar a
    versao mais nova."""
    req = urllib.request.Request(
        url,
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache",
                 "User-Agent": "robo-npjur"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def _parse(texto: str):
    """'AAAA-MM senha' -> (mes, senha). Tolera espacos/linhas em volta e
    senha com espacos. Retorna (None, None) se vazio ou sem os dois campos."""
    linha = (texto or "").strip()
    if not linha:
        return None, None
    partes = linha.split(None, 1)  # 1o espaco separa o mes do resto (a senha)
    if len(partes) < 2 or not partes[1].strip():
        return None, None
    mes = partes[0].strip()
    if not _MES_RX.match(mes):  # nao e AAAA-MM (ex.: sobrou JSON antigo)
        return None, None
    return mes, partes[1].strip()


def senha_confere(senha: str, conteudo: str, mes: str) -> bool:
    """Funcao PURA (sem rede/input), base dos testes: True se o conteudo do
    Gist e do mes informado E a senha bate."""
    m, s = _parse(conteudo)
    if not m or m != mes:
        return False
    return (senha or "").strip() == s


def _pedir_senha(mes: str) -> str:
    # senha VISIVEL: pra amigos, esconder so confunde e nao agrega seguranca.
    return input(f"Senha do mes ({mes}) e ENTER: ")


def checar_licenca(url: str, tentativas: int = 3) -> None:
    """Bloqueia (sys.exit) se a senha do mes nao bater. Chamada no inicio do
    main(). FAIL-CLOSED: sem internet/URL errada, NAO roda."""
    if not url or "COLE_AQUI" in url:
        print("ERRO: licenca nao configurada — falta a URL do Gist no config.yaml "
              "(licenca > gist_url).")
        sys.exit(1)

    try:
        texto = _baixar_texto(url)
    except Exception as e:
        print("Nao consegui acessar o Gist (sem internet ou URL errada).")
        print(f"Detalhe: {e}")
        print("Conecte a internet e confira a 'gist_url' no config.yaml.")
        sys.exit(1)

    mes_gist, senha_valida = _parse(texto)
    if not mes_gist or not senha_valida:
        print("O Gist da senha esta VAZIO ou fora do formato.")
        print(f"Coloque UMA linha assim:   {mes_atual()} suasenha")
        sys.exit(1)

    mes = mes_atual()
    if mes_gist != mes:
        print(f"O robo esta DESATIVADO: o Gist esta no mes {mes_gist}, mas hoje e {mes}.")
        print("Peca a quem te passou o robo pra atualizar a senha do mes.")
        sys.exit(1)

    for i in range(1, tentativas + 1):
        if senha_confere(_pedir_senha(mes), texto, mes):
            print("Licenca OK.")
            return
        restam = tentativas - i
        if restam:
            print(f"Senha incorreta. Tentativas restantes: {restam}")
    print("Senha incorreta. Encerrando.")
    sys.exit(1)
