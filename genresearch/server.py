"""Fila de requisições, produtores e consumidores (frente "Fila e workers").

Modelo produtores/consumidores com buffer LIMITADO (``queue.Queue(maxsize)``):

- ``put()`` bloqueia o produtor com a fila cheia      (equivale a "full")
- ``get()`` bloqueia o worker com a fila vazia        (equivale a "empty")
- o lock interno da ``Queue`` protege o acesso        (equivale a "mutex")

Mecanismo: threads (decisão de arquitetura do projeto). A geração é simulada
por E/S (``time.sleep``) e a recuperação usa numpy, que libera a GIL.

Uso como biblioteca::

    resultados, stats = run_queries(processador, consultas, workers=2,
                                    producers=2, queue_capacity=4)

Uso pela CLI (uma linha JSON em stdout; mensagens humanas em stderr)::

    python -m genresearch.server --store data/store --workers 2 --producers 2 \\
        --queue-capacity 4 --n-queries 40 --config C3_w2 --rep 1 \\
        --csv resultados/fila_resumo.csv

Este módulo só depende da stdlib; ``store``/``retrieve`` são importados
apenas por ``construir_processador``.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import queue
import statistics
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger("genresearch.server")
_FIM = object()  # sentinela: avisa ao worker que não há mais trabalho

REJEITADA = "REJEITADA_FILA_CHEIA"


@dataclass
class _Tarefa:
    idx: int
    consulta: Any
    produtor: int
    t_enfileirada: float = 0.0  # instante em que o item ENTROU na fila


def _percentil(valores: Sequence[float], p: float) -> float:
    """Percentil pelo método nearest-rank; 0.0 se não houver valores."""
    if not valores:
        return 0.0
    s = sorted(valores)
    return s[max(1, math.ceil(p / 100.0 * len(s))) - 1]


def _resumo(valores: Sequence[float]) -> dict:
    if not valores:
        return {"media": 0.0, "mediana": 0.0, "p95": 0.0, "max": 0.0}
    return {"media": statistics.fmean(valores), "mediana": statistics.median(valores),
            "p95": _percentil(valores, 95), "max": max(valores)}


def run_queries(
    processador: Callable[[Any], Any],
    consultas: Sequence[Any],
    *,
    workers: int = 2,
    producers: int = 2,
    queue_capacity: int = 4,
    retries: int = 0,
    put_timeout: Optional[float] = None,
) -> tuple[list, dict]:
    """Processa ``consultas`` com uma fila limitada, produtores e workers.

    ``processador`` recebe uma consulta e devolve o resultado (qualquer valor).
    ``retries`` são tentativas extras quando o processador levanta exceção.
    ``put_timeout=None`` faz o produtor bloquear até haver espaço
    (backpressure); um número de segundos rejeita a consulta se a fila
    continuar cheia (cenário C6, fila saturada).

    Devolve ``(resultados, stats)``: resultados na ordem de ``consultas``
    (``None`` se falhou/rejeitada) e um dict de métricas; o detalhe por
    consulta fica em ``stats["requisicoes"]``.
    """
    if workers < 1 or producers < 1 or queue_capacity < 1:
        raise ValueError("workers, producers e queue_capacity devem ser >= 1")

    n = len(consultas)
    fila: queue.Queue = queue.Queue(maxsize=queue_capacity)
    resultados: list = [None] * n
    reqs: list = [None] * n
    lock = threading.Lock()  # protege as estruturas de métricas abaixo
    amostras_fila: list[int] = []
    bloqueio_prod = [0.0] * producers
    ocupado = [0.0] * workers
    contagem = [0] * workers
    bloqueio_req: dict[int, float] = {}
    rejeitadas = [0]

    def produtor(pid: int) -> None:
        for idx in range(pid, n, producers):  # round-robin determinístico
            t = _Tarefa(idx=idx, consulta=consultas[idx], produtor=pid)
            t0 = time.perf_counter()
            try:
                fila.put(t, block=True, timeout=put_timeout)
            except queue.Full:
                bloq = time.perf_counter() - t0
                with lock:
                    rejeitadas[0] += 1
                    bloqueio_prod[pid] += bloq
                    reqs[idx] = dict(id=idx, consulta=str(t.consulta), produtor=pid,
                                     worker=-1, bloqueio_put_s=bloq, espera_s=0.0,
                                     proc_s=0.0, ok=False, erro=REJEITADA, tentativas=0)
                log.warning("consulta %d rejeitada: fila cheia (timeout %.3fs)", idx, put_timeout)
                continue
            agora = time.perf_counter()
            t.t_enfileirada = agora
            with lock:
                bloqueio_prod[pid] += agora - t0
                bloqueio_req[idx] = agora - t0
                amostras_fila.append(fila.qsize())
            log.debug("produtor %d enfileirou %d", pid, idx)

    def worker(wid: int) -> None:
        while True:
            t = fila.get()
            try:
                if t is _FIM:
                    return
                inicio = time.perf_counter()
                espera = inicio - t.t_enfileirada
                ok, erro, res, tent = False, "", None, 0
                for tent in range(1, retries + 2):
                    try:
                        res = processador(t.consulta)
                        ok, erro = True, ""
                        break
                    except Exception as e:  # um erro não derruba o worker
                        erro = f"{type(e).__name__}: {e}"
                        log.warning("worker %d consulta %d tentativa %d falhou: %s",
                                    wid, t.idx, tent, erro)
                proc = time.perf_counter() - inicio
                with lock:
                    resultados[t.idx] = res if ok else None
                    reqs[t.idx] = dict(id=t.idx, consulta=str(t.consulta), produtor=t.produtor,
                                       worker=wid, bloqueio_put_s=bloqueio_req.get(t.idx, 0.0),
                                       espera_s=espera, proc_s=proc, ok=ok, erro=erro,
                                       tentativas=tent)
                    ocupado[wid] += proc
                    contagem[wid] += 1
                    amostras_fila.append(fila.qsize())
            finally:
                fila.task_done()  # sempre, inclusive com erro ou sentinela

    t_ini = time.perf_counter()
    tw = [threading.Thread(target=worker, args=(i,), name=f"worker-{i}") for i in range(workers)]
    tp = [threading.Thread(target=produtor, args=(i,), name=f"produtor-{i}") for i in range(producers)]
    for t in tw + tp:
        t.start()
    for t in tp:
        t.join()
    for _ in tw:  # uma sentinela por worker: encerramento limpo
        fila.put(_FIM)
    fila.join()
    for t in tw:
        t.join()
    total = time.perf_counter() - t_ini

    feitas = [r for r in reqs if r is not None]
    ok = [r for r in feitas if r["ok"]]
    erros = [r for r in feitas if not r["ok"] and r["erro"] != REJEITADA]
    stats = {
        "workers": workers, "producers": producers, "queue_capacity": queue_capacity,
        "consultas_total": n, "processadas": len(ok), "erros": len(erros),
        "rejeitadas": rejeitadas[0], "retries": retries,
        "tempo_total_s": total,
        "vazao_rps": len(ok) / total if total > 0 else 0.0,
        "espera_s": _resumo([r["espera_s"] for r in ok]),
        "proc_s": _resumo([r["proc_s"] for r in ok]),
        "latencia_s": _resumo([r["espera_s"] + r["proc_s"] for r in ok]),
        "bloqueio_produtores_s": sum(bloqueio_prod),
        "fila_max": max(amostras_fila) if amostras_fila else 0,
        "fila_media": statistics.fmean(amostras_fila) if amostras_fila else 0.0,
        "utilizacao_workers": [o / total if total > 0 else 0.0 for o in ocupado],
        "utilizacao_media": sum(ocupado) / (total * workers) if total > 0 else 0.0,
        "consultas_por_worker": list(contagem),
        "requisicoes": feitas,
    }
    return resultados, stats


# ----------------------------------------------------------------------------
# Integração com o núcleo
# ----------------------------------------------------------------------------
def construir_processador(store_dir: Path, *, k: int = 5, max_context_bytes: int = 8192,
                          gen_ms: float = 20.0, read_mode: Optional[str] = None,
                          vectors_mmap: bool = False) -> Callable[[str], dict]:
    """Monta o processador real: ``retrieve`` + geração simulada por E/S.

    Abre o ``Store`` uma vez (leitura, seguro entre threads, segundo o contrato
    do núcleo) e o compartilha entre os workers. Devolve só um resumo por
    consulta, não o contexto, para não inflar a memória.
    """
    from genresearch.retrieve import retrieve  # import tardio: só a CLI real usa
    from genresearch.store import Store

    store = Store.open(Path(store_dir), vectors_mmap=vectors_mmap)

    def processar(consulta: str) -> dict:
        r = retrieve(store, consulta, k, max_context_bytes, read_mode=read_mode)
        t0 = time.perf_counter()
        if gen_ms > 0:
            time.sleep(gen_ms / 1000.0)  # geração simulada (E/S)
        return {"n_trechos": r.n_trechos, "bytes_contexto": r.bytes_contexto,
                "tempo_retrieve_s": r.tempo_s, "tempo_geracao_s": time.perf_counter() - t0}

    return processar


def _processador_demo(gen_ms: float) -> Callable[[str], dict]:
    def processar(consulta: str) -> dict:
        time.sleep(gen_ms / 1000.0)
        return {"demo": True}
    return processar


CONSULTAS_PADRAO = [
    "memória virtual e paginação", "diferença entre MemFree e MemAvailable",
    "cache de páginas do kernel", "leitura com mmap", "condição de corrida",
    "exclusão mútua com mutex", "produtores e consumidores", "starvation e deadlock",
    "swap e thrashing", "faltas de página maiores e menores",
]


def carregar_consultas(arquivo: Optional[Path], n: int) -> list[str]:
    """Lista determinística de ``n`` consultas (arquivo ou lista padrão repetida)."""
    base = CONSULTAS_PADRAO
    if arquivo is not None:
        base = [ln.strip() for ln in Path(arquivo).read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not base:
            raise ValueError(f"{arquivo} não tem consultas")
    return [base[i % len(base)] for i in range(n)]


# ----------------------------------------------------------------------------
# Exportação de resultados
# ----------------------------------------------------------------------------
CAMPOS_RESUMO = [
    "config", "rep", "timestamp", "workers", "producers", "queue_capacity",
    "consultas_total", "processadas", "erros", "rejeitadas", "tempo_total_s", "vazao_rps",
    "espera_media_s", "espera_mediana_s", "espera_p95_s", "proc_media_s", "proc_mediana_s",
    "proc_p95_s", "latencia_media_s", "latencia_mediana_s", "latencia_p95_s",
    "bloqueio_produtores_s", "fila_max", "fila_media", "utilizacao_media",
]


def linha_resumo(stats: dict, *, config: str = "", rep: int = 0, timestamp: str = "") -> dict:
    """Achata ``stats`` em uma linha de CSV (uma execução)."""
    return {
        "config": config, "rep": rep, "timestamp": timestamp,
        "workers": stats["workers"], "producers": stats["producers"],
        "queue_capacity": stats["queue_capacity"], "consultas_total": stats["consultas_total"],
        "processadas": stats["processadas"], "erros": stats["erros"],
        "rejeitadas": stats["rejeitadas"], "tempo_total_s": round(stats["tempo_total_s"], 6),
        "vazao_rps": round(stats["vazao_rps"], 4),
        "espera_media_s": round(stats["espera_s"]["media"], 6),
        "espera_mediana_s": round(stats["espera_s"]["mediana"], 6),
        "espera_p95_s": round(stats["espera_s"]["p95"], 6),
        "proc_media_s": round(stats["proc_s"]["media"], 6),
        "proc_mediana_s": round(stats["proc_s"]["mediana"], 6),
        "proc_p95_s": round(stats["proc_s"]["p95"], 6),
        "latencia_media_s": round(stats["latencia_s"]["media"], 6),
        "latencia_mediana_s": round(stats["latencia_s"]["mediana"], 6),
        "latencia_p95_s": round(stats["latencia_s"]["p95"], 6),
        "bloqueio_produtores_s": round(stats["bloqueio_produtores_s"], 6),
        "fila_max": stats["fila_max"], "fila_media": round(stats["fila_media"], 4),
        "utilizacao_media": round(stats["utilizacao_media"], 4),
    }


def anexar_resumo_csv(caminho: Path, stats: dict, *, config: str = "", rep: int = 0,
                      timestamp: str = "") -> None:
    """Acrescenta uma linha ao CSV de resumo (cria o cabeçalho se preciso)."""
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    novo = not caminho.exists()
    with open(caminho, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_RESUMO)
        if novo:
            w.writeheader()
        w.writerow(linha_resumo(stats, config=config, rep=rep, timestamp=timestamp))


def gravar_requisicoes_csv(caminho: Path, stats: dict) -> None:
    """Grava uma linha por consulta (dados brutos)."""
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    campos = ["id", "consulta", "produtor", "worker", "bloqueio_put_s", "espera_s",
              "proc_s", "ok", "erro", "tentativas"]
    with open(caminho, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        for r in sorted(stats["requisicoes"], key=lambda x: x["id"]):
            w.writerow({"id": r["id"], "consulta": r["consulta"], "produtor": r["produtor"],
                        "worker": r["worker"], "bloqueio_put_s": r["bloqueio_put_s"],
                        "espera_s": r["espera_s"], "proc_s": r["proc_s"], "ok": r["ok"],
                        "erro": r["erro"], "tentativas": r["tentativas"]})


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m genresearch.server",
                                 description="Executa consultas por fila limitada + workers.")
    ap.add_argument("--store", type=Path, help="diretório do store (obrigatório sem --demo)")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--producers", type=int, default=2)
    ap.add_argument("--queue-capacity", type=int, default=4)
    ap.add_argument("--n-queries", type=int, default=40)
    ap.add_argument("--queries-file", type=Path, help="uma consulta por linha")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--max-context-bytes", type=int, default=8192)
    ap.add_argument("--gen-ms", type=float, default=20.0, help="custo simulado de geração (E/S)")
    ap.add_argument("--read-mode", choices=["read", "mmap"], default=None)
    ap.add_argument("--vectors-mmap", action="store_true")
    ap.add_argument("--retries", type=int, default=0)
    ap.add_argument("--put-timeout", type=float, default=None,
                    help="segundos; rejeita se a fila continuar cheia (C6)")
    ap.add_argument("--config", default="", help="rótulo da configuração, ex.: C3_w2")
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--csv", type=Path, help="anexa uma linha de resumo a este CSV")
    ap.add_argument("--requests-csv", type=Path, help="grava o detalhe por consulta")
    ap.add_argument("--log-file", type=Path)
    ap.add_argument("--demo", action="store_true",
                    help="processador só com sleep, SEM núcleo: valida a fila; não grava CSV")
    a = ap.parse_args(argv)

    logging.basicConfig(filename=str(a.log_file) if a.log_file else None, level=logging.INFO,
                        format="%(asctime)s %(threadName)s %(levelname)s %(message)s")
    if a.demo and (a.csv or a.requests_csv):
        print("erro: --demo não grava CSV (dados simulados não entram em resultados/)", file=sys.stderr)
        return 2
    if not a.demo and a.store is None:
        print("erro: informe --store (ou use --demo)", file=sys.stderr)
        return 2

    consultas = carregar_consultas(a.queries_file, a.n_queries)
    proc = (_processador_demo(a.gen_ms) if a.demo else
            construir_processador(a.store, k=a.k, max_context_bytes=a.max_context_bytes,
                                  gen_ms=a.gen_ms, read_mode=a.read_mode,
                                  vectors_mmap=a.vectors_mmap))
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"executando {len(consultas)} consultas: {a.workers} worker(s), "
          f"{a.producers} produtor(es), fila {a.queue_capacity}", file=sys.stderr)
    _, stats = run_queries(proc, consultas, workers=a.workers, producers=a.producers,
                           queue_capacity=a.queue_capacity, retries=a.retries,
                           put_timeout=a.put_timeout)

    if a.csv:
        anexar_resumo_csv(a.csv, stats, config=a.config, rep=a.rep, timestamp=ts)
    if a.requests_csv:
        gravar_requisicoes_csv(a.requests_csv, stats)

    saida = {k: v for k, v in stats.items() if k != "requisicoes"}
    saida.update(config=a.config, rep=a.rep, timestamp=ts, cpu_count=os.cpu_count(),
                 python=sys.version.split()[0], demo=a.demo)
    print(json.dumps(saida, ensure_ascii=False))
    return 0 if stats["erros"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
