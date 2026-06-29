# RESUMO GERAL — Robô NPJUR (consolidado 19/06/2026)

> Handoff completo e enxuto pra continuar o projeto em chat novo. Estado FINAL (não o histórico de tentativas). O CONTEXTO_SESSAO.md tem a cronologia detalhada se precisar de algum "porquê".
> **ANTES DE MEXER:** ler este arquivo inteiro. Há 1 PENDÊNCIA CRÍTICA EM ABERTO (1351493 — ver no fim). Tudo compila; nada pela metade no código.

## O que é
Robô que automatiza o peticionamento de processos no sistema **NPJUR** (Paschoalotto) + **Loy** (app de protocolo no tribunal). Python + Playwright.
- Pasta: `C:\Users\lucas\Desktop\Scudeler Advocacia\robo_npjur`
- Roda com **run.bat** (venv em `.venv`, zero token em runtime).
- Planilha de entrada: `dados/planilha_desentranhamento.xlsm` (config aponta; a antiga `planilha_entrada.xlsm` ainda funciona).
- Checkpoint: `logs/estado.json` — pula etapas já feitas, **nunca reprotocola**.
- Falha em 1 processo → dump automático em `logs/dump_*.txt` e segue pro próximo (try/except no main).
- Relatório por rodada: `logs/relatorio_AAAA-MM-DD_HHMM.csv`.
- **MODO AUTÔNOMO**: config `modo=continuo`, `etapa_final` controla até onde vai, `confirmar_antes_protocolar=false`. Única interação humana: ENTER após login manual no início (e re-login se a sessão cair).

## As 6 etapas
word → pdf → anexado → acompanhamento → **peticionado** (a complexa, com Loy) → concluido. Todas calibradas e testadas. `estado.json` marca cada uma; só marca `peticionado` após confirmação real de protocolo.

## Checkpoint e INTIMAÇÃO NOVA no mesmo processo (16/06)
O NPJUR (coluna A) é **fixo por processo** — uma intimação nova reusa o mesmo NPJUR. Cada diligência da fila tem um **`id_agendamento`** próprio. O `estado.json` guarda o `id_agenda` junto de cada NPJUR (`estado.set_agendamento`):
- **Agendamento atual ≠ salvo** → intimação NOVA → `estado.resetar(npjur)` zera as etapas e reprocessa do zero (protocola a nova). **Sem isso o robô pularia a intimação nova.**
- **Agendamento igual** (retry/resume) → pula as etapas já feitas, nunca reprotocola.
- Já concluído e fora da fila → pula sem erro ("nada a fazer").
- **Legado (entradas antigas sem `id_agenda`)**: só **resume** se já `peticionado` E não `concluido` (mesma diligência pendente — não reprotocolar); em todos os outros casos reprocessa do zero. ⚠️ única entrada legada que resume: **1543901** — se vier intimação nova nela, conferir manual (risco só de NÃO protocolar, nunca de duplicar).

## Limpeza automática dos arquivos gerados (16/06)
`robo/limpeza.py` (`limpar_antigos`, chamado no início do main): retenção rolante das últimas **`saida.manter_rodadas`** rodadas (config, default 5; 0 = desliga). Apaga `dump_*.txt`, `dump_loy_*.txt`, `relatorio_*.csv` e PDFs/Words antigos. **NUNCA apaga** `estado.json` nem PDF/Word de processo ainda incompleto (referenciado no estado). Roda sozinho a cada execução.

## Fluxo do PETICIONADO (a parte complexa)
Métodos em `robo/npjur.py`: `preparar_peticionamento`, `enviar_peticionamento`, `abrir_loy`, `preencher_loy`, `protocolar_loy`. (`robo/loy.py` está MORTO — main não importa.)

1. **NPJUR cadastro.php** → garante CNJ sanitizado. Processo com vários CNJs vinculados: casa pelo `#num_cnj` da página (só dígitos, startsWith), recusa opção "inválido".
2. Seleciona processo + tipo (`#id_jutipoacomp_2adv`=col E, fallback col C) + radio do agendamento → "Enviar para peticionamento".
3. Marca checkbox `docs_anexo[]` do doc certo, identificado por: **(a) a DESCRIÇÃO que foi realmente anexada** (guardada no estado em `estado.marcar('anexado', info=col C)` — robusto a mudar a planilha entre rodadas) **+ (b) a DATA DO ANEXO** (timestamp da etapa 'anexado', não "hoje" — resolve anexar num dia e peticionar noutro). Desempate: maior nº de sequência do nome `NPJUR_NN.ext`. **Sem doc com essa descrição+data → ERRO** (não protocola doc antigo/errado). "Enviar documentos selecionados" → "Enviado 2ADV? SIM". **Isso NÃO protocola no tribunal** — só o Loy protocola. `preparar_peticionamento`/`enviar_peticionamento` recebem `data_doc` (a data do anexo) do main.
4. Botão "Abrir Peticionamento" → abre o **Loy** (app.loylegal.com, Ember).
5. `protocolar_loy`: clica PROTOCOLAR, espera confirmação OBRIGATÓRIA ("Petição enviada para a fila com sucesso" e/ou redirect `retorno_peticionamento.php` "finalizado com sucesso", ~90s). Sem confirmação = erro, não marca etapa. Detecta recusa do Loy ("Preencha o tipo...") e dá msg clara (não duplicou).

## LOY — REGRAS FINAIS (preencher_loy) ⭐ o ponto mais sensível
Loy é Ember: selects são **power-select** (`.ember-power-select-trigger` abre `li.ember-power-select-option`). **Setar valor por JS NÃO funciona — só clique real.**

**Ordem dos campos:** Instância (col F) → Categoria (col H) → **Tipo de Petição (col J)** → **Envolvimento (col K)** → NOVA PETIÇÃO → **TIPO do documento (col I)**. Sigilo/advogado/assinatura já vêm preenchidos.

**PRINCÍPIO MESTRE (vale pra qualquer tribunal/estado, sem hardcode):**
> Cada campo é tratado pelo **ESTADO real dele**, não pelo nome do tribunal:
> - **vazio + habilitado** → preenche (com o valor da coluna).
> - **travado (aria-disabled) ou ausente** → PULA (não erra). Campo travado = o tribunal já fixou o valor certo; ninguém consegue mudar.
> - quem dá a palavra final é o **PROTOCOLAR**: `protocolar_loy` detecta "Preencha o tipo" e recusa sem risco de duplicar.

Helpers: `_loy_triggers` (lista os power-selects; **`i` = índice REAL do DOM**, pra casar com `.nth(i)` do Playwright), `_loy_trigger_travado` (checa aria-disabled), `_loy_escolher(..., pular_se_travado=False)` (retorna False em vez de `raise` quando travado, se pedido).

**Detalhes por campo:**
- **Instância / Categoria**: preenche se vazio; se vier travado inesperado → erro claro (não silencia). Normalização FORTE no match (tira acento/maiúsculas/pontuação): "PETIÇÃO - SIMPLES" casa com "PETIÇÃO SIMPLES". Maiúsc/minúsc na planilha **não importa**.
- **Tipo de Petição (col J)**: SÓ existe em sistemas tipo **ESAJ** (não existe no eproc). Obrigatório. Detectado por presença (trigger vazio cujo contexto fala "Tipo de Petição"). Se existir e col J vazia → erro pedindo preencher col J.
- **Envolvimento (col K, default "Solicitante")**: nos sistemas **ESAJ (TJSP/ESAJ e TJMS)** é obrigatório, mas **habilita em CASCATA só depois do Tipo de Petição** — por isso o passo 3 **espera ~6s ele destravar** antes de decidir. Em **eproc** o campo é ausente/travado → pula.
- **TIPO do documento (col I)**: em **eproc (TJRS etc.)** já vem fixado ("TIPO: PETIÇÃO" no card) e o select fica **travado** (de forma ASSÍNCRONA — o Ember trava uns segundos depois de renderizar). Regra atual: espera 2s estabilizar, **se editável preenche (col I), se travado/ausente pula, e segue pro PROTOCOLAR de qualquer jeito**. NUNCA dá erro fatal nesse campo. (Se o valor da col I não existir no dropdown, `_loy_escolher` ainda lista as opções reais no erro — isso é bom.)

## Sistemas/tribunais MAPEADOS (todos funcionando)
- **TJRS/EPROC** (e eproc em geral): TIPO do doc travado/pré-fixado; sem seção Partes. ✓ testado (1654760 protocolou 16/06).
- **TJSP/ESAJ** e **TJMS** (sistemas ESAJ): têm "Tipo de Petição" (col J, obrigatório) + Envolvimento em cascata (col K). ✓ vários concluíram 16/06.
- **TJAM** (histórico): seção Partes com Envolvimento editável.
- Tribunais novos (PJe etc.): caem nas regras genéricas acima sem precisar de código novo. Se algum trouxer layout diferente → **inspecionar o Loy AO VIVO via Claude in Chrome**, NÃO adivinhar pelo dump (o dump NÃO captura aria-disabled/innerText dos power-selects). Pra inspecionar ao vivo, a aba do Loy precisa estar aberta (o robô fecha no finally — se precisar, segurar a aba aberta no erro só pra depurar).

## Colunas da planilha
A=NPJUR · C=tipo acompanhamento/descrição do doc · D=nome petição (tese) · E=tipo petição (NPJUR) · F=instância (Loy) · H=categoria (Loy) · I=tipo arquivo/TIPO do doc (Loy) · **J=Tipo de Petição (Loy/Dados Básicos, só ESAJ)** · K=envolvimento (Loy, default Solicitante) · L=ENDEREÇO (desentranhamento; vazio = fluxo normal).

⚠️ **Editar a planilha SÓ via Excel COM ou XML** — openpyxl APAGA as validações x14 (dropdowns). E **planilha aberta no Excel trava edição via COM** (igual Word): fechar antes de editar e antes de rodar.

## Petições especiais já implementadas
- **Desentranhamento com novo endereço (col L preenchida)**: conversor edita a peça no Word (COM) antes do PDF — troca "no endereço constante da inicial"→"no endereço a seguir", insere endereço em negrito recuado + parágrafo "05 (cinco) dias...juntada das custas"; preenche Observação do anexo e `#sObservacoes` do acompanhamento com o endereço. docx original intacto, só o PDF sai editado. (Observação do anexo é campo heurístico, ainda não testada ao vivo.)
- **Dilação de prazo** (col D="DILAÇÃO DE PRAZO - ATIVAS" / col C="DILAÇÃO DE PRAZO"): conversor (`_aplicar_pedido_dilacao`) troca só o PEDIDO do modelo cru por "...REQUERER a concessão de prazo suplementar de 10 dias para prosseguimento no feito." Resto intacto. main detecta por "DILA"+"PRAZO". Testado no Word; ainda não rodado ponta a ponta no run.bat.

## Armadilhas conhecidas (todas já tratadas no código)
- **Sessão cai** → `_goto_logado` detecta gelogin.php em toda navegação, pausa pra login manual. Também **retenta em TIMEOUT/erro de rede** no goto (até 4x) — antes um 'Page.goto: Timeout 30000ms' derrubava o processo (19/06, 1344195).
- **NÃO editar a planilha no meio de um lote**: o robô anexa com um valor de col C e, se você mudar, ele procuraria outro. Já está protegido (usa a descrição guardada do anexo), mas evite mesmo assim.
- **NPJUR redireciona Minha Fila → home mesmo logado** → `abrir_minha_fila` confere URL+`#npjur`, re-tenta 3x.
- **Conversão PDF "Open.SaveAs"** quando Lucas está com Word aberto → `conversor.py` usa COM direto (DispatchEx, instância invisível ReadOnly, fecha só a própria).
- **Select Descrição reverte seleção por JS** → seleção nativa (clique + type-ahead + Enter), 3 tentativas, fallback dropdown+Home+ArrowDown até índice exato.
- **Gravar falha silenciosamente** (anexar/acompanhamento) → conferência pós-Gravar obrigatória + pré-checagem idempotente, exigindo **data de HOJE** e escopo correto (NPJUR trunca descrição longa "...REQU..." → match por prefixo ≥15 chars).
- **NPJUR com zero à esquerda** (Excel come o 0: "879948"→"0879948") → `_candidatos_npjur` gera variantes; `npjur_real` (canônico que casou na fila) usado em todas as etapas; estado/relatório na chave da planilha.
- **Alerts com encoding quebrado** ("obrigatÃ³rio") → regexes sem acento (obrigat|necess|favor|selecion).
- **Gerador de Teses pede VARA/FORO** → `vara_padrao` no config ("1ª VARA CÍVEL") como fallback (norm ignora ª/º).
- **`debugger;` anti-automação** → Chrome do robô tem stealth. **`_eval` com retry** pra "Execution context destroyed".
- **Dump útil do Loy** = `dump_loy_*` gerado DENTRO de preencher_loy/protocolar_loy (a aba do Loy fecha no finally, então o dump do CSV é só a Minha Fila).
- **Relatório CSV**: a coluna `ultimo_erro` mostra o último erro do histórico **mesmo após sucesso** (`concluido;SIM`) — conferir o timestamp do dump; se for de rodada anterior, é histórico, não erro novo.

## 🔴 PENDÊNCIA CRÍTICA EM ABERTO — 1351493 (TJSP, CNJ 1001091-80.2024.8.26.0095)
NÃO resolvido na sessão de 19/06. Sintoma: no peticionamento, erro "Nenhum documento '...REQUERIDO >3' criado ... na lista". Investigado a fundo nos dumps:
- A **lista de documentos do peticionamento mostra só os 6 docs originais de 2024** (CONTRATO/GRAVAME/SEFAZ...); o doc de desentranhamento que o robô marcou como 'anexado' hoje **NÃO aparece na lista** (zero "REQUERIDO", zero data 2026 no dump pag2). Esse é o bloqueio real.
- Secundário: o col C foi **mudado entre as rodadas do dia** (13:37 "REQUERIDO 1" → 16:11 ">3") — já mitigado no código (usa a descrição guardada do anexo), mas registrar.
- Já feito: **etapas do 1351493 ZERADAS** no estado.json (nunca protocolou, é seguro) pra reprocessar limpo.
- **PRÓXIMO PASSO:** rodar o 1351493 sozinho, SEM editar a planilha no meio. Se o doc anexado **ainda não aparecer** na lista do peticionamento → é problema fundo do anexo→peticionamento desse processo (o anexo não entra na lista de docs peticionáveis) → **INSPECIONAR AO VIVO (Claude in Chrome)**: comparar com um processo que funciona, ver em que lista o anexar coloca o doc vs. o que a tela de peticionamento lê. NÃO adivinhar pelo dump.

## Outras pendências
- **1353564** (dilação) JÁ protocolado — NÃO re-rodar (risco de duplicar). Acompanhamento/anexo provavelmente faltam; lançar manual.
- Dilação ainda não rodada ponta a ponta no run.bat.
- Reaplicar col H="PETIÇÃO SIMPLES" em 1407189/1477743 se forem rodar (TJAM).
- **1543901** (legado peticionado-sem-concluido): ver nota do checkpoint.

## Sobre o Lucas
Advogado em Bauru. Direto, **sem emoji**, **MUITO sensível a custo de token**. Não pedir autorização pra prosseguir (exceto protocolo judicial real). Sempre **ler este arquivo (ou CONTEXTO_SESSAO.md) antes de mexer**. Quando um layout de tribunal novo der erro, **inspecionar ao vivo, não adivinhar**.
