# Robô de Manifestação NPJUR — versão completa

O robô agora cobre o fluxo inteiro: baixar Word → converter PDF → anexar no
NPJUR → acompanhamento processual → protocolar no Loy Legal → cumprir prazo.

Em runtime ele **não gasta nenhum token** — é Python puro rodando local.

## 1. Instalação (já feita)

O `setup.bat` já foi rodado (existe `.venv` na pasta). Se um dia precisar
reinstalar, é só rodar ele de novo.

## 2. Preparar a planilha

Coloque a planilha do dia (formato GIOVANA_PlanilhaLancamento) em:

```
dados/planilha_entrada.xlsm
```

Aba `PREENCHER INFORMAÇÕES`, mesmas 11 colunas do escritório.

## 3. Rodar

Dois cliques em **`run.bat`**. Na primeira execução, faça login no NPJUR na
janela do Chrome que abrir e aperte ENTER no terminal. O login fica salvo
no perfil pras próximas vezes.

## 4. TESTE GRADUAL — leia isto antes de rodar

No `config.yaml` existe a chave **`etapa_final`**. Ela controla até onde o
robô vai em cada processo. **Teste nesta ordem**, com 1-2 processos:

| etapa_final | o que acontece | risco |
|---|---|---|
| `pdf` | baixa Word + converte PDF | nenhum (padrão atual) |
| `anexado` | + anexa o PDF no NPJUR | interno, dá pra excluir |
| `acompanhamento` | + registra acompanhamento | interno |
| `peticionado` | + protocola no Loy Legal | **IRREVERSÍVEL** |
| `concluido` | + cumpre o prazo na fila | fluxo completo |

Só avance pra próxima etapa quando a anterior estiver redonda.

`confirmar_antes_protocolar: true` faz o robô pausar com a tela do Loy
preenchida pra você conferir antes de clicar Protocolar. Deixe `true` até
confiar 100%.

## 5. Checkpoint — pode rodar de novo sem medo

Cada etapa concluída de cada NPJUR fica gravada em `logs/estado.json`.
Se o robô parar no meio (erro, luz, Ctrl+C), rode de novo: ele **pula tudo
que já foi feito**. Ele nunca protocola a mesma petição duas vezes.

Pra reprocessar um NPJUR do zero de propósito, apague a entrada dele no
`logs/estado.json` (ou o arquivo inteiro pra zerar tudo).

## 6. Quando algo falhar — calibração barata

As telas de anexar documento, acompanhamento, Loy e cumprir prazo ainda não
foram calibradas com o DOM real. Na primeira vez que cada uma rodar, pode
falhar. Quando falhar:

1. O robô salva automaticamente um arquivo `logs/dump_<npjur>_<etapa>_*.txt`
   com o raio-X da tela (todos os campos, botões e funções JS).
2. **Cola o conteúdo desse .txt no Claude** (não precisa de print — texto
   gasta muito menos token que imagem).
3. O Claude ajusta o seletor com precisão em 1 iteração.

No fim de cada execução sai um `logs/relatorio_*.csv` com o resultado de
cada processo (até onde chegou, qual erro, qual dump).

## 7. Conversão Word → PDF

Usa o Word instalado no Windows. **Feche dialogs abertos do Word** antes de
rodar (um "Deseja salvar?" pendente trava a conversão). O PDF é nomeado com
o número CNJ do processo (extraído da fila do NPJUR).

## 8. Arquivos

- `config.yaml` — etapa_final, modo, URLs, tempos. É o único que você edita.
- `dados/planilha_entrada.xlsm` — planilha do dia
- `pdfs/` — Words baixados e PDFs convertidos
- `logs/estado.json` — checkpoint (não apague sem motivo)
- `logs/relatorio_*.csv` — resultado de cada rodada
- `logs/dump_*.txt` — raio-X de telas com falha (pra colar no Claude)
- `.chrome_perfil_robo/` — login salvo do Chrome (não apague!)
