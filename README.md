# GenResearch-Servidor

Serviço local de RAG simplificado — Atividade 2 (AV2) de Sistemas
Operacionais, UFS 2026.2. O objetivo é medir memória, memória virtual, cache
de páginas, filas e sincronização, não qualidade de resposta.

## Como rodar

```bash
uv venv venv && uv pip install --python venv/bin/python -r requirements.txt
# ou: python3 -m venv venv && venv/bin/pip install -r requirements.txt

venv/bin/python -m genresearch gen-corpus --out data/corpus --docs 100 --size-mb 200 --seed 42
venv/bin/python -m genresearch ingest --corpus data/corpus --store data/store \
    --chunk-size 2048 --overlap 128 --read-mode read --dim 512 --batch-size 256
venv/bin/python -m genresearch query --store data/store --q "texto" --k 5 --max-context-bytes 8192

venv/bin/python -m unittest discover -s tests -v
```

Cada comando imprime suas medições como uma linha JSON em stdout; mensagens
humanas vão para stderr.

## O store depende do corpus

O índice (`data/store/`) **não guarda o texto dos chunks**: `meta.sqlite` tem
só posições (`chunk_id, doc_path, offset, length, n_bytes`) e `vectors.npy`
tem os vetores. Na consulta, o contexto é remontado relendo o corpus nesses
offsets, com o mesmo `read_mode` da ingestão (ou o de `query --read-mode`).

Por isso:

- o diretório do corpus precisa continuar existindo no caminho usado no
  `ingest` (gravado relativo ao diretório do store; mova os dois juntos);
- o corpus não pode mudar depois do `ingest`: `query` falha se o tamanho de
  um documento for diferente do ingerido. Regerar o corpus exige reingerir.

Essa escolha é deliberada: mantém o índice pequeno e faz a comparação
`read()` vs `mmap` e o cache de páginas valerem também na fase de consulta.
