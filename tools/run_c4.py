"""C4 — cache compartilhado sobre a fila do Toni (``genresearch.server``).

Não altera ``server.py``: reaproveita ``run_queries``, ``construir_processador``
e ``linha_resumo`` e envolve o processador com ``cache.cached_processor``.

  python -m tools.run_c4 --store data/store --workers 2 --n-queries 200 \\
      --workload mista --distinct 20 --repeat-frac 0.7 \\
      --cache-capacity 16 --cache-policy lru --cache-mode safe \\
      --config C4_mista --rep 1 --csv resultados/cache_resumo.csv \\
      --event-log logs/c4_mista_rep1.jsonl

``--cache-capacity 0`` = linha de base sem cache. ``--demo`` usa processador
só com sleep, imprime JSON e RECUSA gravar CSV (dado simulado não vai para
``resultados/``). Uma linha JSON em stdout; mensagens em stderr.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path

from genresearch.cache import SharedCache, cached_processor
from genresearch.logging_ import EventLog, Evento, verify_log
from genresearch.server import (CAMPOS_RESUMO, CONSULTAS_PADRAO, _processador_demo,
                                construir_processador, linha_resumo, run_queries)

WORKLOADS = ("repetida", "distinta", "mista")
CAMPOS_CACHE = ["workload", "distinct", "repeat_frac", "seed", "cache_capacity",
                "cache_policy", "cache_mode", "window_us", "acertos", "erros",
                "taxa_acerto", "substituicoes", "calculos", "coalescidas",
                "tempo_economizado_s", "espera_lock_s", "tamanho_max_observado",
                "falhas_concorrencia"]


def gerar_workload(n: int, kind: str, *, distinct: int = 20, repeat_frac: float = 0.7,
                   seed: int = 1, base: list[str] | None = None) -> list[str]:
    """Lista determinística de ``n`` consultas (mesma seed = mesma lista).

    - ``repetida``: só ``distinct`` consultas diferentes, repetidas em ordem
      embaralhada (acertos esperados quando o cache comporta ``distinct``).
    - ``distinta``: ``n`` consultas todas diferentes (nenhum acerto possível).
    - ``mista``: fração ``repeat_frac`` vem de um conjunto quente de
      ``distinct`` consultas; o resto é único.

    Consultas distintas são combinações de 3 palavras do vocabulário de
    ``base`` (padrão: consultas do server), então diferem no texto e portanto
    na chave de cache.
    """
    if kind not in WORKLOADS:
        raise ValueError(f"workload deve ser um de {WORKLOADS}")
    if n < 0 or distinct < 1 or not 0.0 <= repeat_frac <= 1.0:
        raise ValueError("parâmetros inválidos")
    rng = random.Random(seed)
    vocab = sorted({w for q in (base or CONSULTAS_PADRAO) for w in q.split()})
    vistas: set[str] = set()

    def nova() -> str:
        while True:
            q = " ".join(rng.sample(vocab, 3))
            if q not in vistas:
                vistas.add(q)
                return q

    if kind == "distinta":
        return [nova() for _ in range(n)]
    quentes = [nova() for _ in range(distinct)]
    if kind == "repetida":
        out = [quentes[i % distinct] for i in range(n)]
        rng.shuffle(out)
        return out
    return [rng.choice(quentes) if rng.random() < repeat_frac else nova()
            for _ in range(n)]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.run_c4")
    p.add_argument("--store", type=Path)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--producers", type=int, default=2)
    p.add_argument("--queue-capacity", type=int, default=4)
    p.add_argument("--n-queries", type=int, default=200)
    p.add_argument("--workload", choices=WORKLOADS, default="mista")
    p.add_argument("--distinct", type=int, default=20)
    p.add_argument("--repeat-frac", type=float, default=0.7)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--max-context-bytes", type=int, default=8192)
    p.add_argument("--gen-ms", type=float, default=20.0)
    p.add_argument("--read-mode", choices=["read", "mmap"], default=None)
    p.add_argument("--cache-capacity", type=int, default=16, help="0 = sem cache")
    p.add_argument("--cache-policy", choices=["lru", "fifo"], default="lru")
    p.add_argument("--cache-mode", choices=["unsafe", "safe", "single-flight"], default="safe")
    p.add_argument("--window-us", type=float, default=None,
                   help="janela de corrida artificial (declarar no relatório)")
    p.add_argument("--event-log", type=Path)
    p.add_argument("--log-mode", choices=["none", "lock", "queue"], default="lock")
    p.add_argument("--config", default="")
    p.add_argument("--rep", type=int, default=0)
    p.add_argument("--csv", type=Path)
    p.add_argument("--demo", action="store_true")
    a = p.parse_args(argv)

    if a.demo and a.csv:
        print("erro: --demo não grava CSV (dados simulados não entram em resultados/)",
              file=sys.stderr)
        return 2
    if not a.demo and a.store is None:
        print("erro: informe --store (ou use --demo)", file=sys.stderr)
        return 2

    consultas = gerar_workload(a.n_queries, a.workload, distinct=a.distinct,
                               repeat_frac=a.repeat_frac, seed=a.seed)
    proc = (_processador_demo(a.gen_ms) if a.demo else
            construir_processador(a.store, k=a.k, max_context_bytes=a.max_context_bytes,
                                  gen_ms=a.gen_ms, read_mode=a.read_mode))
    log = EventLog(a.event_log, mode=a.log_mode) if a.event_log else None
    cache = None
    if a.cache_capacity > 0:
        cache = SharedCache(a.cache_capacity, policy=a.cache_policy,
                            protected=a.cache_mode != "unsafe",
                            single_flight=a.cache_mode == "single-flight",
                            race_window_s=None if a.window_us is None else a.window_us / 1e6)
        proc = cached_processor(proc, cache, k=a.k, max_context_bytes=a.max_context_bytes,
                                log=log)
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"C4: {len(consultas)} consultas ({a.workload}), {a.workers} worker(s), "
          f"cache={'off' if cache is None else f'{a.cache_mode}/{a.cache_policy}/{a.cache_capacity}'}",
          file=sys.stderr)
    _, stats = run_queries(proc, consultas, workers=a.workers, producers=a.producers,
                           queue_capacity=a.queue_capacity)

    log_info = None
    if log is not None:
        log.log(Evento.ENCERRAMENTO, processadas=stats["processadas"], erros=stats["erros"])
        log.close()
        log_info = {"stats": dataclasses.asdict(log.stats()),
                    "verificacao": dataclasses.asdict(
                        verify_log(a.event_log, esperadas=log.stats().n_eventos))}

    cs = dataclasses.asdict(cache.stats()) if cache is not None else {}
    if a.csv:
        linha = {**linha_resumo(stats, config=a.config, rep=a.rep, timestamp=ts),
                 "workload": a.workload, "distinct": a.distinct,
                 "repeat_frac": a.repeat_frac, "seed": a.seed,
                 "cache_capacity": a.cache_capacity,
                 "cache_policy": a.cache_policy if cache else "",
                 "cache_mode": a.cache_mode if cache else "",
                 "window_us": a.window_us if a.window_us is not None else "",
                 **{k: cs.get(k, "") for k in CAMPOS_CACHE[8:]}}
        a.csv.parent.mkdir(parents=True, exist_ok=True)
        novo = not a.csv.exists()
        with open(a.csv, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS_RESUMO + CAMPOS_CACHE)
            if novo:
                w.writeheader()
            w.writerow(linha)

    saida = {k: v for k, v in stats.items() if k != "requisicoes"}
    saida.update(config=a.config, rep=a.rep, timestamp=ts, cpu_count=os.cpu_count(),
                 workload=a.workload, demo=a.demo, cache=cs, event_log=log_info)
    print(json.dumps(saida, ensure_ascii=False))
    return 0 if stats["erros"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
