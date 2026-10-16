"""Testes de cache.py e logging_.py (frente Cache e sincronização)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from genresearch.cache import SharedCache, make_key
from genresearch.logging_ import EventLog, Evento, verify_log


def _run(n: int, fn) -> None:
    b = threading.Barrier(n)

    def w(i: int) -> None:
        b.wait()
        fn(i)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


class CacheSequencial(unittest.TestCase):
    def test_make_key_normaliza_e_separa_parametros(self) -> None:
        self.assertEqual(make_key("  Olá   Mundo ", 5, 8192), make_key("olá mundo", 5, 8192))
        self.assertNotEqual(make_key("a", 5, 8192), make_key("a", 3, 8192))
        self.assertNotEqual(make_key("a", 5, 8192), make_key("b", 5, 8192))

    def test_lru_remove_o_menos_recente(self) -> None:
        c = SharedCache(2, policy="lru")
        c.put("a", 1); c.put("b", 2)
        c.get("a")           # a fica mais recente
        c.put("c", 3)        # remove b
        self.assertEqual(c.get("a"), 1)
        self.assertIsNone(c.get("b"))
        self.assertEqual(c.stats().substituicoes, 1)

    def test_fifo_ignora_acessos(self) -> None:
        c = SharedCache(2, policy="fifo")
        c.put("a", 1); c.put("b", 2)
        c.get("a")
        c.put("c", 3)        # remove a (o mais antigo)
        self.assertIsNone(c.get("a"))
        self.assertEqual(c.get("b"), 2)

    def test_get_or_compute_conta_e_economiza_tempo(self) -> None:
        c = SharedCache(4)
        calls = []
        v1, h1 = c.get_or_compute("k", lambda: calls.append(1) or "v")
        v2, h2 = c.get_or_compute("k", lambda: calls.append(1) or "v")
        self.assertEqual((v1, h1, v2, h2), ("v", False, "v", True))
        self.assertEqual(len(calls), 1)
        s = c.stats()
        self.assertEqual((s.acertos, s.erros, s.calculos), (1, 1, 1))
        self.assertAlmostEqual(s.taxa_acerto, 0.5)
        self.assertGreaterEqual(s.tempo_economizado_s, 0.0)

    def test_excecao_propaga_e_nao_guarda(self) -> None:
        c = SharedCache(2, single_flight=True)
        with self.assertRaises(ValueError):
            c.get_or_compute("k", lambda: (_ for _ in ()).throw(ValueError("x")))
        self.assertEqual(len(c), 0)
        self.assertEqual(c.get_or_compute("k", lambda: 7), (7, False))

    def test_parametros_invalidos(self) -> None:
        with self.assertRaises(ValueError):
            SharedCache(0)
        with self.assertRaises(ValueError):
            SharedCache(2, policy="x")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SharedCache(2, protected=False, single_flight=True)


class CacheConcorrente(unittest.TestCase):
    def test_protegido_mantem_contadores_e_capacidade(self) -> None:
        c = SharedCache(8, protected=True, race_window_s=0.0)
        n, ops = 6, 150

        def w(i: int) -> None:
            for j in range(ops):
                c.get_or_compute(f"k{(i + j) % 20}", lambda: "v")

        _run(n, w)
        s = c.stats()
        self.assertEqual(s.acertos + s.erros, n * ops)
        self.assertLessEqual(s.tamanho_max_observado, 8)
        self.assertEqual(s.falhas_concorrencia, 0)

    def test_single_flight_calcula_uma_vez_por_chave(self) -> None:
        c = SharedCache(8, single_flight=True)
        calls = []
        lock = threading.Lock()

        def compute() -> str:
            with lock:
                calls.append(1)
            threading.Event().wait(0.05)
            return "v"

        _run(8, lambda i: c.get_or_compute("mesma", compute))
        self.assertEqual(len(calls), 1)
        s = c.stats()
        self.assertEqual(s.calculos, 1)
        self.assertEqual(s.coalescidas, 7)


class LogTest(unittest.TestCase):
    def test_modos_protegidos_nao_corrompem(self) -> None:
        for mode in ("lock", "queue"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                p = Path(d) / "x.jsonl"
                log = EventLog(p, mode=mode, race_window_s=0.0)
                _run(6, lambda i: [log.log(Evento.CONSULTA, w=i, j=j) for j in range(100)])
                log.log(Evento.ENCERRAMENTO)
                log.close()
                chk = verify_log(p, esperadas=601)
                self.assertEqual(chk.corrompidas, 0)
                self.assertEqual(chk.validas, 601)
                self.assertEqual(chk.registros_perdidos, 0)
                self.assertEqual(chk.duplicadas, 0)
                self.assertEqual(chk.fora_de_ordem, 0)

    def test_verify_detecta_corrupcao_duplicata_e_perda(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            ok = lambda s: json.dumps({"seq": s, "t": 0, "thr": "t", "evento": "e", "campos": {}})
            p.write_text("\n".join([ok(0), ok(1), ok(1), '{"seq":2,"t":0,{"seq":3', ok(0)]) + "\n")
            chk = verify_log(p, esperadas=5)
            self.assertEqual((chk.linhas, chk.validas, chk.corrompidas), (5, 4, 1))
            self.assertEqual(chk.duplicadas, 2)
            self.assertEqual(chk.fora_de_ordem, 1)
            self.assertEqual(chk.registros_perdidos, 3)

    def test_log_fechado_rejeita(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = EventLog(Path(d) / "x.jsonl", mode="lock")
            log.close()
            with self.assertRaises(RuntimeError):
                log.log(Evento.FALHA)


if __name__ == "__main__":
    unittest.main()
