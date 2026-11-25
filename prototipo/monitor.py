#!/usr/bin/env python3
"""Monitor de longa duração (teste de 23 h) — Câmara e Senado.

Mede três coisas:
  1. Quando cada votação nominal aparece na API (aparicoes.csv) e se os votos
     chegam completos ou aos poucos (mudancas.csv, tipo=parcial).
  2. Se votos já publicados mudam depois — "erratas" (mudancas.csv, tipo=errata).
     A cada --recheck segundos, rebaixa os votos dos últimos --dias dias e compara.
  3. O custo de cada ciclo: tempo, CPU, nº de requisições e bytes (ciclos.csv),
     para simular intervalos de 30 s, 2, 5 e 10 min.

O estado (votos já vistos) fica em estado.json, então dá para reiniciar sem
perder o histórico.

Uso: python3 monitor.py --intervalo 30 --recheck 600 --dias 7 --duracao 82800
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import coletor as c

BRT = dt.timezone(dt.timedelta(hours=-3))
DIR = c.SAIDA / "monitor23h"


def agora_utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Log:
    def __init__(self, nome, cab):
        p = DIR / nome
        novo = not p.exists()
        self.f = open(p, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if novo or p.stat().st_size == 0:
            self.w.writerow(cab)
            self.f.flush()

    def __call__(self, *linha):
        self.w.writerow(linha)
        self.f.flush()


def camara_nominais(data_ini: str, data_fim_incl: str) -> list[dict]:
    """Votações do Plenário com placar na descrição, entre duas datas (inclusive)."""
    fim = (dt.date.fromisoformat(data_fim_incl) + dt.timedelta(days=1)).isoformat()
    out, pagina = [], 1
    while True:
        q = c.urllib.parse.urlencode({"idOrgao": c.ID_PLENARIO_CAMARA, "dataInicio": data_ini, "dataFim": fim,
                                      "itens": 100, "pagina": pagina})
        d = c.get_json(f"{c.CAMARA}/votacoes?{q}", "camara", "listagem")
        out += d.get("dados", [])
        if not any(l["rel"] == "next" for l in d.get("links", [])):
            break
        pagina += 1
    return [v for v in out if c.PADRAO_NOMINAL.search(v.get("descricao") or "")]


def camara_votos(ids: list[str], paralelo=8) -> dict[str, dict]:
    def um(i):
        d = c.get_json(f"{c.CAMARA}/votacoes/{i}/votos", "camara", "votos").get("dados", [])
        return i, {f"{x['deputado_']['id']}|{x['deputado_']['nome']}": x.get("tipoVoto") for x in d}
    with ThreadPoolExecutor(paralelo) as ex:
        return dict(ex.map(um, ids))


def senado(data_ini: str, data_fim: str) -> list[dict]:
    q = c.urllib.parse.urlencode({"dataInicio": data_ini, "dataFim": data_fim, "_t": time.time_ns()})
    return c.get_json(f"{c.SENADO}/votacao?{q}", "senado", "listagem_e_votos")


def status_sessoes(hoje: str) -> dict:
    """Situação da sessão deliberativa do Plenário em cada Casa: 'em andamento', 'prevista',
    'encerrada' ou 'sem sessão'. É o sinal para trocar o intervalo entre 30 s e 10 min."""
    out = {}
    try:
        q = c.urllib.parse.urlencode({"idOrgao": c.ID_PLENARIO_CAMARA, "dataInicio": hoje, "dataFim": hoje})
        evs = c.get_json(f"{c.CAMARA}/eventos?{q}", "camara", "status").get("dados", [])
        delib = [e for e in evs if "Deliberativa" in (e.get("descricaoTipo") or "")
                 and "Não Deliberativa" not in (e.get("descricaoTipo") or "")]
        sit = [(e.get("situacao") or "").lower() for e in delib]
        out["camara"] = ("em andamento" if any("andamento" in x for x in sit) else
                         "prevista" if any(x in ("convocada", "agendada") for x in sit) else
                         "encerrada" if sit else "sem sessão")
        out["camara_det"] = "; ".join(f"{e['id']} {e.get('dataHoraInicio')} {e.get('situacao')}" for e in delib)
    except Exception as e:
        out["camara"], out["camara_det"] = "erro", str(e)[:100]
    try:
        d = c.get_json(f"{c.SENADO}/plenario/agenda/dia/{hoje.replace('-', '')}?_t={time.time_ns()}",
                       "senado", "status")
        ss = (((d.get("AgendaPlenario") or {}).get("Sessoes") or {}).get("Sessao")) or []
        ss = ss if isinstance(ss, list) else [ss]
        delib = [x for x in ss if "DELIBERATIVA" in (x.get("TipoSessao") or "").upper()
                 and "NÃO DELIBERATIVA" not in (x.get("TipoSessao") or "").upper()]
        sit = [(x.get("SituacaoSessao") or "").lower() for x in delib]
        out["senado"] = ("em andamento" if any("andamento" in x for x in sit) else
                         "encerrada" if any("encerrada" in x for x in sit) else
                         "prevista" if sit else "sem sessão")
        out["senado_det"] = "; ".join(f"{x.get('CodigoSessao')} {x.get('Hora')} {x.get('SituacaoSessao')}" for x in delib)
    except Exception as e:
        out["senado"], out["senado_det"] = "erro", str(e)[:100]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervalo", type=int, default=30)
    ap.add_argument("--recheck", type=int, default=600)
    ap.add_argument("--dias", type=int, default=7)
    ap.add_argument("--duracao", type=int, default=23 * 3600)
    ap.add_argument("--pasta", default="monitor23h", help="subpasta de saida/ para este teste")
    a = ap.parse_args()

    global DIR
    DIR = c.SAIDA / a.pasta

    DIR.mkdir(parents=True, exist_ok=True)
    pest = DIR / "estado.json"
    est = json.loads(pest.read_text()) if pest.exists() else {"votos": {}, "meta": {}, "inicio": agora_utc()}
    if "fim" not in est:
        est["fim"] = time.time() + a.duracao
    pest.write_text(json.dumps(est))

    ciclos = Log("ciclos.csv", ["inicio_utc", "tipo", "segundos", "cpu_s", "requisicoes", "bytes", "erros",
                                "votacoes_conhecidas"])
    aparic = Log("aparicoes.csv", ["casa", "id", "materia", "data_sessao", "registro_oficial_brt", "visto_em_utc",
                                   "atraso_s", "n_votos", "descricao"])
    mud = Log("mudancas.csv", ["detectado_em_utc", "tipo", "casa", "id", "materia", "parlamentar", "antes", "depois",
                               "minutos_desde_aparicao"])

    sess = Log("sessoes.csv", ["visto_em_utc", "camara", "senado", "camara_detalhe", "senado_detalhe"])
    ultimo_status = {}

    def registrar(casa, vid, materia, votos_novos: dict, meta: dict, recheck: bool):
        chave = f"{casa}:{vid}"
        antigos = est["votos"].get(chave)
        ts = agora_utc()
        if antigos is None:
            est["votos"][chave] = votos_novos
            est["meta"][chave] = {"visto": time.time(), "materia": materia}
            if not recheck:  # aparição durante o monitor (não estava lá na carga inicial)
                atraso = ""
                if meta.get("registro"):
                    reg = dt.datetime.fromisoformat(meta["registro"]).replace(tzinfo=BRT)
                    atraso = round((dt.datetime.now(dt.timezone.utc) - reg).total_seconds())
                aparic(casa, vid, materia, meta.get("data"), meta.get("registro"), ts, atraso, len(votos_novos),
                       (meta.get("descricao") or "")[:200])
                print(f"NOVA {chave} {materia} atraso={atraso}s votos={len(votos_novos)}", file=sys.stderr)
            return
        if antigos == votos_novos:
            return
        mins = round((time.time() - est["meta"][chave]["visto"]) / 60, 1)
        tipo = "parcial" if mins < 60 else "errata"
        for p in sorted(set(antigos) | set(votos_novos)):
            if antigos.get(p) != votos_novos.get(p):
                mud(ts, tipo, casa, vid, materia, p, antigos.get(p, "(ausente)"), votos_novos.get(p, "(ausente)"), mins)
        print(f"MUDOU {chave} tipo={tipo}", file=sys.stderr)
        est["votos"][chave] = votos_novos

    def ciclo(recheck: bool):
        hoje = dt.datetime.now(BRT).date()
        ini = (hoje - dt.timedelta(days=a.dias)).isoformat() if recheck else hoje.isoformat()
        fim = hoje.isoformat()
        erros = 0
        # Câmara
        try:
            vs = camara_nominais(ini, fim)
            alvo = [v for v in vs if recheck or f"Câmara:{v['id']}" not in est["votos"]
                    or time.time() - est["meta"][f"Câmara:{v['id']}"]["visto"] < 3600]
            vts = camara_votos([v["id"] for v in alvo])
            for v in alvo:
                if vts[v["id"]]:
                    registrar("Câmara", v["id"], v.get("proposicaoObjeto") or "", vts[v["id"]],
                              {"registro": v.get("dataHoraRegistro"), "data": v.get("data"),
                               "descricao": v.get("descricao")}, recheck)
        except Exception as e:
            erros += 1
            print("erro camara:", e, file=sys.stderr)
        # Senado
        try:
            for v in senado(ini, fim):
                votos = {f"{x['codigoParlamentar']}|{x['nomeParlamentar']}": x.get("siglaVotoParlamentar")
                         for x in v.get("votos", [])}
                registrar("Senado", str(v["codigoSessaoVotacao"]), v.get("identificacao") or "", votos,
                          {"data": v.get("dataSessao"), "descricao": v.get("descricaoVotacao")}, recheck)
        except Exception as e:
            erros += 1
            print("erro senado:", e, file=sys.stderr)
        return erros

    # carga inicial: últimos N dias, sem registrar como "aparição"
    if not est["votos"]:
        ciclo(recheck=True)
        pest.write_text(json.dumps(est))
        print(f"carga inicial: {len(est['votos'])} votações", file=sys.stderr)

    ultimo_recheck = time.time()
    while time.time() < est["fim"]:
        recheck = time.time() - ultimo_recheck >= a.recheck
        n0, t0, cpu0 = len(c.REQS), time.perf_counter(), time.process_time()
        inicio = agora_utc()
        erros = ciclo(recheck)
        st = status_sessoes(dt.datetime.now(BRT).date().isoformat())
        if (st.get("camara"), st.get("senado")) != (ultimo_status.get("camara"), ultimo_status.get("senado")):
            sess(agora_utc(), st.get("camara"), st.get("senado"), st.get("camara_det"), st.get("senado_det"))
            print(f"SESSAO camara={st.get('camara')} senado={st.get('senado')}", file=sys.stderr)
            ultimo_status.update(st)
        reqs = c.REQS[n0:]
        seg = round(time.perf_counter() - t0, 3)
        ciclos(inicio, "recheck" if recheck else "base", seg, round(time.process_time() - cpu0, 3), len(reqs),
               sum(r["bytes"] for r in reqs), erros, len(est["votos"]))
        del c.REQS[:]  # não acumular memória por 23 h
        if recheck:
            ultimo_recheck = time.time()
        pest.write_text(json.dumps(est))
        time.sleep(max(0, a.intervalo - seg))
    print("fim do monitor", file=sys.stderr)


if __name__ == "__main__":
    main()
