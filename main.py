"""
ROBO DE MANIFESTACAO NPJUR — entrada principal

Como rodar:
    run.bat   (ou: python main.py)

Fluxo completo por processo (cada etapa so roda se a anterior passou):
    word          -> baixa o Word do Gerador de Teses
    pdf           -> converte pra PDF (nome = numero CNJ do processo)
    anexado       -> anexa o PDF em Documentos Anexos (NPJUR)
    acompanhamento-> registra o Acompanhamento Processual (com pre-requisitos)
    peticionado   -> protocola via peticionamento eletronico do NPJUR/2ADV
                     (IRREVERSIVEL — pede confirmacao)
    concluido     -> cumpre o prazo na fila e responde NAO ao novo agendamento

CHECKPOINT: cada etapa concluida fica salva em logs/estado.json. Se o robo
parar no meio e voce rodar de novo, ele PULA o que ja foi feito — nunca
protocola duas vezes.

TESTE GRADUAL: o config.yaml tem `etapa_final`. Comece com "pdf" (nenhum
efeito no processo), depois "anexado", e so no fim "concluido".

CALIBRACAO BARATA: quando uma etapa falha, o robo salva logs/dump_*.txt
com o raio-X da tela. Cola o conteudo desse arquivo no Claude (em vez de
print) pra ajustar o seletor gastando o minimo de tokens.
"""

from pathlib import Path
import sys
import yaml
from rich.console import Console
from rich.panel import Panel

from robo.planilha import ler_planilha
from robo.browser import Navegador
from robo.npjur import NPJUR, CalibracaoNecessaria
from robo.conversor import word_para_pdf, nome_pdf
from robo.estado import Estado, ETAPAS
from robo.inspecao import dump_contexto
from robo.limpeza import limpar_antigos

console = Console()
RAIZ = Path(__file__).parent


def carregar_config() -> dict:
    with open(RAIZ / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def pausar(mensagem: str = "Pressione ENTER pra continuar..."):
    console.print(f"\n[yellow]⏸  {mensagem}[/yellow]")
    input()


def main():
    cfg = carregar_config()

    # Trava mensal: pede a senha do mes (validada no Gist online). Sem senha
    # valida -> nem comeca. Roda ANTES de qualquer coisa.
    from robo.licenca import checar_licenca
    checar_licenca(cfg.get("licenca", {}).get("gist_url", ""))

    etapa_final = cfg["execucao"].get("etapa_final", "pdf")
    if etapa_final not in ETAPAS:
        console.print(f"[red]etapa_final '{etapa_final}' invalida. Use uma de: {ETAPAS}[/red]")
        sys.exit(1)
    limite = ETAPAS.index(etapa_final)

    def roda(etapa: str) -> bool:
        return ETAPAS.index(etapa) <= limite

    # ----- 1. Planilha -----
    console.print(Panel.fit(
        f"[bold cyan]ROBO DE MANIFESTACAO NPJUR[/bold cyan]\n"
        f"vai ate a etapa: [bold]{etapa_final}[/bold]"
    ))
    caminho_planilha = RAIZ / cfg["planilha"]["caminho"]
    console.log(f"Lendo planilha: {caminho_planilha}")
    try:
        processos = ler_planilha(str(caminho_planilha), cfg["planilha"]["aba"])
    except Exception as e:
        console.print(f"[red]ERRO ao ler planilha:[/red] {e}")
        sys.exit(1)

    if not processos:
        console.print("[yellow]Planilha vazia. Nada a fazer.[/yellow]")
        return

    pasta_logs = RAIZ / cfg["saida"]["pasta_logs"]
    pasta_logs.mkdir(parents=True, exist_ok=True)
    estado = Estado(pasta_logs / "estado.json")

    # limpeza recorrente: mantem so as ultimas N rodadas de dumps/relatorios/
    # PDFs antigos (estado.json e arquivos de processo incompleto preservados)
    limpar_antigos(
        pasta_logs, RAIZ / cfg["saida"]["pasta_pdfs"], estado,
        manter_rodadas=cfg["saida"].get("manter_rodadas", 5),
    )

    console.print(f"[green]✓ {len(processos)} processo(s) na planilha[/green]")
    for p in processos:
        feita = estado.ultima_etapa(p.npjur)
        extra = f"  [dim](ja feito ate: {feita})[/dim]" if feita != "-" else ""
        console.print(f"   • {p}{extra}")

    # ----- 2. Navegador -----
    pasta_perfil = RAIZ / cfg["chrome"]["pasta_perfil"]
    pasta_pdfs = RAIZ / cfg["saida"]["pasta_pdfs"]

    modo = cfg["execucao"]["modo"]
    confirmar_protocolo = cfg["execucao"].get("confirmar_antes_protocolar", True)

    with Navegador(
        pasta_perfil=str(pasta_perfil),
        pasta_downloads=str(pasta_pdfs),
        headless=cfg["chrome"]["modo_headless"],
    ) as nav:
        nav.npjur.goto(cfg["urls"]["npjur_home"])
        console.print(Panel(
            "[bold]Confere se voce esta logado no NPJUR.[/bold]\n"
            "Se nao estiver, faz login manualmente agora.\n"
            "Quando estiver pronto, volte aqui e tecle ENTER."
        ))
        pausar()

        npjur = NPJUR(
            page=nav.npjur,
            url_minha_fila=cfg["urls"]["npjur_minha_fila"],
            pasta_downloads=pasta_pdfs,
            espera_reload=cfg["execucao"]["espera_reload"],
            timeout_pagina=cfg["execucao"]["timeout_pagina"],
            pasta_logs=pasta_logs,
            marcacao_processo=cfg["execucao"].get("marcacao_processo", "PROCESSUAL"),
            vara_padrao=cfg["execucao"].get("vara_padrao", "1ª VARA CÍVEL"),
        )

        # ----- 3. Loop nos processos -----
        ok, falhas = 0, 0
        for i, proc in enumerate(processos, start=1):
            console.print(f"\n[bold cyan]━━━ Processo {i}/{len(processos)}: {proc} ━━━[/bold cyan]")

            etapa_atual = "fila"
            try:
                # --- fila: id_agendamento + numero CNJ (sempre necessario) ---
                npjur.abrir_minha_fila()
                info = npjur.buscar_npjur_na_fila(proc.npjur)
                if not info:
                    # ja concluido numa rodada anterior saiu da fila — pular sem
                    # erro; so e erro de verdade se nunca foi concluido
                    if estado.feita(proc.npjur, "concluido"):
                        console.print(f"[dim]NPJUR {proc.npjur} ja concluido e fora da fila — nada a fazer.[/dim]")
                        ok += 1
                        continue
                    raise RuntimeError(f"NPJUR {proc.npjur} nao encontrado na fila")
                id_agenda, cnj = info["id"], info.get("cnj")
                id_juprocesso = info.get("id_juprocesso")
                # NPJUR canonico = o que aparece NA FILA (com zero a esquerda
                # que o Excel comeu). Todas as telas do NPJUR precisam dele
                # (15/06: 879948 na planilha = 0879948 na fila; o peticionamento
                # com 879948 nao carregava o processo). O estado/relatorio
                # continuam na chave da planilha (proc.npjur).
                npjur_real = info.get("npjur") or proc.npjur
                if npjur_real != proc.npjur:
                    console.print(f"[dim]   NPJUR na fila: {npjur_real} (planilha: {proc.npjur})[/dim]")

                # --- INTIMACAO NOVA? cada diligencia tem id_agendamento proprio.
                # O NPJUR e fixo por processo, entao uma intimacao nova reusa o
                # mesmo NPJUR — sem isto o robo PULARIA achando que ja fez.
                # Regra: se o agendamento atual difere do salvo -> reprocessa do
                # zero. Legado sem id salvo: so reprocessa se ja estava
                # 'concluido' (diligencia antiga ja saiu da fila); senao resume,
                # pra NUNCA reprotocolar a mesma diligencia pendente.
                stored_ag = estado.get_agendamento(proc.npjur)
                if stored_ag is None:
                    # legado (sem id_agenda salvo). So NAO reprocessa quando ja
                    # protocolou e nao concluiu (mesma diligencia pendente —
                    # evitar reprotocolar). Nos demais casos (ja concluido, ou
                    # ainda nem protocolou) trata o que esta na fila como a
                    # diligencia atual e reprocessa do zero.
                    if not (estado.feita(proc.npjur, "peticionado")
                            and not estado.feita(proc.npjur, "concluido")):
                        estado.resetar(proc.npjur)
                elif str(stored_ag) != str(id_agenda):
                    console.print(f"[cyan]   Intimacao NOVA no NPJUR {proc.npjur} "
                                  f"(agendamento {stored_ag} -> {id_agenda}) — reprocessando do zero.[/cyan]")
                    estado.resetar(proc.npjur)
                estado.set_agendamento(proc.npjur, id_agenda)

                if estado.feita(proc.npjur, etapa_final):
                    console.print(f"[dim]   Ja concluido ate '{etapa_final}' (mesma diligencia) — pulando.[/dim]")
                    ok += 1
                    continue

                # --- word ---
                etapa_atual = "word"
                if roda("word") and not estado.feita(proc.npjur, "word"):
                    page_teses = npjur.abrir_gerador_teses(npjur_real)
                    try:
                        caminho_word = npjur.gerar_e_baixar_word(
                            page_teses, proc.nome_peticao, prefixo_arquivo=proc.npjur
                        )
                    finally:
                        try:
                            page_teses.close()
                        except Exception:
                            pass
                    estado.marcar(proc.npjur, "word", str(caminho_word))
                else:
                    caminho_word = Path(estado.info(proc.npjur, "word")["info"]) \
                        if estado.feita(proc.npjur, "word") else None

                # --- pdf ---
                etapa_atual = "pdf"
                if roda("pdf") and not estado.feita(proc.npjur, "pdf"):
                    destino_pdf = pasta_pdfs / nome_pdf(cnj, proc.npjur)
                    # dilacao de prazo: troca so o pedido da peca (col D/C)
                    _txt_peca = f"{proc.nome_peticao} {proc.tipo_acompanhamento}".upper()
                    eh_dilacao = "DILA" in _txt_peca and "PRAZO" in _txt_peca
                    # peticao livre (col C == "PETIÇÃO"): col L e o pedido,
                    # nao o endereco
                    word_para_pdf(
                        caminho_word, destino_pdf,
                        endereco=None if proc.eh_peticao_livre else (proc.endereco or None),
                        adaptar_dilacao=eh_dilacao and not proc.eh_peticao_livre,
                        pedido_livre=proc.endereco if proc.eh_peticao_livre else None,
                    )
                    estado.marcar(proc.npjur, "pdf", str(destino_pdf))
                caminho_pdf = Path(estado.info(proc.npjur, "pdf")["info"]) \
                    if estado.feita(proc.npjur, "pdf") else None

                # --- anexado (NPJUR Documentos Anexos > NOVO DOCUMENTO) ---
                etapa_atual = "anexado"
                if roda("anexado") and not estado.feita(proc.npjur, "anexado"):
                    observacao_anexo = (proc.obs_peticao_livre or None) if proc.eh_peticao_livre \
                        else (proc.endereco or None)
                    seq_doc = npjur.anexar_documento(
                        npjur_real, caminho_pdf, tipo_documento=proc.tipo_acompanhamento,
                        observacao=observacao_anexo,
                    )
                    # guarda a IDENTIDADE da peca anexada: a DESCRICAO (caso o
                    # col C mude entre rodadas — 19/06, 1351493) E o `seq` (NN do
                    # nome {npjur}_NN), que e o identificador UNICO da peca. No
                    # peticionamento o robo marca EXATAMENTE essa peca pelo seq,
                    # sem depender de descricao/data (25/06, 1493904: o doc estava
                    # anexado mas o match por descricao+data nao o achava).
                    estado.marcar(proc.npjur, "anexado",
                                  {"descricao": proc.tipo_acompanhamento, "seq": seq_doc})

                # --- acompanhamento ---
                # Pedido do Lucas (22/06): se o acompanhamento falhar (ex.:
                # "Observação obrigatória >=100 caracteres" ou falha silenciosa
                # do NPJUR), NAO aborta o processo — registra o aviso, NAO marca
                # a etapa (pra lancar manual depois) e segue pro protocolo. O
                # acompanhamento e cadastro interno; o que importa pro tribunal
                # e o protocolo no Loy.
                etapa_atual = "acompanhamento"
                if roda("acompanhamento") and not estado.feita(proc.npjur, "acompanhamento"):
                    observacao_acomp = (proc.obs_peticao_livre or None) if proc.eh_peticao_livre \
                        else (proc.endereco or None)
                    try:
                        npjur.lancar_acompanhamento(
                            npjur_real, id_juprocesso, proc.tipo_acompanhamento,
                            observacao=observacao_acomp,
                        )
                        estado.marcar(proc.npjur, "acompanhamento")
                    except KeyboardInterrupt:
                        raise
                    except Exception as e_acomp:
                        console.print(
                            f"[yellow]⚠ Acompanhamento do NPJUR {proc.npjur} falhou "
                            f"(seguindo pro protocolo mesmo assim — lancar manual depois):[/yellow] {e_acomp}"
                        )
                        estado.registrar_erro(proc.npjur, "acompanhamento", str(e_acomp))

                # --- peticionado: envio 2ADV + protocolo REAL no Loy ---
                #     (fluxo completo mapeado 11/06 — o envio 2ADV sozinho NAO
                #      protocola; sem o Loy o NPJUR deixa concluir sem peticao!)
                etapa_atual = "peticionado"
                if roda("peticionado") and not estado.feita(proc.npjur, "peticionado"):
                    tipo_pet = proc.tipo_peticao_peticionamento or proc.tipo_acompanhamento
                    # data em que o doc foi ANEXADO (pode ser de outro dia, se o
                    # robo retomou): o doc a protocolar tem ESSA data, nao a de
                    # hoje. Vem do timestamp da etapa 'anexado' no estado.
                    _info_anx = estado.info(proc.npjur, "anexado")
                    data_anexo = None
                    desc_anexada = proc.tipo_acompanhamento
                    seq_anexo = None
                    if _info_anx and _info_anx.get("quando"):
                        data_anexo = "/".join(reversed(_info_anx["quando"][:10].split("-")))
                    # info do anexo: hoje e um dict {descricao, seq}; entradas
                    # LEGADAS guardavam so a string da descricao. O `seq` (NN) e
                    # o identificador UNICO da peca — com ele o peticionamento
                    # marca EXATAMENTE a peca anexada, sem depender de data.
                    _raw = _info_anx.get("info") if _info_anx else None
                    if isinstance(_raw, dict):
                        desc_anexada = _raw.get("descricao") or desc_anexada
                        seq_anexo = _raw.get("seq")
                    elif _raw:
                        desc_anexada = _raw
                    docs_page = npjur.preparar_peticionamento(
                        npjur_real, id_agenda, tipo_pet, desc_anexada,
                        data_doc=data_anexo, seq_doc=seq_anexo,
                    )
                    loy_page = None
                    try:
                        npjur.enviar_peticionamento(docs_page, desc_anexada,
                                                    data_doc=data_anexo,
                                                    seq_doc=seq_anexo)
                        loy_page = npjur.abrir_loy(docs_page)
                        npjur.preencher_loy(
                            loy_page,
                            instancia=proc.instancia or "1º Grau",
                            categoria=proc.categoria_peticionamento,
                            tipo_arquivo=proc.tipo_arquivo_peticionamento,
                            envolvimento=proc.envolvimento or "Solicitante",
                            tipo_peticao=proc.tipo_peticao_estados,
                        )
                        if confirmar_protocolo:
                            pausar(
                                "CONFIRA a tela do Loy (documento, tipo, advogado). "
                                "ENTER = PROTOCOLAR | Ctrl+C = abortar"
                            )
                        npjur.protocolar_loy(loy_page)
                        # marca IMEDIATAMENTE — se cair depois daqui, nunca reprotocola
                        estado.marcar(proc.npjur, "peticionado")
                    finally:
                        for pg in (loy_page, docs_page):
                            try:
                                if pg:
                                    pg.close()
                            except Exception:
                                pass

                # --- concluido (cumprir prazo na fila) ---
                etapa_atual = "concluido"
                if roda("concluido") and not estado.feita(proc.npjur, "concluido"):
                    npjur.cumprir_prazo(npjur_real, id_agenda, proc.nome_peticao)
                    estado.marcar(proc.npjur, "concluido")

                ok += 1
                console.print(f"[green]✓ Processo {proc.npjur} OK ate a etapa '{etapa_final}'[/green]")

            except KeyboardInterrupt:
                raise
            except Exception as e:
                falhas += 1
                tipo = "CALIBRACAO" if isinstance(e, CalibracaoNecessaria) else "ERRO"
                console.print(f"[red]✗ {tipo} na etapa '{etapa_atual}' do NPJUR {proc.npjur}:[/red] {e}")
                dumps = dump_contexto(nav.context, pasta_logs, f"{proc.npjur}_{etapa_atual}")
                if dumps:
                    console.print(f"[yellow]   Raio-X da tela salvo em:[/yellow]")
                    for d in dumps:
                        console.print(f"     {d}")
                    console.print("[yellow]   → Cola o conteudo desse(s) .txt no Claude pra ajustar.[/yellow]")
                estado.registrar_erro(
                    proc.npjur, etapa_atual, str(e),
                    dump="; ".join(str(d) for d in dumps) if dumps else None,
                )
                # fecha popups orfaos pra nao poluir o proximo processo
                for pg in list(nav.context.pages):
                    if pg is not nav.npjur:
                        try:
                            pg.close()
                        except Exception:
                            pass
                console.print("[yellow]Pulando para o proximo processo.[/yellow]")

            if modo == "passo_a_passo" and i < len(processos):
                pausar(f"Processo {i} finalizado. ENTER pro proximo, Ctrl+C pra parar.")

        # ----- 4. Relatorio -----
        relatorio = estado.gerar_relatorio_csv(pasta_logs, [p.npjur for p in processos])
        console.print(Panel.fit(
            f"[bold]Resultado:[/bold] {ok} ok, {falhas} com falha\n"
            f"Relatorio: {relatorio}"
        ))
        pausar("ENTER pra fechar o navegador.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrompido pelo usuario.[/yellow]")
        sys.exit(0)
