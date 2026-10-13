"""Teste ponta a ponta do núcleo: corpus -> ingest (read e mmap) -> query.

Roda com `python -m unittest discover -s tests -v` (ou pytest).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from genresearch.corpus import generate_corpus
from genresearch.ingest import count_chunks, ingest
from genresearch.retrieve import retrieve
from genresearch.store import VECTORS_FILE, Store

ROOT = Path(__file__).resolve().parent.parent
PARAMS = dict(chunk_size=2048, overlap=128)
QUERY_K, QUERY_BUDGET = 5, 8192


def _digest(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.glob("*.txt"))}


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.corpus = cls.tmp / "corpus"
        generate_corpus(cls.corpus, docs=6, size_mb=1.5, seed=7)
        # Consulta = trecho real do corpus, para ter similaridade garantida.
        cls.query = (cls.corpus / "doc_00003.txt").read_bytes()[5000:5300].decode()

        cls.stats, cls.results = {}, {}
        for mode in ("read", "mmap"):
            store_dir = cls.tmp / f"store_{mode}"
            store = Store.create(store_dir, dim=512)
            try:
                cls.stats[mode] = ingest(cls.corpus, store, read_mode=mode,
                                         batch_size=64, **PARAMS)
            finally:
                store.close()
            store = Store.open(store_dir)
            try:
                cls.results[mode] = retrieve(store, cls.query, QUERY_K, QUERY_BUDGET)
            finally:
                store.close()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_corpus_reprodutivel(self) -> None:
        again = self.tmp / "corpus_again"
        generate_corpus(again, docs=6, size_mb=1.5, seed=7)
        self.assertEqual(_digest(self.corpus), _digest(again))
        other = self.tmp / "corpus_seed8"
        generate_corpus(other, docs=6, size_mb=1.5, seed=8)
        self.assertNotEqual(_digest(self.corpus), _digest(other))

    def test_ingest_contagens(self) -> None:
        sizes = [p.stat().st_size for p in sorted(self.corpus.glob("*.txt"))]
        expected = sum(count_chunks(s, **PARAMS) for s in sizes)
        for mode, st in self.stats.items():
            with self.subTest(mode=mode):
                self.assertEqual(st.n_documentos, 6)
                self.assertEqual(st.n_chunks, expected)
                self.assertEqual(st.tamanho_corpus_bytes, round(1.5 * (1 << 20)))
                self.assertGreater(st.tamanho_indice_bytes, 0)
                self.assertGreater(st.bytes_lidos, 0)
        # read lê cada arquivo uma vez; mmap copia as fatias, sobreposição inclusa.
        self.assertEqual(self.stats["read"].bytes_lidos, self.stats["read"].tamanho_corpus_bytes)
        self.assertGreater(self.stats["mmap"].bytes_lidos, self.stats["mmap"].tamanho_corpus_bytes)

    def test_vetores_identicos_entre_modos(self) -> None:
        a = np.load(self.tmp / "store_read" / VECTORS_FILE)
        b = np.load(self.tmp / "store_mmap" / VECTORS_FILE)
        self.assertEqual(a.dtype, np.float32)
        self.assertTrue(a.flags.c_contiguous)
        np.testing.assert_allclose(np.linalg.norm(a, axis=1), 1.0, rtol=1e-5)
        self.assertTrue(np.array_equal(a, b))

    def test_consulta_nao_vazia_e_igual_entre_modos(self) -> None:
        r, m = self.results["read"], self.results["mmap"]
        self.assertGreater(r.n_trechos, 0)
        self.assertGreater(r.bytes_contexto, 0)
        self.assertLessEqual(r.bytes_contexto, QUERY_BUDGET)
        self.assertEqual(r.bytes_contexto, len(r.contexto.encode()))
        self.assertEqual(r.chunks[0].doc, "doc_00003.txt")
        self.assertEqual({c.chunk_id for c in r.chunks}, {c.chunk_id for c in m.chunks})

    def test_vectors_mmap_mesmo_resultado(self) -> None:
        store = Store.open(self.tmp / "store_read", vectors_mmap=True)
        try:
            self.assertIsInstance(store.vectors, np.memmap)
            res = retrieve(store, self.query, QUERY_K, QUERY_BUDGET)
        finally:
            store.close()
        self.assertEqual([c.chunk_id for c in res.chunks],
                         [c.chunk_id for c in self.results["read"].chunks])

    def test_cli_json_em_stdout(self) -> None:
        cmd = [sys.executable, "-m", "genresearch", "query",
               "--store", str(self.tmp / "store_read"), "--q", self.query, "--k", "3"]
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True)
        lines = out.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertGreater(data["n_trechos"], 0)
        self.assertNotIn("contexto", data)


if __name__ == "__main__":
    unittest.main()
