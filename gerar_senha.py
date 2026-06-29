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

    # Salva num arquivo pra evitar erro de copiar a linha errada do terminal:
    # basta abrir esse arquivo, selecionar tudo (Ctrl+A), copiar e colar no Gist.
    from pathlib import Path
    arq = Path(__file__).parent / "senha_para_o_gist.txt"
    arq.write_text(conteudo, encoding="utf-8")

    print()
    print("=" * 60)
    print(f"PRONTO! Arquivo gerado:  {arq.name}")
    print("=" * 60)
    print()
    print("1) Abra o arquivo  senha_para_o_gist.txt  (na pasta do robo),")
    print("   selecione TUDO (Ctrl+A), copie (Ctrl+C) e cole no Gist")
    print("   (Edit -> apague tudo -> cole -> Save).")
    print()
    print("   O conteudo e exatamente este:")
    print("   " + conteudo)
    print()
    print(f"2) Avise os amigos a senha do mes {mes}:  {senha}")


if __name__ == "__main__":
    main()
