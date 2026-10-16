"""Demonstração controlada da Parte D: cache e log sem proteção vs protegidos.

Uso (da raiz do repositório; uma linha JSON em stdout, mensagens em stderr):

  python -m tools.sync_demo cache --mode {unsafe,safe,single-flight} [...]
  python -m tools.sync_demo log   --mode {none,lock,queue} [...]

A "verdade" (operações realmente feitas, cálculos realmente executados) é
contada fora do cache/log, pelo próprio harness, para comparar com o que o
componente reporta. ``--window-us`` liga a janela de corrida artificial
(ver docstring de ``genresearch/cache.py``); omitido = só corrida natural.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sys
import threading
import time
from pathlib import Path

from genresearch.cache import SharedCache
from genresearch.logging_ import EventLog, Evento, verify_log


def _window(us: float | None) -> float | None:
    return None if us is None else us / 1e6


def run_cache(a: argparse.Namespace) -> dict:
    cache = SharedCache(a.capacity, policy=a.policy,
                        protected=a.mode != "unsafe",
                        single_flight=a.mode == "single-flight",
                        race_window_s=_window(a.window_us))
    truth = threading.Lock()
    computacoes = 0
    em_andamento: dict[str, int] = {}
    duplicadas_concorrentes = 0
    contagem = [0] * a.threads
    pedidas: list[set[str]] = [set() for _ in range(a.threads)]
    erros_thread: list[str] = []
    barrier = threading.Barrier(a.threads)

    def make_compute(key: str):
        def compute() -> str:
            nonlocal computacoes, duplicadas_concorrentes
            with truth:
                computacoes += 1
                if em_andamento.get(key, 0) > 0:
                    duplicadas_concorrentes += 1
                em_andamento[key] = em_andamento.get(key, 0) + 1
            time.sleep(a.cost_ms / 1000)
            with truth:
                em_andamento[key] -= 1
            return f"resposta:{key}"
        return compute

    def worker(tid: int) -> None:
        rng = random.Random(a.seed + tid)
        barrier.wait()
        try:
            for _ in range(a.ops):
                key = f"k{rng.randrange(a.keys)}"
                pedidas[tid].add(key)
                cache.get_or_compute(key, make_compute(key))
                contagem[tid] += 1
        except Exception as e:  # falha observada, não escondida
            erros_thread.append(repr(e))

    ths = [threading.Thread(target=worker, args=(i,), name=f"w{i}")
           for i in range(a.threads)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.perf_counter() - t0

    s = cache.stats()
    ops = sum(contagem)
    distintas = len(set().union(*pedidas))
    return {
        "experimento": "cache", "modo": a.mode, "politica": a.policy,
        "threads": a.threads, "ops_por_thread": a.ops, "chaves": a.keys,
        "capacidade": a.capacity, "custo_calculo_ms": a.cost_ms,
        "window_us": a.window_us, "seed": a.seed,
        "tempo_total_s": round(wall, 4), "vazao_ops_s": round(ops / wall, 1),
        "verdade": {
            "ops_realizadas": ops, "computacoes_reais": computacoes,
            "chaves_distintas": distintas,
            "computacoes_concorrentes_duplicadas": duplicadas_concorrentes,
            "excecoes_em_threads": erros_thread,
        },
        "reportado": dataclasses.asdict(s),
        "anomalias": {
            "contagens_perdidas": ops - (s.acertos + s.erros),
            "calculos_reportados_menos_reais": s.calculos - computacoes,
            "estouro_de_capacidade": max(0, s.tamanho_max_observado - a.capacity),
        },
    }


def run_log(a: argparse.Namespace) -> dict:
    out = Path(a.out or f"logs/sync_demo_log_{a.mode}.jsonl")
    log = EventLog(out, mode=a.mode, race_window_s=_window(a.window_us))
    barrier = threading.Barrier(a.threads)

    def worker(tid: int) -> None:
        barrier.wait()
        for i in range(a.events):
            log.log(Evento.CONSULTA, worker=tid, i=i)

    ths = [threading.Thread(target=worker, args=(i,), name=f"w{i}")
           for i in range(a.threads)]
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    log.close()
    wall = time.perf_counter() - t0
    esperadas = a.threads * a.events
    return {
        "experimento": "log", "modo": a.mode, "threads": a.threads,
        "eventos_por_thread": a.events, "window_us": a.window_us,
        "arquivo": str(out), "tempo_total_s": round(wall, 4),
        "vazao_eventos_s": round(esperadas / wall, 1),
        "esperadas": esperadas,
        "reportado": dataclasses.asdict(log.stats()),
        "verificacao": dataclasses.asdict(verify_log(out, esperadas=esperadas)),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.sync_demo")
    sub = p.add_subparsers(dest="alvo", required=True)
    c = sub.add_parser("cache")
    c.add_argument("--mode", choices=("unsafe", "safe", "single-flight"), required=True)
    c.add_argument("--policy", choices=("lru", "fifo"), default="lru")
    c.add_argument("--threads", type=int, default=8)
    c.add_argument("--ops", type=int, default=500)
    c.add_argument("--keys", type=int, default=40)
    c.add_argument("--capacity", type=int, default=16)
    c.add_argument("--cost-ms", type=float, default=1.0)
    c.add_argument("--window-us", type=float, default=None)
    c.add_argument("--seed", type=int, default=1)
    g = sub.add_parser("log")
    g.add_argument("--mode", choices=("none", "lock", "queue"), required=True)
    g.add_argument("--threads", type=int, default=8)
    g.add_argument("--events", type=int, default=500)
    g.add_argument("--window-us", type=float, default=None)
    g.add_argument("--out", default=None)
    a = p.parse_args(argv)
    res = run_cache(a) if a.alvo == "cache" else run_log(a)
    print(f"{a.alvo}/{a.mode}: ok", file=sys.stderr)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
