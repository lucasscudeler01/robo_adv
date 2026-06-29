"""
GERADOR DA SENHA DO MES — ferramenta do ADMIN (Lucas), nao distribuir.

Uso:
    python gerar_senha.py
Digite a senha nova do mes. O script imprime o JSON pronto pra colar no Gist
(substituindo TODO o conteudo). No mes seguinte, rode de novo com uma senha
nova: as senhas de meses anteriores param de funcionar sozinhas.
"""

from robo.licenca import _hash, mes_atual


def main():
    print("=== Gerador da senha do mes (ADMIN) ===")
    mes = input(f"Mes no formato AAAA-MM (Enter = atual {mes_atual()}): ").strip() or mes_atual()
    senha = input("Senha nova do mes: ").strip()
    if not senha:
        print("Senha vazia — cancelado.")
        return
    conteudo = '{"%s": "%s"}' % (mes, _hash(senha))
    print()
    print("1) COLE ISTO no Gist (substitua TODO o conteudo do arquivo):")
    print()
    print("   " + conteudo)
    print()
    print(f"2) Avise os amigos a senha do mes {mes}:  {senha}")


if __name__ == "__main__":
    main()
