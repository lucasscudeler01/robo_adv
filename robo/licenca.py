"""
Trava mensal do robo (licenca).

COMO FUNCIONA (resumo):
- O admin (Lucas) mantem um Gist secreto no GitHub com o conteudo, por mes:
      {"2026-07": "<hash sha256 da senha do mes>"}
- Ao abrir, o robo pede a senha, calcula o hash e compara com o do MES ATUAL
  lido do Gist. Bate -> roda; nao bate / mes sem senha / sem internet -> para.
- Trocar a senha todo mes (ou esvaziar o Gist) e o "botao de desligar" remoto.

SEGURANCA (honesta): o robo roda na maquina do usuario, entao isto DISSUADE
compartilhamento casual e da um kill-switch remoto — NAO e uma trava
inviolavel. O Gist guarda o HASH, nunca a senha em texto.
"""

import hashlib
import json
import sys
import time
import urllib.request

# "Pimenta" fixa concatenada antes do hash. Como o codigo e aberto, isto nao
# e segredo forte — so evita comparar o hash com tabelas genericas prontas.
# O gerar_senha.py usa a MESMA constante, entao nao mude sem regerar a senha.
_SALT = "robo_npjur::v1::"


def _hash(senha: str) -> str:
    return hashlib.sha256((_SALT + (senha or "").strip()).encode("utf-8")).hexdigest()


def mes_atual() -> str:
    return time.strftime("%Y-%m")


def _baixar_json(url: str, timeout: float = 15.0) -> dict:
    """Baixa e parseia o JSON do Gist. Cache-Control no-cache pra sempre pegar
    a versao mais nova (o GitHub raw as vezes cacheia alguns minutos)."""
    req = urllib.request.Request(
        url,
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache",
                 "User-Agent": "robo-npjur"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        bruto = r.read().decode("utf-8")
    dados = json.loads(bruto)
    if not isinstance(dados, dict):
        raise ValueError("conteudo do Gist nao e um objeto JSON {\"mes\": \"hash\"}")
    return dados


def senha_confere(senha: str, dados: dict, mes: str) -> bool:
    """True se a senha bate com o hash do mes informado. Funcao PURA (sem
    rede/input) — base dos testes."""
    esperado = dados.get(mes)
    if not esperado:
        return False
    return _hash(senha) == str(esperado).strip().lower()


def _pedir_senha(mes: str) -> str:
    # Senha VISIVEL (input normal): pra amigos, esconder o texto so confunde
    # ("parece que nao digita nada") e nao agrega seguranca real numa senha
    # compartilhada. Por isso NAO usa getpass.
    return input(f"Senha do mes ({mes}) e ENTER: ")


def checar_licenca(url: str, tentativas: int = 3) -> None:
    """Bloqueia (sys.exit) se a senha do mes nao bater. Chamada no inicio do
    main(). FAIL-CLOSED: sem internet ou Gist fora do ar, NAO roda (o robo ja
    precisa de internet pra operar o NPJUR, entao isto nao atrapalha o uso
    legitimo)."""
    if not url or "COLE_AQUI" in url:
        print("ERRO: licenca nao configurada — falta a URL do Gist no config.yaml "
              "(licenca > gist_url).")
        sys.exit(1)

    try:
        dados = _baixar_json(url)
    except (ValueError, json.JSONDecodeError) as e:
        # conectou, mas o conteudo do Gist veio vazio ou nao e JSON valido
        print("O Gist da senha esta VAZIO ou com conteudo invalido.")
        print("Cole nele uma linha como:  {\"%s\": \"<hash>\"}" % mes_atual())
        print("(gere com  python gerar_senha.py  e confira a URL no config.yaml).")
        print(f"Detalhe tecnico: {e}")
        sys.exit(1)
    except Exception as e:
        print("Nao consegui acessar o Gist (sem internet ou URL errada).")
        print(f"Detalhe: {e}")
        print("Conecte a internet e confira a 'gist_url' no config.yaml.")
        sys.exit(1)

    mes = mes_atual()
    if not dados.get(mes):
        print(f"O robo esta DESATIVADO para este mes ({mes}).")
        print("Peca a senha do mes atual para quem te passou o robo.")
        sys.exit(1)

    for i in range(1, tentativas + 1):
        senha = _pedir_senha(mes)
        if senha_confere(senha, dados, mes):
            print("Licenca OK.")
            return
        restam = tentativas - i
        if restam:
            print(f"Senha incorreta. Tentativas restantes: {restam}")
    print("Senha incorreta. Encerrando.")
    sys.exit(1)
