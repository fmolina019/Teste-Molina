# Protótipo: coleta de votações nominais (Câmara + Senado)

Python 3.9+, sem dependências externas.

```bash
# listas por votação (entregável): JSON + CSV nominal + CSV por partido, com validação
python3 listas.py --data 2026-11-23

python3 coletor.py coletar --data 2026-11-23 --so-nominais
python3 coletor.py bench --data 2026-11-23 --paralelo 16 --rodadas 5 --so-nominais
python3 monitor.py --intervalo 30 --recheck 600 --dias 7 --duracao 82800
```

Saídas em `saida/` (ignorada pelo git): JSON normalizado, HTML de apresentação,
CSV com tempo de cada requisição, medianas do bench e `monitor.csv`.

Detalhes, medições e riscos estão no relatório de handoff.
