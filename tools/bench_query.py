"""Mede retrieve nos dois read_modes, com cache de páginas quente e frio.

    python -m tools.bench_query --store data/store_read

Gera `--n-queries` consultas determinísticas (trechos de 200 bytes do
próprio corpus, seed fixa), roda cada modo `--reps` vezes alternando a
ordem, e imprime uma linha JSON por (cache, rep, modo) em stdout.

Cache frio: antes de cada consulta, todos os arquivos do corpus são
despejados com posix_fadvise(DONTNEED) (ver tools/README.md).

Limitação: retrieve abre e mapeia o documento a cada consulta (mmap/munmap
por consulta). Um mapeamento persistente mudaria o resultado do mmap.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
from pathlib import Path

import numpy as np

from genresearch.retrieve import retrieve
from genresearch.store import Store
from tools.evict import evict_files


def make_queries(docs: list[Path], n: int, seed: int, size: int = 200) -> list[str]:
    """Consultas = trechos de `size` bytes em posições sorteadas do corpus."""
    rng = np.random.Generator(np.random.PCG64(seed))
    queries = []
    for _ in range(n):
        p = docs[rng.integers(len(docs))]
        off = int(rng.integers(0, p.stat().st_size - size))
        with open(p, "rb") as f:
            f.seek(off)
            queries.append(f.read(size).decode("ascii"))
    return queries


def run(store: Store, docs: list[Path], queries: list[str], mode: str, cold: bool,
        k: int, budget: int) -> dict:
    """Roda todas as consultas num modo e resume tempos e faltas de página."""
    leitura, total = [], []
    minflt = majflt = bytes_lidos = 0
    for q in queries:
        if cold:
            evict_files(docs)
        r0 = resource.getrusage(resource.RUSAGE_SELF)
        res = retrieve(store, q, k, budget, read_mode=mode)
        r1 = resource.getrusage(resource.RUSAGE_SELF)
        leitura.append(res.tempo_leitura_s)
        total.append(res.tempo_s)
        minflt += r1.ru_minflt - r0.ru_minflt
        majflt += r1.ru_majflt - r0.ru_majflt
        bytes_lidos += res.bytes_lidos
    n = len(queries)
    return {
        "tempo_leitura_mediana_s": statistics.median(leitura),
        "tempo_total_mediana_s": statistics.median(total),
        "minflt_por_consulta": minflt / n,
        "majflt_por_consulta": majflt / n,
        "bytes_lidos_por_consulta": bytes_lidos / n,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--store", type=Path, required=True)
    ap.add_argument("--n-queries", type=int, default=50)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=123)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--max-context-bytes", type=int, default=8192)
    a = ap.parse_args()

    store = Store.open(a.store)
    docs = sorted(store.corpus_dir.glob("*.txt"))
    queries = make_queries(docs, a.n_queries, a.seed)
    for q in queries[:3]:  # aquece numpy/sqlite fora da medição
        retrieve(store, q, a.k, a.max_context_bytes)
    for cold in (False, True):
        for rep in range(a.reps):
            for mode in (("read", "mmap") if rep % 2 == 0 else ("mmap", "read")):
                row = {"cache": "frio" if cold else "quente", "rep": rep, "read_mode": mode,
                       "n_consultas": a.n_queries, "k": a.k,
                       "max_context_bytes": a.max_context_bytes, "seed": a.seed}
                row.update(run(store, docs, queries, mode, cold, a.k, a.max_context_bytes))
                print(json.dumps(row), flush=True)
    store.close()


if __name__ == "__main__":
    main()
