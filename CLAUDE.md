# Contexto do projeto

Repositório da Atividade 2 (AV2) da disciplina Sistemas Operacionais — UFS, 2026.2.
Enunciado completo em `docs/enunciado.pdf` — consulte quando precisar do texto literal.

**Entrega: 20/10/2026 às 23h59. Apresentação: 21/10/2026.**

## O que é

GenResearch-Servidor: um serviço local de RAG simplificado, em Python.
Ingere documentos, fragmenta, gera vetores, indexa, recebe consultas por uma
fila, recupera contexto, responde e registra tudo.

O objetivo acadêmico é medir memória, memória virtual, cache de páginas,
armazenamento, filas e sincronização. **Não é qualidade textual de resposta.**
Toda decisão de design favorece medição limpa e reprodutibilidade acima de
performance ou elegância.

Equipe: 7 integrantes (a mesma da AV1).

## Regras inegociáveis

Nunca commitar, em nenhuma circunstância:

- corpus gerado, índices, vetores (`data/`, `*.npy`, `*.sqlite`)
- `.env`, chaves, tokens, senhas
- dados pessoais ou documentos sigilosos
- qualquer arquivo acima de 50 MB

`.gitignore` não limpa o histórico. Se algo dessa lista já foi commitado antes,
avise — o caso exige `git filter-repo`, não `git rm`.

O corpus é sintético e gerado por script com seed fixa. O gerador entra no
git; a saída dele, não.

## Decisões de arquitetura já tomadas

Não "melhore" nenhuma destas sem me perguntar. Cada uma existe para tornar um
experimento mensurável.

- **Threads + `queue.Queue`**, não `multiprocessing`. Geração simulada por
  I/O (`time.sleep`), recuperação em numpy (que libera o GIL). Isso dá
  contenção real e mantém o cache como estrutura compartilhada no mesmo
  espaço de endereçamento — que é o que a Parte D precisa para demonstrar
  condição de corrida.
- **No modo mmap, nunca materializar o arquivo inteiro.** Nada de `mm[:]`.
  Fatiar `mm[offset:offset+n]` sob demanda. Se o RSS do modo mmap ficar igual
  ao do modo read, o experimento central do trabalho não mede nada.
- **Hash determinístico** (`zlib.crc32` ou `hashlib.blake2b`), nunca o `hash()`
  embutido, que é aleatorizado por processo via PYTHONHASHSEED.
- **Vetores em um único `.npy` contíguo**, N x D float32, L2-normalizado na
  escrita. Nada de BLOB no SQLite — o arquivo contíguo é o que permite
  `np.load(mmap_mode='r')`, que é outro experimento.
- **Chunking por janela fixa de bytes com sobreposição**, determinístico. Sem
  separador semântico: tamanho variável contamina as medições de memória.
- **`read_mode` decidido em um ponto só**, dentro de `reader.py`.
- Tempos vêm de `time.perf_counter`; memória e faltas de página vêm de
  `/proc/self/status` e `resource.getrusage`. Nada de cronometragem manual.

## Estrutura alvo

```
README.md
VIDEO.md
CLAUDE.md
.gitignore
.python-version
requirements.txt
genresearch/
  __main__.py    entrypoint: python -m genresearch <subcomando>
  config.py      Config como dataclass + CLI
  corpus.py      gerador de corpus sintético
  reader.py      leitura via read() ou mmap
  store.py       SQLite (metadados) + .npy (vetores)
  ingest.py      chunking + embeddings + persistência
  retrieve.py    similaridade e montagem de contexto
  cache.py       cache compartilhado, com e sem lock
  server.py      fila, produtores, consumidores
  metrics.py     coleta de RSS, VSZ, faltas de página
  logging_.py    log protegido contra escrita concorrente
scripts/
  00-ambiente.sh      inventário da Parte A
  run-experimentos.sh executa C1 a C6
  graficos.py
tests/
docs/
  enunciado.pdf
  relatorio.pdf
  apresentacao.pdf
resultados/     CSVs e gráficos (versionados)
logs/
```

## Contratos do núcleo

Assinaturas públicas das quais as outras frentes dependem. Mudanças aqui
exigem aviso à equipe. Parâmetros novos só entram como keyword-only com
default, para não quebrar chamadas existentes.

```python
# reader.py — único ponto onde read_mode é decidido
def open_source(path: Path, mode: ReadMode) -> ContextManager[ByteSource]
#   ByteSource: .size, .bytes_lidos, .slice(offset, n) -> bytes
#   "read": f.read() do arquivo inteiro; "mmap": fatia mm[a:b] sob demanda,
#   nunca materializa o arquivo. (Não existe read_file: foi removido do
#   contrato porque no modo mmap ele materializaria o arquivo inteiro.)

# ingest.py
def iter_chunks(path: Path, *, chunk_size: int, overlap: int,
                read_mode: str) -> Iterator[Chunk]
def embed(texts: list[str], *, dim: int = 512) -> np.ndarray  # (N, D) float32, L2
def ingest(corpus_dir: Path, store: Store, *, chunk_size: int, overlap: int,
           read_mode: str, batch_size: int = 256) -> IngestStats
#   dim vem de store.dim; batch_size = chunks por lote de embedding/persistência

# store.py — <dir>/meta.sqlite (metadados + texto) e <dir>/vectors.npy (N x D)
Store.create(root, *, dim=512) -> Store       # escrita; apaga índice anterior
Store.open(root, *, vectors_mmap=False) -> Store  # leitura; seguro entre threads

# retrieve.py
def retrieve(store: Store, query: str, k: int,
             max_context_bytes: int) -> RetrieveResult
```

- `Chunk(doc, seq, offset, length, text)`
- `IngestStats`: n_documentos, n_chunks, bytes_lidos, tamanho_corpus_bytes,
  tamanho_indice_bytes, tempo_total_s, tempo_leitura_s, tempo_embedding_s,
  tempo_persistencia_s, + parâmetros (read_mode, chunk_size, overlap, dim,
  batch_size)
- `RetrieveResult`: chunks (`RetrievedChunk`), n_trechos, bytes_contexto,
  tempo_s, contexto, tempo_embedding_s, tempo_busca_s, tempo_montagem_s,
  k, max_context_bytes. Na linha JSON da CLI, `contexto` e o texto dos
  trechos são omitidos
- `iter_chunks` é um gerador que, ao terminar, retorna os bytes lidos da
  fonte em `StopIteration.value` (é assim que `ingest` preenche
  `bytes_lidos`)
- `chunk_id` = linha em `vectors.npy`; determinístico (documentos em ordem de
  nome, chunks em ordem de offset), independe do modo de leitura
- Contexto: trechos em ordem de score; o que estoura `max_context_bytes` é
  truncado e encerra a montagem

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

Versões fixadas em `.python-version` e `requirements.txt`: o fluxo de
`numpy.random` não tem garantia de estabilidade entre versões do numpy, e o
corpus só é idêntico byte a byte com a mesma versão.

## Divisão de frentes

Sete pessoas mexem neste repositório em paralelo. Ao trabalhar em uma frente,
não implemente as outras — nem como stub.

| Frente | Módulos |
|---|---|
| Núcleo | `config`, `corpus`, `reader`, `store`, `ingest`, `retrieve` |
| Fila e workers | `server` |
| Cache e sincronização | `cache`, `logging_` |
| Instrumentação | `metrics`, `scripts/run-experimentos.sh` |
| Análise | `scripts/graficos.py`, `resultados/` |
| Relatório e vídeo | `docs/`, `README.md`, `VIDEO.md` |

## Critérios de aceite

Experimentos obrigatórios, no mesmo corpus e mesma aplicação:

- C1 leitura convencional, C2 mmap, C3 concorrência e fila (≥2 produtores e
  ≥2 consumidores), C4 cache compartilhado — todos obrigatórios
- C5 pressão de memória e C6 falha controlada — executar se possível; caso
  contrário, justificar tecnicamente e apresentar alternativa
- Mínimo 3 repetições por configuração
- Variar pelo menos 3 fatores: workers (1, 2, 4), tamanho do corpus, e um
  entre lote, contexto, capacidade da fila, tamanho do cache ou uso de mmap
- Registrar comandos, parâmetros, horário, versões, ambiente e dados brutos

Parte D exige pelo menos uma **falha controlada demonstrada**, não descrita:
atualização perdida, log intercalado, duplicação de cache, consumidor
bloqueado, fila saturada ou starvation. Precisa haver uma flag que roda a
versão sem proteção e outra com.

Relatório: PDF entre 8 e 12 páginas, excluídos anexos, com estas seções —
tabela de contribuição individual; introdução e problema; pergunta norteadora;
ambiente; arquitetura; dados, corpus, embeddings, índice e cache; processos,
threads, filas e recursos compartilhados; estratégia de sincronização;
metodologia; resultados; análise de memória, armazenamento, paginação e E/S;
análise de concorrência, cache e correção; relação com Sistemas Operacionais e
IA generativa; limitações; conclusão; referências.

Vídeo de até 20 minutos, com participação de todos, URL no `README.md`, no
relatório e em `VIDEO.md`.

## Convenções

- Python 3, stdlib + numpy. Sem frameworks web, LangChain, torch ou
  sentence-transformers
- Type hints em funções públicas, docstrings curtas em português
- Commits atômicos, mensagens em português; não faça `push` sem eu pedir
- Scripts bash com `set -euo pipefail`
- Caminhos relativos à raiz do repositório, nunca absolutos do host
- Stats de cada comando saem como uma linha JSON em stdout; mensagens humanas
  vão para stderr
- **Nada de dados inventados.** Se um CSV, log ou medição não existir, deixe o
  arquivo ausente e me avise. Não preencha resultado nenhum por estimativa,
  nem gere dados de exemplo para "completar a estrutura"
