"""Testes da frente Fila e workers. Rodar: python -m unittest discover -s tests -v"""
import os
import threading
import time
import unittest
from pathlib import Path

from genresearch.server import run_queries, carregar_consultas, linha_resumo, CAMPOS_RESUMO


def _dorme(ms):
    def p(q):
        time.sleep(ms / 1000.0)
        return q
    return p


class TestFila(unittest.TestCase):
    def test_processa_tudo_na_ordem(self):
        qs = [f"q{i}" for i in range(20)]
        res, s = run_queries(lambda q: q.upper(), qs, workers=2, producers=2, queue_capacity=4)
        self.assertEqual(res, [q.upper() for q in qs])
        self.assertEqual((s["processadas"], s["erros"], s["rejeitadas"]), (20, 0, 0))

    def test_1_2_4_workers(self):
        qs = list(range(16))
        for w in (1, 2, 4):
            res, s = run_queries(lambda q: q * 2, qs, workers=w, producers=2, queue_capacity=4)
            self.assertEqual(res, [q * 2 for q in qs], f"falhou com {w} workers")
            self.assertEqual(sum(s["consultas_por_worker"]), 16)
            self.assertEqual(len(s["consultas_por_worker"]), w)

    def test_fila_respeita_capacidade(self):
        _, s = run_queries(_dorme(5), list(range(30)), workers=1, producers=2, queue_capacity=3)
        self.assertLessEqual(s["fila_max"], 3)

    def test_erro_nao_derruba_workers(self):
        def p(q):
            if q % 5 == 0:
                raise RuntimeError("falha simulada")
            return q
        res, s = run_queries(p, list(range(20)), workers=2, producers=2, queue_capacity=4)
        self.assertEqual((s["erros"], s["processadas"]), (4, 16))
        self.assertIsNone(res[0])
        self.assertEqual(res[1], 1)

    def test_retentativa(self):
        tent, lock = {}, threading.Lock()
        def p(q):
            with lock:
                tent[q] = tent.get(q, 0) + 1
                n = tent[q]
            if n < 2:
                raise RuntimeError("1a falha")
            return q
        _, s = run_queries(p, list(range(6)), workers=2, producers=1, queue_capacity=2, retries=1)
        self.assertEqual((s["processadas"], s["erros"]), (6, 0))

    def test_produtor_bloqueia_com_fila_cheia(self):
        _, s = run_queries(_dorme(30), list(range(10)), workers=1, producers=2, queue_capacity=1)
        self.assertGreater(s["bloqueio_produtores_s"], 0.05)

    def test_rejeicao_por_fila_saturada(self):  # C6
        _, s = run_queries(_dorme(100), list(range(12)), workers=1, producers=2,
                           queue_capacity=1, put_timeout=0.01)
        self.assertGreater(s["rejeitadas"], 0)
        self.assertEqual(s["processadas"] + s["rejeitadas"] + s["erros"], 12)

    def test_workers_concorrentes(self):
        _, s = run_queries(_dorme(30), list(range(16)), workers=4, producers=2, queue_capacity=8)
        self.assertGreater(len({r["worker"] for r in s["requisicoes"]}), 1)

    def test_mais_workers_reduz_tempo_com_es(self):
        qs = list(range(24))
        _, s1 = run_queries(_dorme(20), qs, workers=1, producers=2, queue_capacity=4)
        _, s4 = run_queries(_dorme(20), qs, workers=4, producers=2, queue_capacity=4)
        self.assertLess(s4["tempo_total_s"], s1["tempo_total_s"])

    def test_lista_vazia(self):
        res, s = run_queries(lambda q: q, [], workers=2, producers=2, queue_capacity=2)
        self.assertEqual((res, s["processadas"]), ([], 0))

    def test_parametros_invalidos(self):
        with self.assertRaises(ValueError):
            run_queries(lambda q: q, [1], workers=0)

    def test_linha_resumo_tem_todos_os_campos(self):
        _, s = run_queries(lambda q: q, list(range(4)), workers=2, producers=2, queue_capacity=2)
        self.assertEqual(set(linha_resumo(s, config="X", rep=1)), set(CAMPOS_RESUMO))

    def test_consultas_deterministicas(self):
        self.assertEqual(carregar_consultas(None, 25), carregar_consultas(None, 25))
        self.assertEqual(len(carregar_consultas(None, 25)), 25)


@unittest.skipUnless(Path("data/store/meta.sqlite").exists(),
                     "integração: requer data/store (rode gen-corpus + ingest)")
class TestIntegracaoNucleo(unittest.TestCase):
    def test_fila_com_retrieve_real(self):
        from genresearch.server import construir_processador
        proc = construir_processador(Path("data/store"), k=3, max_context_bytes=2048, gen_ms=1)
        qs = carregar_consultas(None, 12)
        for w in (1, 2, 4):
            res, s = run_queries(proc, qs, workers=w, producers=2, queue_capacity=4)
            self.assertEqual(s["erros"], 0, f"erros com {w} workers: {s['requisicoes']}")
            self.assertEqual(s["processadas"], 12)
            self.assertTrue(all(r["n_trechos"] >= 0 for r in res))


if __name__ == "__main__":
    unittest.main()
