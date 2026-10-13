# tools/

Medições do núcleo cujos números estão citados no `CLAUDE.md`. Rode da raiz
do repositório; cada ferramenta imprime linhas JSON em stdout.

| Ferramenta | O que mede |
|---|---|
| `python -m tools.evict ARQ...` | despeja arquivos do page cache (ver abaixo) |
| `python -m tools.probe_rss --doc D --read-mode M [--cache frio]` | RssAnon, RssFile e faltas de página durante o chunking de um documento |
| `python -m tools.bench_query --store S` | tempo de leitura e faltas por consulta, read vs mmap, cache quente e frio |

## Despejo de cache por arquivo: `posix_fadvise(POSIX_FADV_DONTNEED)`

Para medir leitura "a frio" é preciso tirar do page cache as páginas dos
arquivos lidos. `tools/evict.py` faz isso por arquivo:

```python
fd = os.open(path, os.O_RDONLY)
os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)   # offset 0, len 0 = arquivo todo
os.close(fd)
```

Por que preferir isso a `echo 3 > /proc/sys/vm/drop_caches`:

- **Não precisa de root.** Qualquer usuário com permissão de leitura no
  arquivo pode despejá-lo, então a bateria roda sem `sudo` e sem pedir
  senha no meio de um experimento.
- **Não limpa o cache da máquina inteira.** `drop_caches` descarta o page
  cache, dentries e inodes de todo o sistema: bibliotecas do Python, do
  numpy, o binário do interpretador e outros arquivos passam a ser lidos do
  disco de novo.
- **Não contamina as outras medições da bateria.** Com `drop_caches`, a
  medição seguinte paga faltas maiores e E/S que não têm relação com o
  fator estudado (carregar o interpretador, o `vectors.npy`, o SQLite).
  Com o despejo por arquivo, só o corpus fica frio; todo o resto continua
  quente, e a diferença medida é atribuível à leitura do corpus.

Ressalvas:

- é um *conselho* ao kernel: páginas sujas (ainda não gravadas) não são
  descartadas. Para arquivos recém-escritos, chame `os.fsync` antes. O
  corpus gerado por `gen-corpus` já é escrito antes da ingestão, mas em
  um arquivo recém-gerado vale sincronizar;
- páginas mapeadas por algum processo (um mmap ainda aberto) não são
  despejadas; despeje com os mapeamentos fechados;
- para conferir o efeito, `fincore` (util-linux) mostra quantas páginas de
  um arquivo estão no cache: `fincore data/corpus/*.txt`.

Uso em C1 e C2: antes de cada repetição "fria", despejar todos os arquivos
do corpus (`tools.evict.evict_files(sorted(corpus.glob("*.txt")))`); o
restante do ambiente permanece como estava.
