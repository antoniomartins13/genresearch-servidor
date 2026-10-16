# Integração da instrumentação ao repositório (16/10/2026)

Base: ZIP `genresearch-servidor(1).zip`. Foram preservados sem alterações os módulos `genresearch/{cache,logging_,reader,store,retrieve,ingest,corpus,config}.py` e `tools/run_c4.py`.

## Arquivos adicionados

- `genresearch/server.py`: versão do Antônio recebida no pacote integrado; necessária porque o repositório-base não continha o servidor. **Confirmar com Antônio se esta é sua versão mais recente antes de enviar ao remoto.**
- `src/metrics.py`, `src/component_metrics.py`: instrumentação externa, sem duplicar contadores internos do cache ou da fila.
- `scripts/benchmark.py`, `scripts/server_runner.py`, `scripts/cache_runner.py`: execução em subprocesso com PID monitorado; wrappers aguardam confirmação via stdin para permitir snapshot final.
- `tests/test_server.py`, `tests/test_instrumentation.py` e scripts de teste avulsos.
- `requirements.txt`: acrescentado `psutil` para instrumentação.

## Comandos (Linux/WSL, na raiz)

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m unittest discover -s tests -v
venv/bin/python -m genresearch gen-corpus --out data/corpus --docs 6 --size-mb 6 --seed 42
venv/bin/python -m genresearch ingest --corpus data/corpus --store data/store --read-mode read
venv/bin/python -m scripts.benchmark --configs C1 C2 C3 C4 C5 C6 --repeats 3 --requests 40 --store data/store --event-logs
```

`--demo` separa os dados em `results/instrumentation/demo`; sem `--demo`, em `results/instrumentation/real`. Não usar dados de demo no relatório. C1/C2 no benchmark comparam leitura **na consulta** por `read_mode` explicitamente passado ao `retrieve`; a comparação de **ingestão** requer criar índices independentes com `genresearch ingest --read-mode read|mmap` e medir essas execuções separadamente. C5 varia o tamanho de contexto (não é prova de swap ou thrashing). C6 testa saturação com `put_timeout`.

Resultados do benchmark: `resumo.csv`, `amostras.csv`, `requisicoes/<config>_rep<N>.csv` (servidor), logs C4 quando ativados. CSVs de resumo/amostras acrescentam linhas sem sobrescrever; os CSVs por requisição e logs nomeados por configuração/repetição devem ser executados em diretórios de saída novos para evitar sobrescrita entre rodadas.

## Limitações

- O cache do Davi já tem contadores próprios; `ComponentMetrics` não deve contá-los novamente.
- O `run_c4.py` existente não expõe CSV individual de requisições; não foi modificado.
- `--cache-mode unsafe` demonstra uma condição de corrida e seus contadores não devem ser interpretados como verdade de referência.
- Experimentos finais requerem Linux/WSL e corpus/index persistentes; medições de Linux não são substituíveis por Windows.
