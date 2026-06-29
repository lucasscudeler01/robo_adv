"""
Limpeza recorrente dos arquivos gerados pelo robô.

OBJETIVO: nao acumular dump_*.txt, relatorio_*.csv e PDFs/Words antigos.
POLITICA: retencao ROLANTE por rodada. Cada rodada do robo gera exatamente
1 relatorio_*.csv (no fim). Mantemos os `manter_rodadas` relatorios mais
recentes e apagamos tudo que for mais ANTIGO que o mais antigo mantido.
Assim cada arquivo "vive" ~`manter_rodadas` rodadas e depois e excluido
(pedido do Lucas: a cada ~5 usos, some o que e velho).

SEGURANCA (o que NUNCA e apagado):
  - estado.json (e .bak/.tmp) — o checkpoint que evita reprotocolar
  - qualquer .pdf/.docx ainda referenciado no estado por um processo que
    NAO chegou em 'concluido' (pra nao quebrar um retomar de processo
    incompleto, que usa o caminho salvo do Word/PDF)
  - config, planilha, perfil do Chrome, codigo
"""

from pathlib import Path
from rich.console import Console

console = Console()


def _arquivos_protegidos(estado) -> set[str]:
    """Nomes de Word/PDF que NAO podem ser apagados porque pertencem a um
    processo ainda incompleto (pode ser retomado e o caminho vem do estado)."""
    protegidos: set[str] = set()
    try:
        for _npjur, p in (estado.dados or {}).items():
            etapas = p.get("etapas", {})
            if "concluido" in etapas:
                continue  # ja terminou: pode apagar os arquivos dele
            for et in ("word", "pdf"):
                info = etapas.get(et)
                caminho = info.get("info") if isinstance(info, dict) else None
                if caminho:
                    protegidos.add(Path(caminho).name)
    except Exception:
        pass  # protecao e best-effort; nunca derruba a execucao
    return protegidos


def limpar_antigos(pasta_logs: Path, pasta_pdfs: Path, estado,
                   manter_rodadas: int = 5) -> None:
    """Apaga dumps, relatorios e PDFs/Words mais antigos que as ultimas
    `manter_rodadas` rodadas. Roda no inicio de cada execucao. Tolerante a
    erro: se algo falhar, apenas avisa e segue (limpeza nunca trava o robo)."""
    pasta_logs = Path(pasta_logs)
    pasta_pdfs = Path(pasta_pdfs)
    if manter_rodadas < 1:
        return

    relatorios = sorted(
        pasta_logs.glob("relatorio_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if len(relatorios) <= manter_rodadas:
        return  # ainda nao ha rodadas suficientes pra limpar

    manter = relatorios[:manter_rodadas]
    cutoff = manter[-1].stat().st_mtime  # mtime do relatorio mais antigo mantido
    protegidos = _arquivos_protegidos(estado)

    apagados = 0
    erros = 0

    def _apagar(p: Path):
        nonlocal apagados, erros
        try:
            p.unlink()
            apagados += 1
        except Exception:
            erros += 1

    # 1) relatorios alem dos mantidos
    for r in relatorios[manter_rodadas:]:
        _apagar(r)

    # 2) dumps (puro diagnostico, nunca referenciados) anteriores ao cutoff
    for padrao in ("dump_*.txt", "dump_loy_*.txt"):
        for d in pasta_logs.glob(padrao):
            if d.stat().st_mtime < cutoff:
                _apagar(d)

    # 3) PDFs/Words gerados, anteriores ao cutoff, exceto os protegidos
    for padrao in ("*.pdf", "*.docx"):
        for f in pasta_pdfs.glob(padrao):
            if f.stat().st_mtime < cutoff and f.name not in protegidos:
                _apagar(f)

    if apagados or erros:
        msg = f"[dim]Limpeza: {apagados} arquivo(s) antigo(s) removido(s) " \
              f"(mantendo as ultimas {manter_rodadas} rodadas)"
        if erros:
            msg += f"; {erros} nao puderam ser apagados (em uso?)"
        msg += ".[/dim]"
        console.print(msg)
