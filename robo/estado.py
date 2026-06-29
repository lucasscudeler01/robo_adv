"""
Checkpoint de execucao + relatorio.

PROBLEMA QUE RESOLVE:
  Com 30-60 processos/dia, se o robo falhar no 20o processo e voce rodar de
  novo, ele NAO pode refazer o que ja foi feito — principalmente o
  peticionamento no Loy Legal (protocolar 2x a mesma peticao e o pior erro
  possivel).

COMO FUNCIONA:
  Cada etapa concluida de cada NPJUR e gravada em logs/estado.json na hora.
  Na proxima execucao, etapas ja feitas sao puladas automaticamente.

ETAPAS (em ordem):
  word          -> Word baixado do Gerador de Teses
  pdf           -> convertido pra PDF
  anexado       -> PDF anexado em Documentos Anexos no NPJUR
  acompanhamento-> Acompanhamento Processual registrado
  peticionado   -> protocolado no Loy Legal  (IRREVERSIVEL!)
  concluido     -> prazo cumprido na fila do NPJUR
"""

import csv
import json
import os
import time
from pathlib import Path

# Ordem oficial das etapas do fluxo
ETAPAS = ["word", "pdf", "anexado", "acompanhamento", "peticionado", "concluido"]


class Estado:
    """Persistencia simples em JSON, salva a cada marcacao (crash-safe)."""

    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self.dados: dict = {}
        if self.caminho.exists():
            try:
                self.dados = json.loads(self.caminho.read_text(encoding="utf-8"))
            except Exception:
                # Arquivo corrompido — renomeia pra .bak e comeca limpo
                self.caminho.rename(self.caminho.with_suffix(".json.bak"))
                self.dados = {}

    # ----------------------------------------------------------------- helpers
    def _proc(self, npjur: str) -> dict:
        return self.dados.setdefault(str(npjur), {"etapas": {}, "erros": []})

    def _salvar(self):
        # Escrita atomica: grava num temp e troca, pra nunca corromper o json
        tmp = self.caminho.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.dados, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.caminho)

    # --------------------------------------------------------------------- API
    def feita(self, npjur: str, etapa: str) -> bool:
        return etapa in self._proc(npjur)["etapas"]

    def info(self, npjur: str, etapa: str):
        """Retorna o que foi salvo junto com a etapa (ex: caminho do PDF)."""
        return self._proc(npjur)["etapas"].get(etapa)

    def marcar(self, npjur: str, etapa: str, info=None):
        self._proc(npjur)["etapas"][etapa] = {
            "quando": time.strftime("%Y-%m-%d %H:%M:%S"),
            "info": info,
        }
        self._salvar()

    # --- agendamento (id da diligencia na fila) — distingue INTIMACOES -------
    # O NPJUR e fixo por processo; cada nova intimacao gera uma diligencia nova
    # na fila, com id_agendamento proprio. Guardando esse id junto do estado, o
    # robo sabe quando o que esta na fila e uma INTIMACAO NOVA (id diferente do
    # salvo) — ai reprocessa do zero em vez de pular achando que ja fez.
    def get_agendamento(self, npjur: str):
        return self._proc(npjur).get("id_agenda")

    def set_agendamento(self, npjur: str, id_agenda):
        p = self._proc(npjur)
        if p.get("id_agenda") != str(id_agenda):
            p["id_agenda"] = str(id_agenda)
            self._salvar()

    def resetar(self, npjur: str):
        """Zera as etapas concluidas (preserva o historico de erros). Usado
        quando chega intimacao NOVA pro mesmo NPJUR — o trabalho anterior foi
        de outra diligencia, ja protocolada e concluida."""
        self._proc(npjur)["etapas"] = {}
        self._salvar()

    def registrar_erro(self, npjur: str, etapa: str, erro: str, dump: str | None = None):
        self._proc(npjur)["erros"].append({
            "quando": time.strftime("%Y-%m-%d %H:%M:%S"),
            "etapa": etapa,
            "erro": str(erro)[:500],
            "dump": dump,
        })
        self._salvar()

    def ultima_etapa(self, npjur: str) -> str:
        feitas = self._proc(npjur)["etapas"]
        ultima = "-"
        for e in ETAPAS:
            if e in feitas:
                ultima = e
        return ultima

    # --------------------------------------------------------------- relatorio
    def gerar_relatorio_csv(self, pasta_logs: Path, npjurs_da_rodada: list[str]) -> Path:
        """CSV com o resultado de cada processo da rodada."""
        destino = Path(pasta_logs) / f"relatorio_{time.strftime('%Y-%m-%d_%H%M')}.csv"
        with open(destino, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["NPJUR", "ultima_etapa_concluida", "completo", "ultimo_erro", "dump_para_calibracao"])
            for npjur in npjurs_da_rodada:
                p = self._proc(npjur)
                erros = p["erros"]
                ultimo_erro = erros[-1]["erro"] if erros else ""
                dump = erros[-1].get("dump") or "" if erros else ""
                w.writerow([
                    npjur,
                    self.ultima_etapa(npjur),
                    "SIM" if "concluido" in p["etapas"] else "NAO",
                    ultimo_erro,
                    dump,
                ])
        return destino
