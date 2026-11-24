#!/usr/bin/env python3
"""Gera, para cada votação nominal, as duas listas que alimentam a etapa de textos:

  1. lista_nominal  — todos os parlamentares da Casa, com o voto de cada um
                      (quem não votou aparece como "Ausente" / código do Senado).
  2. lista_por_partido — os mesmos votos agrupados por partido, com os nomes,
                      a contagem e a orientação do partido quando houver.

As duas trazem a "pauta" (matéria, ementa, o que foi votado, resultado, data).
Antes de gravar, confere a lista contra o placar oficial e registra o resultado
em "validacao". Votação secreta sai marcada (secreta=true): há presença, não voto.

Uso:
  python3 listas.py --data 2026-11-23            # as duas Casas
  python3 listas.py --data 2026-11-18 --casa senado

Saída: saida/listas/<casa>/<data>/<id>.json, <id>_nominal.csv e <id>_partidos.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import coletor as c

OUT = c.SAIDA / "listas"
TOTAL_CAMARA, TOTAL_SENADO = 513, 81
# A API da Câmara só lista quem registrou voto; não informa presença. Quem estava no Plenário
# sem votar e quem faltou ficam iguais, por isso o rótulo é "Não votou", e não "Ausente".
NAO_VOTOU = "Não votou"

# Câmara: tipoVoto → rótulo. "Artigo 17" = Presidente da Câmara, que não vota.
VOTO_CAMARA = {"Sim": "Sim", "Não": "Não", "Abstenção": "Abstenção", "Obstrução": "Obstrução",
               "Artigo 17": "Presidente (art. 17 RICD)"}
# Senado: nomes de partido na orientação → sigla usada nos votos
ALIAS_SENADO = {"PROGRESSISTAS": "PP", "PODEMOS": "PODE", "UNIAO BRASIL": "UNIÃO"}


def sem_acento(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", s or "") if unicodedata.category(ch) != "Mn").upper()


def placar_do_texto(txt: str) -> dict:
    """'Sim: 319; Não: 127; Abstenção: 1; Total: 447' → dict (Câmara e informe do Senado)."""
    return {k: int(v) for k, v in re.findall(r"(Sim|Não|Abstenção|Obstrução|Total):\s*(\d+)", txt or "")}


def por_partido(nominal: list[dict], orientacoes: dict[str, str], secreta: bool) -> list[dict]:
    grupos = defaultdict(list)
    for p in nominal:
        grupos[p["partido"] or "Sem partido"].append(p)
    out = []
    for sigla in sorted(grupos, key=lambda s: (-len(grupos[s]), s)):
        membros = grupos[sigla]
        por_voto = defaultdict(list)
        for p in sorted(membros, key=lambda p: p["nome"]):
            por_voto[p["voto"]].append({"nome": p["nome"], "uf": p["uf"], "id": p["id"]})
        out.append({
            "partido": sigla,
            "bancada_na_votacao": len(membros),
            "orientacao": orientacoes.get(sigla),
            "contagem": dict(Counter(p["voto"] for p in membros).most_common()),
            "votos": dict(por_voto),
            "secreta": secreta,
        })
    return out


def validar(nominal, total_casa, oficial: dict, secreta: bool) -> dict:
    """Confere a lista e decompõe o total da Casa.

    Na Câmara, o "Total" oficial do placar = Sim + Não + Abstenção. Obstrução, o presidente da
    sessão e quem não votou ficam fora dele. A decomposição deixa a conta explícita:
    total_oficial + obstrução + presidente + sem voto = parlamentares em exercício.
    """
    ids = [p["id"] for p in nominal]
    cont = Counter(p["voto"] for p in nominal)
    sim, nao, abst = cont.get("Sim", 0), cont.get("Não", 0), cont.get("Abstenção", 0)
    presid = sum(n for k, n in cont.items() if k and k.startswith("Presidente"))
    obstr = cont.get("Obstrução", 0)
    secretos = cont.get("Votou (secreto)", 0)
    sem_voto = len(nominal) - sim - nao - abst - obstr - presid - secretos
    comp = {
        "em_exercicio": len(nominal),
        "sim": sim, "nao": nao, "abstencao": abst,
        "total_placar (sim+nao+abstencao)": sim + nao + abst,
        "obstrucao": obstr,
        "presidente_nao_vota": presid,
        "sem_voto_registrado": sem_voto,
    }
    if secreta:
        comp["votou_secreto"] = secretos
    v = {
        "total_parlamentares": len(nominal),
        "total_esperado": total_casa,
        "sem_duplicados": len(ids) == len(set(ids)),
        "placar_oficial": oficial or None,
        "composicao": comp,
    }
    checks = [v["sem_duplicados"], len(nominal) == total_casa]
    if oficial and secreta and oficial.get("Total") is not None:
        checks.append(oficial["Total"] == secretos)  # em votação secreta, Total = quem votou
    if oficial and not secreta:
        for k, val in (("Sim", sim), ("Não", nao), ("Abstenção", abst), ("Total", sim + nao + abst)):
            if oficial.get(k) is not None:
                checks.append(oficial[k] == val)
    v["ok"] = all(checks)
    if secreta:
        v["observacao"] = "votação secreta: só presença; não gerar 'como votou'"
    return v


# ------------------------------------------------------------------ Câmara
def camara(data: str) -> list[dict]:
    cr = c.Cronometro()
    vs = [v for v in c.camara_listar(data, cr) if c.PADRAO_NOMINAL.search(v.get("descricao") or "")]
    if not vs:
        return []

    def detalhe(v):
        i = v["id"]
        return (i, c.get_json(f"{c.CAMARA}/votacoes/{i}", "camara", "detalhe").get("dados", {}),
                c.get_json(f"{c.CAMARA}/votacoes/{i}/votos", "camara", "votos").get("dados", []),
                c.get_json(f"{c.CAMARA}/votacoes/{i}/orientacoes", "camara", "orientacoes").get("dados", []))

    with ThreadPoolExecutor(8) as ex:
        dets = list(ex.map(detalhe, vs))

    # Relação de deputados em exercício. A API só informa o dia, não a hora, e numa troca de
    # mandato (titular reassume, suplente sai) os dois aparecem em dias diferentes. Regra:
    # estáveis = em exercício no dia do fechamento E no dia seguinte; entram também todos que
    # votaram; quem trocou de situação no dia e não votou só entra se fechar a conta de 513.
    elencos = {}

    def elenco(dia):
        if dia not in elencos:
            q = f"dataInicio={dia}&dataFim={dia}&itens=1000"
            elencos[dia] = {d["id"]: d for d in c.get_json(f"{c.CAMARA}/deputados?{q}", "camara", "elenco")["dados"]}
        return elencos[dia]

    # primeiro voto de cada deputado no dia: resolve trocas de mandato (quem entrou só estava
    # em exercício a partir do primeiro voto; antes disso a vaga era de quem saiu)
    primeiro_voto = {}
    for _, _, votos, _ in dets:
        for x in votos:
            h, did = x.get("dataRegistroVoto"), x["deputado_"]["id"]
            if h and (did not in primeiro_voto or h < primeiro_voto[did]):
                primeiro_voto[did] = h

    saida = []
    for i, det, votos, ori in dets:
        if not votos:
            # placar na descrição sem votos nominais: é um ato que repete o placar de outra
            # votação do mesmo dia (ex. recurso registrado em dois atos). Não gera lista.
            print(f"camara {i}: placar na descrição, sem votos nominais — ignorada", file=sys.stderr)
            continue
        dia = (det.get("dataHoraRegistro") or data)[:10]
        seg = (dt.date.fromisoformat(dia) + dt.timedelta(days=1)).isoformat()
        hoje = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).date().isoformat()
        e0 = elenco(dia)
        e1 = elenco(seg) if seg <= hoje else e0  # dia seguinte ainda não existe
        estaveis = set(e0) & set(e1)
        secreta = bool(votos) and all(x.get("tipoVoto") is None for x in votos)
        nominal, vistos = [], set()
        for x in votos:
            d = x["deputado_"]
            vistos.add(d["id"])
            voto = "Votou (secreto)" if secreta else VOTO_CAMARA.get(x.get("tipoVoto"), x.get("tipoVoto"))
            nominal.append({"id": d["id"], "nome": d["nome"], "partido": d.get("siglaPartido"),
                            "uf": d.get("siglaUf"), "voto": voto, "horario_voto": x.get("dataRegistroVoto")})
        todos = {**e1, **e0}
        for did in sorted(estaveis - vistos):
            d = todos[did]
            nominal.append({"id": did, "nome": d["nome"], "partido": d.get("siglaPartido"),
                            "uf": d.get("siglaUf"), "voto": NAO_VOTOU, "horario_voto": None})
        # pareia as trocas por UF: se alguém da troca votou, o outro da mesma UF não estava em exercício
        mudaram = set(e0) ^ set(e1)
        uf_votou = {todos[d].get("siglaUf") for d in mudaram & vistos}
        transicao = sorted(d for d in mudaram - vistos if todos[d].get("siglaUf") not in uf_votou)
        fech = det.get("dataHoraRegistro") or ""
        for ent in [d for d in transicao if d in e1 and d not in e0 and d in primeiro_voto]:
            uf = todos[ent].get("siglaUf")
            saiu = [d for d in transicao if d in e0 and d not in e1 and todos[d].get("siglaUf") == uf]
            if primeiro_voto[ent] > fech:      # ainda não tinha reassumido: vaga de quem saiu
                transicao.remove(ent)
            elif saiu:                         # já tinha reassumido: quem saiu fica fora
                for d in saiu:
                    transicao.remove(d)
        # cadeiras ocupadas = relação do dia do fechamento (vaga aberta some dela) + quem votou sem
        # constar nela (posse no dia, ou falha da relação), nunca acima de 513 (a relação do dia
        # pode trazer quem saiu e quem entrou)
        ocupadas = min(TOTAL_CAMARA, len(set(e0) | vistos))
        falta = ocupadas - len(nominal)
        troca = None
        if transicao and falta == len(transicao):
            for did in transicao:
                d = todos[did]
                nominal.append({"id": did, "nome": d["nome"], "partido": d.get("siglaPartido"),
                                "uf": d.get("siglaUf"), "voto": NAO_VOTOU, "horario_voto": None})
        elif transicao and falta > 0:
            troca = {"vagas_sem_dono": falta,
                     "candidatos": [f"{todos[d]['nome']} ({todos[d].get('siglaPartido')}-{todos[d].get('siglaUf')})"
                                    for d in transicao]}
        nominal.sort(key=lambda p: sem_acento(p["nome"]))

        # orientação: do próprio partido; se vazia, da federação que o contém
        ori_p, ori_fed, ori_bruta = {}, {}, []
        for o in ori:
            nome, voto = o.get("siglaPartidoBloco") or "", o.get("orientacaoVoto") or ""
            ori_bruta.append({"lideranca": nome, "tipo": o.get("codTipoLideranca"), "orientacao": voto or None})
            if not voto:
                continue
            if o.get("codTipoLideranca") == "P":
                ori_p[nome] = voto
            elif nome.startswith("Fdr "):
                for s in nome[4:].split("-"):
                    ori_fed[s.strip()] = f"{voto} (via {nome})"
        partidos = {p["partido"] for p in nominal}
        orientacoes = {s: ori_p.get(s) or ori_fed.get(s) for s in partidos if ori_p.get(s) or ori_fed.get(s)}

        props = det.get("proposicoesAfetadas") or []
        materia = det.get("proposicaoObjeto") or (
            f"{props[0]['siglaTipo']} {props[0]['numero']}/{props[0]['ano']}" if props else "")
        # o id da votação é <id da proposição>-<sequencial>; o site da Câmara busca pela proposição
        id_prop = props[0]["id"] if props else int(i.split("-")[0])
        pauta = {
            "casa": "Câmara dos Deputados", "id_votacao": i, "data_sessao": det.get("data"),
            "encerrada_em": det.get("dataHoraRegistro"), "materia": materia,
            "id_proposicao": id_prop,
            "url_materia": f"https://www.camara.leg.br/propostas-legislativas/{id_prop}",
            "ementa": props[0].get("ementa") if props else None,
            "o_que_foi_votado": det.get("descUltimaAberturaVotacao") or det.get("descricao"),
            "resultado": det.get("descricao"),
            "url_api": f"{c.CAMARA}/votacoes/{i}",
        }
        val = validar(nominal, ocupadas, placar_do_texto(det.get("descricao")), secreta)
        if ocupadas < TOTAL_CAMARA:
            val["cadeiras_vagas_na_relacao_oficial"] = TOTAL_CAMARA - ocupadas
        if troca:
            val["troca_de_mandato_no_dia"] = troca
        saida.append({"pauta": pauta, "secreta": secreta, "validacao": val,
                      "orientacoes_brutas": ori_bruta, "lista_nominal": nominal,
                      "lista_por_partido": por_partido(nominal, orientacoes, secreta)})
    return saida


# ------------------------------------------------------------------ Senado
def senado(data: str) -> list[dict]:
    cr = c.Cronometro()
    vs = c.senado_coletar(data, cr)
    if not vs:
        return []
    try:
        ob = c.get_json(f"{c.SENADO}/plenario/votacao/orientacaoBancada/{data.replace('-', '')}",
                        "senado", "orientacao").get("votacoes", [])
    except RuntimeError:
        ob = []
    ob_seq = {o["sequencialVotacao"]: o for o in ob}

    saida = []
    for v in vs:
        secreta = v.get("votacaoSecreta") == "S"
        o = ob_seq.get(v.get("sequencialVotacao"), {})
        nominal = []
        for x in v.get("votos", []):
            sig = x.get("siglaVotoParlamentar")
            rotulo = x.get("descricaoVotoParlamentar")  # legenda oficial para códigos de ausência
            voto = sig if sig in ("Sim", "Não", "Abstenção", "Votou") or not rotulo else f"{rotulo} ({sig})"
            if sig == "Votou":
                voto = "Votou (secreto)"
            nominal.append({"id": x["codigoParlamentar"], "nome": x["nomeParlamentar"],
                            "partido": x.get("siglaPartidoParlamentar"), "uf": x.get("siglaUFParlamentar"),
                            "voto": voto})
        nominal.sort(key=lambda p: sem_acento(p["nome"]))

        siglas = {sem_acento(p["partido"]): p["partido"] for p in nominal if p["partido"]}
        orientacoes = {}
        for ol in o.get("orientacoesLideranca", []):
            k = sem_acento(ol["partido"])
            sigla = siglas.get(k) or siglas.get(sem_acento(ALIAS_SENADO.get(k, "")))
            # Governo, Oposição, Maioria, Minoria e Bancada Feminina não são partidos: ficam só em
            # orientacoes_brutas
            if sigla:
                orientacoes[sigla] = ol["voto"].capitalize()

        oficial = {}
        if o:
            oficial = {"Sim": o.get("qtdVotosSim"), "Não": o.get("qtdVotosNao"),
                       "Abstenção": o.get("qtdVotosAbstencao")}
            oficial = {k: val for k, val in oficial.items() if val is not None}
        pauta = {
            "casa": "Senado Federal", "id_votacao": v["codigoSessaoVotacao"], "data_sessao": v.get("dataSessao"),
            "inicio": o.get("dataInicioVotacao"), "encerrada_em": o.get("dataTerminoVotacao"),
            "materia": v.get("identificacao"), "id_proposicao": v.get("codigoMateria"),
            "url_materia": f"https://www25.senado.leg.br/web/atividade/materias/-/materia/{v.get('codigoMateria')}",
            "ementa": v.get("ementa"),
            "o_que_foi_votado": v.get("descricaoVotacao"),
            "resultado": {"A": "Aprovado", "R": "Rejeitado"}.get(v.get("resultadoVotacao"), v.get("resultadoVotacao")),
            "url_api": f"{c.SENADO}/votacao?codigoSessao={v.get('codigoSessao')}",
        }
        saida.append({"pauta": pauta, "secreta": secreta,
                      "validacao": validar(nominal, TOTAL_SENADO, oficial, secreta),
                      "orientacoes_brutas": o.get("orientacoesLideranca", []), "lista_nominal": nominal,
                      "lista_por_partido": por_partido(nominal, orientacoes, secreta)})
    return saida


def gravar(votacoes: list[dict], casa: str, data: str):
    pasta = OUT / casa / data
    pasta.mkdir(parents=True, exist_ok=True)
    for v in votacoes:
        i = str(v["pauta"]["id_votacao"])
        (pasta / f"{i}.json").write_text(json.dumps(v, ensure_ascii=False, indent=1), encoding="utf-8")
        cab = ["casa", "id_votacao", "materia", "ementa", "o_que_foi_votado", "resultado", "url_materia",
               "parlamentar", "partido", "uf", "voto"]
        with open(pasta / f"{i}_nominal.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cab)
            for p in v["lista_nominal"]:
                pa = v["pauta"]
                w.writerow([pa["casa"], i, pa["materia"], pa["ementa"], pa["o_que_foi_votado"], pa["resultado"],
                            pa["url_materia"], p["nome"], p["partido"], p["uf"], p["voto"]])
        with open(pasta / f"{i}_partidos.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["casa", "id_votacao", "materia", "partido", "orientacao", "voto", "qtd", "parlamentares"])
            for g in v["lista_por_partido"]:
                for voto, nomes in g["votos"].items():
                    w.writerow([v["pauta"]["casa"], i, v["pauta"]["materia"], g["partido"], g["orientacao"] or "",
                                voto, len(nomes), "; ".join(f"{n['nome']} ({n['uf']})" for n in nomes)])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--casa", choices=["camara", "senado", "ambas"], default="ambas")
    a = ap.parse_args()
    for casa, fn in (("camara", camara), ("senado", senado)):
        if a.casa in (casa, "ambas"):
            vs = fn(a.data)
            gravar(vs, casa, a.data)
            for v in vs:
                val = v["validacao"]
                print(f"{casa} {v['pauta']['id_votacao']} {v['pauta']['materia']}: "
                      f"{val['total_parlamentares']}/{val['total_esperado']} parlamentares, "
                      f"oficial={val['placar_oficial']} composicao={val['composicao']} "
                      f"{'OK' if val['ok'] else 'REVISAR'}{' (secreta)' if v['secreta'] else ''}")


if __name__ == "__main__":
    main()
