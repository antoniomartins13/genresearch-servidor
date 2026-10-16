"""Cache + log sobre a fila do Toni (server.run_queries), sem alterar server.py."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from genresearch.cache import SharedCache, cached_processor
from genresearch.logging_ import EventLog, verify_log
from genresearch.server import run_queries
from tools.run_c4 import gerar_workload


def _proc(ms: float = 0.0):
    def p(q):
        time.sleep(ms / 1000)
        return {"n_trechos": 1, "bytes_contexto": 10, "tempo_retrieve_s": 0.001,
                "tempo_geracao_s": 0.002}
    return p


class Integracao(unittest.TestCase):
    def test_um_worker_acertos_exatos(self) -> None:
        qs = gerar_workload(60, "repetida", distinct=6, seed=3)
        cache = SharedCache(8)
        res, s = run_queries(cached_processor(_proc(), cache, k=5, max_context_bytes=8192),
                             qs, workers=1, producers=2, queue_capacity=4)
        st = cache.stats()
        self.assertEqual((st.erros, st.acertos), (6, 54))
        self.assertEqual(s["processadas"], 60)
        self.assertEqual(sum(r["cache"] == "acerto" for r in res), 54)

    def test_distinta_nunca_acerta(self) -> None:
        qs = gerar_workload(30, "distinta", seed=2)
        cache = SharedCache(64)
        run_queries(cached_processor(_proc(), cache, k=5, max_context_bytes=8192),
                    qs, workers=2, producers=2, queue_capacity=4)
        self.assertEqual(cache.stats().acertos, 0)

    def test_single_flight_com_4_workers_calcula_uma_vez(self) -> None:
        calls, lock = [], threading.Lock()

        def p(q):
            with lock:
                calls.append(q)
            time.sleep(0.05)
            return {"x": 1}

        cache = SharedCache(4, single_flight=True)
        run_queries(cached_processor(p, cache, k=5, max_context_bytes=8192),
                    ["igual"] * 12, workers=4, producers=2, queue_capacity=4)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.stats().calculos, 1)

    def test_valor_guardado_nao_e_alterado_e_excecao_passa(self) -> None:
        cache = SharedCache(2)
        proc = cached_processor(_proc(), cache, k=5, max_context_bytes=8192)
        a = proc("q"); b = proc("q")
        self.assertEqual((a["cache"], b["cache"]), ("erro", "acerto"))
        from genresearch.cache import make_key
        self.assertNotIn("cache", cache.get(make_key("q", 5, 8192)))
        ruim = cached_processor(lambda q: 1 / 0, cache, k=5, max_context_bytes=8192)
        with self.assertRaises(ZeroDivisionError):
            ruim("outra")

    def test_log_registra_eventos_sem_corrupcao(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "e.jsonl"
            log = EventLog(p, mode="lock")
            cache = SharedCache(8)
            qs = gerar_workload(40, "mista", distinct=4, repeat_frac=0.8, seed=5)
            run_queries(cached_processor(_proc(), cache, k=5, max_context_bytes=8192, log=log),
                        qs, workers=4, producers=2, queue_capacity=4)
            n = log.stats().n_eventos
            log.close()
            chk = verify_log(p, esperadas=n)
            self.assertEqual((chk.corrompidas, chk.registros_perdidos, chk.fora_de_ordem), (0, 0, 0))
            self.assertGreaterEqual(n, 40 * 2)  # >= consulta + cache por consulta


class Workload(unittest.TestCase):
    def test_deterministico_e_formas(self) -> None:
        for kind in ("repetida", "distinta", "mista"):
            self.assertEqual(gerar_workload(50, kind, seed=9), gerar_workload(50, kind, seed=9))
        self.assertEqual(len(set(gerar_workload(50, "repetida", distinct=7))), 7)
        self.assertEqual(len(set(gerar_workload(50, "distinta"))), 50)
        m = gerar_workload(400, "mista", distinct=5, repeat_frac=0.5, seed=1)
        self.assertGreater(len(set(m)), 100)  # ~200 únicas + 5 quentes
        with self.assertRaises(ValueError):
            gerar_workload(5, "x")


if __name__ == "__main__":
    unittest.main()
