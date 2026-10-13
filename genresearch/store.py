"""Persistência do índice: SQLite para metadados e texto, .npy para vetores.

Layout de um diretório de store:
    meta.sqlite   tabelas docs, chunks (com texto) e meta (parâmetros)
    vectors.npy   matriz N x D float32 contígua, linha i = chunk_id i

Os vetores ficam fora do SQLite (nada de BLOB) para que o arquivo possa ser
aberto com np.load(mmap_mode='r'). A escrita é em streaming: o cabeçalho é
gravado com o shape final e os lotes seguem por write() comum, sem acumular
a matriz em memória.

Uma Store aberta para leitura pode ser usada por várias threads: cada thread
recebe sua própria conexão SQLite (threading.local) e a matriz de vetores é
um array numpy somente leitura compartilhado.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from .config import DEFAULT_DIM

META_FILE = "meta.sqlite"
VECTORS_FILE = "vectors.npy"
VECTOR_DTYPE = np.dtype("<f4")

_SCHEMA = """
CREATE TABLE docs (
    doc_id     INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL
);
CREATE TABLE chunks (
    chunk_id INTEGER PRIMARY KEY,
    doc_id   INTEGER NOT NULL REFERENCES docs(doc_id),
    seq      INTEGER NOT NULL,
    offset   INTEGER NOT NULL,
    length   INTEGER NOT NULL,
    text     TEXT NOT NULL
);
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ChunkRecord:
    """Chunk persistido, como lido do SQLite."""

    chunk_id: int
    doc: str
    offset: int
    length: int
    text: str


class VectorWriter:
    """Grava vectors.npy em streaming; o número de linhas é fixado na criação."""

    def __init__(self, path: Path, n_rows: int, dim: int) -> None:
        self._final = path
        self._tmp = path.with_name(path.name + ".tmp")
        self.n_rows, self.dim, self.written = n_rows, dim, 0
        self._f = open(self._tmp, "wb")
        np.lib.format.write_array_header_1_0(self._f, {
            "descr": np.lib.format.dtype_to_descr(VECTOR_DTYPE),
            "fortran_order": False,
            "shape": (n_rows, dim),
        })

    def write(self, batch: np.ndarray) -> None:
        """Acrescenta um lote (B, D) ao final do arquivo."""
        if batch.ndim != 2 or batch.shape[1] != self.dim:
            raise ValueError(f"lote com shape {batch.shape}, esperado (B, {self.dim})")
        if self.written + batch.shape[0] > self.n_rows:
            raise ValueError("mais linhas do que o declarado no cabeçalho")
        self._f.write(np.ascontiguousarray(batch, dtype=VECTOR_DTYPE))
        self.written += batch.shape[0]

    def finish(self) -> None:
        """Fecha, confere a contagem de linhas e publica o arquivo final."""
        self._f.close()
        if self.written != self.n_rows:
            raise RuntimeError(f"gravadas {self.written} linhas, cabeçalho diz {self.n_rows}")
        os.replace(self._tmp, self._final)


class Store:
    """Índice em disco: metadados no SQLite, vetores em .npy."""

    def __init__(self, root: Path, dim: int, *, writable: bool,
                 vectors: np.ndarray | None = None) -> None:
        self.root = root
        self.dim = dim
        self.writable = writable
        self._vectors = vectors
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._write_conn: sqlite3.Connection | None = None

    # ---- criação / abertura -------------------------------------------

    @classmethod
    def create(cls, root: Path, *, dim: int = DEFAULT_DIM) -> "Store":
        """Cria um store vazio em `root`, apagando um índice anterior ali."""
        root.mkdir(parents=True, exist_ok=True)
        for name in (META_FILE, META_FILE + "-journal", VECTORS_FILE, VECTORS_FILE + ".tmp"):
            (root / name).unlink(missing_ok=True)
        store = cls(root, dim, writable=True)
        conn = sqlite3.connect(root / META_FILE)
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO meta VALUES ('dim', ?)", (str(dim),))
        conn.commit()
        store._write_conn = conn
        return store

    @classmethod
    def open(cls, root: Path, *, vectors_mmap: bool = False) -> "Store":
        """Abre um store existente para leitura; vetores via np.load."""
        meta_path = root / META_FILE
        if not meta_path.exists():
            raise FileNotFoundError(f"store não encontrado: {meta_path}")
        conn = sqlite3.connect(f"file:{meta_path}?mode=ro", uri=True)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
        finally:
            conn.close()
        dim = int(meta["dim"])
        vectors = np.load(root / VECTORS_FILE, mmap_mode="r" if vectors_mmap else None)
        if vectors.dtype != VECTOR_DTYPE or vectors.ndim != 2 or vectors.shape[1] != dim:
            raise ValueError(f"vectors.npy inconsistente: {vectors.dtype} {vectors.shape}, dim={dim}")
        if "n_chunks" in meta and vectors.shape[0] != int(meta["n_chunks"]):
            raise ValueError("vectors.npy e meta.sqlite discordam no número de chunks")
        return cls(root, dim, writable=False, vectors=vectors)

    # ---- escrita (usada por ingest) -----------------------------------

    def _wconn(self) -> sqlite3.Connection:
        if self._write_conn is None:
            raise RuntimeError("store aberto somente para leitura")
        return self._write_conn

    def add_document(self, name: str, size_bytes: int) -> int:
        """Registra um documento e devolve seu doc_id."""
        cur = self._wconn().execute(
            "INSERT INTO docs (name, size_bytes) VALUES (?, ?)", (name, size_bytes))
        return int(cur.lastrowid)

    def add_chunks(self, rows: Sequence[tuple[int, int, int, int, int, str]]) -> None:
        """Insere chunks como (chunk_id, doc_id, seq, offset, length, text)."""
        self._wconn().executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)", rows)

    def set_meta(self, key: str, value: object) -> None:
        """Grava um parâmetro do índice na tabela meta."""
        self._wconn().execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def vector_writer(self, n_rows: int) -> VectorWriter:
        """Abre o gravador de vectors.npy com shape (n_rows, dim)."""
        self._wconn()
        return VectorWriter(self.root / VECTORS_FILE, n_rows, self.dim)

    def commit(self) -> None:
        """Efetiva as escritas pendentes no SQLite."""
        self._wconn().commit()

    # ---- leitura (usada por retrieve) ---------------------------------

    @property
    def vectors(self) -> np.ndarray:
        """Matriz N x D (somente leitura); disponível após Store.open."""
        if self._vectors is None:
            raise RuntimeError("vetores não carregados; use Store.open")
        return self._vectors

    def _rconn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(f"file:{self.root / META_FILE}?mode=ro", uri=True,
                                   check_same_thread=False)
            self._local.conn = conn
            self._conns.append(conn)
        return conn

    def fetch_chunks(self, ids: Sequence[int]) -> list[ChunkRecord]:
        """Busca chunks pelo id, devolvendo na mesma ordem de `ids`."""
        if not ids:
            return []
        ids = [int(i) for i in ids]
        marks = ",".join("?" * len(ids))
        rows = self._rconn().execute(
            "SELECT c.chunk_id, d.name, c.offset, c.length, c.text "
            f"FROM chunks c JOIN docs d USING (doc_id) WHERE c.chunk_id IN ({marks})",
            ids).fetchall()
        by_id = {r[0]: ChunkRecord(*r) for r in rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise KeyError(f"chunk_ids ausentes no índice: {missing}")
        return [by_id[i] for i in ids]

    # ---- geral --------------------------------------------------------

    def size_bytes(self) -> int:
        """Tamanho em disco do índice (meta.sqlite + vectors.npy)."""
        return sum((self.root / n).stat().st_size
                   for n in (META_FILE, VECTORS_FILE) if (self.root / n).exists())

    def close(self) -> None:
        """Fecha as conexões SQLite abertas por este store."""
        if self._write_conn is not None:
            self._write_conn.close()
            self._write_conn = None
        for c in self._conns:
            c.close()
        self._conns.clear()
