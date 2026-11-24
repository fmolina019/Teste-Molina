#!/usr/bin/env python3
"""Protótipo de coleta de votações nominais — Câmara dos Deputados e Senado Federal.

Só usa a biblioteca padrão do Python (3.9+). Mede o tempo de cada etapa:
listagem, votos, normalização e apresentação (geração de HTML).

Uso:
  python3 coletor.py coletar --casa ambas --data 2026-11-18
  python3 coletor.py bench --data 2026-11-18 --rodadas 5
  python3 coletor.py monitorar --intervalo 30 --duracao 3600

Saídas ficam em ./saida/ (JSON normalizado, HTML, CSV de tempos).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CAMARA = "https://dadosabertos.camara.leg.br/api/v2"
SENADO = "https://legis.senado.leg.br/dadosabertos"
ID_PLENARIO_CAMARA = 180  # órgão "PLEN" na API da Câmara
# Votações nominais da Câmara trazem o placar na descrição ("Sim: 319; Não: 127; ...").
# Filtrar por isso evita chamar /votos em atos simbólicos (178 de 178 em 10/11/2026).
PADRAO_NOMINAL = re.compile(r"Sim:\s*\d+")
UA = "prototipo-votacoes/0.1"
SAIDA = Path(__file__).resolve().parent / "saida"

# Registro de cada requisição HTTP: (casa, etapa, url, status, segundos, bytes)
REQS: list[dict] = []


def get_json(url: str, casa: str, etapa: str, tentativas: int = 3) -> dict | list:
    """GET com retentativa simples e registro de tempo."""
    ultimo_erro = None
    for n in range(tentativas):
        t0 = time.perf_counter()
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                corpo = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            corpo, status, ultimo_erro = b"", e.code, e
        except Exception as e:  # timeout, conexão
            corpo, status, ultimo_erro = b"", 0, e
        seg = time.perf_counter() - t0
        REQS.append({"casa": casa, "etapa": etapa, "url": url, "status": status,
                     "segundos": round(seg, 3), "bytes": len(corpo), "tentativa": n + 1})
        if status == 200:
            return json.loads(corpo) if corpo else {}
        if status == 404:  # Câmara: votação simbólica às vezes devolve 404 em /votos
            return {}
        time.sleep(0.5 * 2 ** n)
    raise RuntimeError(f"falhou: {url} ({ultimo_erro})")


class Cronometro:
    def __init__(self):
        self.etapas: dict[str, float] = {}

    def medir(self, nome):
        crono = self

        class _Ctx:
            def __enter__(self):
                self.t0 = time.perf_counter()

            def __exit__(self, *a):
                crono.etapas[nome] = round(time.perf_counter() - self.t0, 3)

        return _Ctx()


# ---------------------------------------------------------------- Câmara
def camara_listar(data: str, cr: Cronometro) -> list[dict]:
    """Votações do Plenário numa data (paginado, máx. 100 por página).

    Atenção: na API da Câmara o dataFim é EXCLUSIVO (dataInicio=dataFim volta vazio).
    """
    fim = (dt.date.fromisoformat(data) + dt.timedelta(days=1)).isoformat()
    with cr.medir("camara.listagem"):
        out, pagina = [], 1
        while True:
            q = urllib.parse.urlencode({"idOrgao": ID_PLENARIO_CAMARA, "dataInicio": data, "dataFim": fim,
                                        "itens": 100, "pagina": pagina, "ordem": "ASC",
                                        "ordenarPor": "dataHoraRegistro"})
            d = get_json(f"{CAMARA}/votacoes?{q}", "camara", "listagem")
            out += [v for v in d.get("dados", []) if v.get("data") == data]
            if not any(l["rel"] == "next" for l in d.get("links", [])):
                break
            pagina += 1
    return out


_CACHE_VOTOS: dict[str, list] = {}  # votos já completos não mudam; usado no modo monitorar


def camara_votos(votacoes: list[dict], cr: Cronometro, paralelo: int, usar_cache=False) -> dict[str, list]:
    """GET /votacoes/{id}/votos para cada votação, em paralelo."""
    with cr.medir("camara.votos"):
        def um(v):
            if usar_cache and _CACHE_VOTOS.get(v["id"]):
                return v["id"], _CACHE_VOTOS[v["id"]]
            dados = get_json(f"{CAMARA}/votacoes/{v['id']}/votos", "camara", "votos").get("dados", [])
            _CACHE_VOTOS[v["id"]] = dados
            return v["id"], dados
        with ThreadPoolExecutor(max_workers=paralelo) as ex:
            return dict(ex.map(um, votacoes))


def camara_normalizar(votacoes, votos_por_id) -> list[dict]:
    out = []
    for v in votacoes:
        votos = votos_por_id.get(v["id"], [])
        if not votos:  # votação simbólica: sem votos nominais
            continue
        out.append({
            "casa": "Câmara",
            "id": v["id"],
            "data_hora": v.get("dataHoraRegistro"),
            "materia": v.get("proposicaoObjeto") or "",
            "descricao": v.get("descricao") or "",
            "resultado": {1: "Aprovado", 0: "Rejeitado"}.get(v.get("aprovacao"), "—"),
            "votos": [{
                "parlamentar": x["deputado_"]["nome"],
                "partido": x["deputado_"].get("siglaPartido"),
                "uf": x["deputado_"].get("siglaUf"),
                "voto": x.get("tipoVoto"),
            } for x in votos],
        })
    return out


# ---------------------------------------------------------------- Senado
def senado_coletar(data: str, cr: Cronometro, sem_cache: bool = True) -> list[dict]:
    """Uma chamada traz votações + votos nominais do período.

    A API do Senado responde com cache-control max-age=600 (CDN): sem o parâmetro
    extra _t, a resposta pode ter até 10 min de atraso. Ver relatório.
    """
    with cr.medir("senado.listagem_e_votos"):
        params = {"dataInicio": data, "dataFim": data}
        if sem_cache:
            params["_t"] = time.time_ns()
        q = urllib.parse.urlencode(params)
        return get_json(f"{SENADO}/votacao?{q}", "senado", "listagem_e_votos")


def senado_normalizar(votacoes) -> list[dict]:
    res = {"A": "Aprovado", "R": "Rejeitado"}
    out = []
    for v in votacoes:
        out.append({
            "casa": "Senado",
            "id": str(v["codigoSessaoVotacao"]),
            "data_hora": v.get("dataSessao"),
            "materia": v.get("identificacao") or "",
            "descricao": v.get("descricaoVotacao") or "",
            "resultado": res.get(v.get("resultadoVotacao"), v.get("resultadoVotacao") or "—"),
            "votos": [{
                "parlamentar": x.get("nomeParlamentar"),
                "partido": x.get("siglaPartidoParlamentar"),
                "uf": x.get("siglaUFParlamentar"),
                "voto": x.get("siglaVotoParlamentar"),
            } for x in v.get("votos", [])],
        })
    return out


# ---------------------------------------------------------------- comum
def placar(votos):
    c = {}
    for x in votos:
        c[x["voto"]] = c.get(x["voto"], 0) + 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


def render_html(votacoes: list[dict], caminho: Path):
    linhas = []
    for v in votacoes:
        p = placar(v["votos"])
        resumo = " · ".join(f"{html.escape(str(k))}: {n}" for k, n in p.items())
        tabela = "".join(
            f"<tr><td>{html.escape(x['parlamentar'] or '')}</td><td>{html.escape(x['partido'] or '')}</td>"
            f"<td>{html.escape(x['uf'] or '')}</td><td>{html.escape(x['voto'] or '')}</td></tr>"
            for x in sorted(v["votos"], key=lambda x: (x["voto"] or "", x["parlamentar"] or "")))
        linhas.append(
            f"<section><h2>{v['casa']} · {html.escape(v['materia'])} · {v['resultado']}</h2>"
            f"<p>{html.escape(v['descricao'])}</p><p><b>{resumo}</b></p>"
            f"<details><summary>{len(v['votos'])} votos</summary><table>{tabela}</table></details></section>")
    caminho.write_text(
        "<!doctype html><meta charset=utf-8><title>Votações</title>"
        "<style>body{font:14px system-ui;max-width:900px;margin:2em auto}section{border-bottom:1px solid #ccc}"
        "td{padding:2px 8px}</style>" + "".join(linhas), encoding="utf-8")


def rodada(casa: str, data: str, paralelo: int, salvar=True, usar_cache=False,
           so_nominais=False) -> tuple[list[dict], dict]:
    cr = Cronometro()
    t0 = time.perf_counter()
    normal = []
    if casa in ("camara", "ambas"):
        lst = camara_listar(data, cr)
        cr.etapas["camara.qtd_votacoes_listadas"] = len(lst)
        if so_nominais:
            lst = [v for v in lst if PADRAO_NOMINAL.search(v.get("descricao") or "")]
        vts = camara_votos(lst, cr, paralelo, usar_cache)
        with cr.medir("camara.normalizacao"):
            normal += camara_normalizar(lst, vts)
    if casa in ("senado", "ambas"):
        sen = senado_coletar(data, cr)
        with cr.medir("senado.normalizacao"):
            normal += senado_normalizar(sen)
    with cr.medir("apresentacao.html"):
        if salvar:
            SAIDA.mkdir(exist_ok=True)
            render_html(normal, SAIDA / f"votacoes_{data}.html")
            (SAIDA / f"votacoes_{data}.json").write_text(json.dumps(normal, ensure_ascii=False, indent=1),
                                                         encoding="utf-8")
    cr.etapas["total"] = round(time.perf_counter() - t0, 3)
    cr.etapas["votacoes_nominais"] = len(normal)
    cr.etapas["votos"] = sum(len(v["votos"]) for v in normal)
    return normal, cr.etapas


def salvar_reqs(nome):
    SAIDA.mkdir(exist_ok=True)
    with open(SAIDA / nome, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(REQS[0].keys()))
        w.writeheader()
        w.writerows(REQS)


def cmd_coletar(a):
    _, et = rodada(a.casa, a.data, a.paralelo, so_nominais=a.so_nominais)
    print(json.dumps(et, indent=1, ensure_ascii=False))
    salvar_reqs(f"requisicoes_{a.data}.csv")
    print(f"Saída em {SAIDA}")


def cmd_bench(a):
    todas = []
    for i in range(a.rodadas):
        _, et = rodada(a.casa, a.data, a.paralelo, salvar=(i == 0), so_nominais=a.so_nominais)
        todas.append(et)
        print(f"rodada {i + 1}: total {et['total']}s", file=sys.stderr)
    chaves = [k for k in todas[0] if isinstance(todas[0][k], float)]
    resumo = {k: {"mediana": round(statistics.median(t[k] for t in todas), 3),
                  "min": min(t[k] for t in todas), "max": max(t[k] for t in todas)} for k in chaves}
    resumo["_contagens"] = {k: todas[0][k] for k in todas[0] if not isinstance(todas[0][k], float)}
    lat = {}
    for r in REQS:
        lat.setdefault(f"{r['casa']}.{r['etapa']}", []).append(r["segundos"])
    resumo["_latencia_por_requisicao"] = {
        k: {"n": len(v), "mediana": round(statistics.median(v), 3),
            "p95": round(sorted(v)[max(0, int(len(v) * .95) - 1)], 3), "max": max(v)} for k, v in lat.items()}
    print(json.dumps(resumo, indent=1, ensure_ascii=False))
    SAIDA.mkdir(exist_ok=True)
    (SAIDA / f"bench_{a.data}_p{a.paralelo}.json").write_text(json.dumps(resumo, indent=1, ensure_ascii=False))
    salvar_reqs(f"requisicoes_bench_{a.data}_p{a.paralelo}.csv")


def cmd_monitorar(a):
    """Consulta as duas Casas a cada N s e registra quando cada votação nominal aparece.

    Atraso de publicação = momento em que vimos a votação (com votos) − dataHoraRegistro (Câmara).
    O Senado só informa a data da sessão, então registramos apenas o horário em que apareceu.
    """
    vistos: dict[str, dict] = {}
    log = SAIDA / "monitor.csv"
    SAIDA.mkdir(exist_ok=True)
    novo = not log.exists()
    f = open(log, "a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if novo:
        w.writerow(["casa", "id", "materia", "registro_oficial", "visto_em_utc", "atraso_s", "n_votos"])
    fim = time.time() + a.duracao
    while time.time() < fim:
        hoje = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).date().isoformat()
        try:
            normal, et = rodada("ambas", hoje, a.paralelo, salvar=False, usar_cache=True, so_nominais=True)
        except Exception as e:
            print("erro:", e, file=sys.stderr)
            time.sleep(a.intervalo)
            continue
        agora = dt.datetime.now(dt.timezone.utc)
        for v in normal:
            chave = f"{v['casa']}:{v['id']}"
            if chave in vistos:
                continue
            atraso = ""
            if v["casa"] == "Câmara" and v["data_hora"]:
                reg = dt.datetime.fromisoformat(v["data_hora"]).replace(tzinfo=dt.timezone(dt.timedelta(hours=-3)))
                atraso = round((agora - reg).total_seconds())
            vistos[chave] = v
            w.writerow([v["casa"], v["id"], v["materia"], v["data_hora"], agora.isoformat(timespec="seconds"),
                        atraso, len(v["votos"])])
            f.flush()
            print(f"NOVA {chave} {v['materia']} atraso={atraso}s", file=sys.stderr)
        print(f"{agora:%H:%M:%S} ciclo {et['total']}s, {len(vistos)} votações conhecidas", file=sys.stderr)
        time.sleep(max(0, a.intervalo - et["total"]))
    f.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for nome in ("coletar", "bench"):
        s = sub.add_parser(nome)
        s.add_argument("--casa", choices=["camara", "senado", "ambas"], default="ambas")
        s.add_argument("--data", required=True, help="AAAA-MM-DD")
        s.add_argument("--paralelo", type=int, default=8, help="requisições simultâneas na Câmara")
        s.add_argument("--so-nominais", action="store_true",
                       help="Câmara: só chama /votos se a descrição tiver o placar ('Sim: N')")
        if nome == "bench":
            s.add_argument("--rodadas", type=int, default=5)
    m = sub.add_parser("monitorar")
    m.add_argument("--intervalo", type=int, default=30)
    m.add_argument("--duracao", type=int, default=3600)
    m.add_argument("--paralelo", type=int, default=8)
    a = p.parse_args()
    {"coletar": cmd_coletar, "bench": cmd_bench, "monitorar": cmd_monitorar}[a.cmd](a)


if __name__ == "__main__":
    main()
