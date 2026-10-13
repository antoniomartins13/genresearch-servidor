"""Mede RSS (anônimo vs arquivo) e faltas de página durante o chunking de um doc.

    python -m tools.probe_rss --doc data/corpus_2docs/doc_00000.txt --read-mode mmap --cache frio

Imprime uma linha JSON em stdout com o estado antes do chunking e no último
chunk (antes do munmap/close), lidos de /proc/self/status e getrusage.
Números do CLAUDE.md: corpus de `gen-corpus --docs 2 --size-mb 200 --seed 42`.
"""

from __future__ import annotations

import argparse
import json
import resource
from pathlib import Path

from genresearch.ingest import count_chunks, iter_chunks
from tools.evict import evict_files

_FIELDS = ("VmRSS", "RssAnon", "RssFile", "VmHWM")


def snapshot() -> dict:
    """Campos de memória (kB) de /proc/self/status e faltas de getrusage."""
    with open("/proc/self/status") as f:
        status = dict(line.split(":", 1) for line in f)
    r = resource.getrusage(resource.RUSAGE_SELF)
    snap = {k + "_kb": int(status[k].split()[0]) for k in _FIELDS}
    snap.update(ru_minflt=r.ru_minflt, ru_majflt=r.ru_majflt)
    return snap


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--doc", type=Path, required=True)
    ap.add_argument("--read-mode", choices=("read", "mmap"), required=True)
    ap.add_argument("--cache", choices=("quente", "frio"), default="quente")
    ap.add_argument("--chunk-size", type=int, default=2048)
    ap.add_argument("--overlap", type=int, default=128)
    a = ap.parse_args()

    total = count_chunks(a.doc.stat().st_size, chunk_size=a.chunk_size, overlap=a.overlap)
    if a.cache == "frio":
        evict_files([a.doc])
    antes = snapshot()
    no_fim = None
    for i, _ in enumerate(iter_chunks(a.doc, chunk_size=a.chunk_size, overlap=a.overlap,
                                      read_mode=a.read_mode)):
        if i == total - 1:  # último chunk: fonte ainda aberta (antes do munmap)
            no_fim = snapshot()
    print(json.dumps({"doc": str(a.doc), "read_mode": a.read_mode, "cache": a.cache,
                      "n_chunks": total, "antes": antes, "no_fim": no_fim}))


if __name__ == "__main__":
    main()
