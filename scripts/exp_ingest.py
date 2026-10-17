"""Ingestão instrumentada (C1/C2 e C5): uma linha JSON em stdout.

    python -m scripts.exp_ingest --corpus data/exp/corpus_200mb_100d \\
        --store data/exp/c12_store_read --read-mode read --cache frio --rep 1 --rotulo C12

Roda ``genresearch.ingest`` no próprio processo (sem alterar o núcleo) e mede
em volta dele: tempos do ``IngestStats``, pico de RssAnon e RssFile (thread
amostradora), faltas de página, bytes realmente lidos do armazenamento
(``/proc/self/io``) e MemAvailable/Cached antes e depois.

``--cache frio`` faz ``sync`` e despeja os arquivos do corpus do page cache
antes de começar (``tools.evict``); ``quente`` não despeja nada.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from genresearch.ingest import ingest
from genresearch.store import Store
from scripts import medidas_proc as mp
from tools.evict import evict_files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.exp_ingest")
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--store", type=Path, required=True)
    ap.add_argument("--read-mode", choices=("read", "mmap"), required=True)
    ap.add_argument("--cache", choices=("frio", "quente"), default="frio")
    ap.add_argument("--chunk-size", type=int, default=2048)
    ap.add_argument("--overlap", type=int, default=128)
    ap.add_argument("--dim", type=int, default=512)
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--rotulo", default="")
    a = ap.parse_args(argv)

    docs = sorted(a.corpus.glob("*.txt"))
    if not docs:
        print(f"erro: nenhum .txt em {a.corpus}", file=sys.stderr)
        return 2
    if a.cache == "frio":
        os.sync()
        evict_files(docs)

    m0, s0, r0, i0 = mp.meminfo(), mp.status(), mp.rusage(), mp.io()
    t0 = time.perf_counter()
    with mp.PicoRss() as pico:
        store = Store.create(a.store, dim=a.dim)
        try:
            stats = ingest(a.corpus, store, chunk_size=a.chunk_size, overlap=a.overlap,
                           read_mode=a.read_mode, batch_size=a.batch_size)
        finally:
            store.close()
    t1 = time.perf_counter()
    m1, s1, r1, i1 = mp.meminfo(), mp.status(), mp.rusage(), mp.io()

    row = {
        "experimento": "ingestao", "rotulo": a.rotulo, "rep": a.rep, "cache": a.cache,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pid": os.getpid(), "corpus": str(a.corpus), "store": str(a.store),
        **dataclasses.asdict(stats),
        "tempo_medido_externo_s": t1 - t0,
        "pico_rss_kb": pico.rss_kb, "pico_rss_anon_kb": pico.anon_kb,
        "pico_rss_file_kb": pico.file_kb,
        "vmhwm_kb": s1.get("VmHWM_kb"),
        "rss_anon_antes_kb": s0.get("RssAnon_kb"), "rss_file_antes_kb": s0.get("RssFile_kb"),
        **mp.delta(r0, r1),
        **mp.delta(i0, i1),
        **mp.com_prefixo(m0, "antes_"), **mp.com_prefixo(m1, "depois_"),
    }
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
