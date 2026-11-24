# Protótipo: coleta de votações nominais (Câmara + Senado)

Python 3.9+, sem dependências externas.

```bash
python3 coletor.py coletar --data 2026-11-23 --so-nominais
python3 coletor.py bench --data 2026-11-23 --paralelo 16 --rodadas 5 --so-nominais
python3 coletor.py monitorar --intervalo 30 --duracao 3600
```

Saídas em `saida/` (ignorada pelo git): JSON normalizado, HTML de apresentação,
CSV com tempo de cada requisição, medianas do bench e `monitor.csv`.

Detalhes, medições e riscos estão no relatório de handoff.
