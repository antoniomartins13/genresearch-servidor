"""Ingestão: chunking por janela fixa, embeddings por feature hashing e persistência.

Chunking: janela fixa de `chunk_size` bytes, avançando `chunk_size - overlap`
bytes por vez; o último chunk de cada documento pode ser menor. Sem
separador semântico: o tamanho dos chunks não depende do conteúdo.

Embedding: cada token ([a-z0-9]+, minúsculo) é hasheado com zlib.crc32 (nunca
hash(), que muda por processo via PYTHONHASHSEED). O hash escolhe a posição
(h % D) e o sinal (bit 31) no vetor; o vetor final é normalizado em L2, de
modo que cosseno = produto interno.
"""

from __future__ import annotations

import re
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

import numpy as np

from .config import DEFAULT_BATCH_SIZE, DEFAULT_DIM
from .reader import open_source
from .store import Store

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Chunk:
    """Fatia de um documento: bytes [offset, offset+length) decodificados."""

    doc: str
    seq: int
    offset: int
    length: int
    text: str


@dataclass
class IngestStats:
    """Medições de uma execução de ingest (tempos via perf_counter)."""

    n_documentos: int
    n_chunks: int
    bytes_lidos: int
    tamanho_corpus_bytes: int
    tamanho_indice_bytes: int
    tempo_total_s: float
    tempo_leitura_s: float
    tempo_embedding_s: float
    tempo_persistencia_s: float
    read_mode: str
    chunk_size: int
    overlap: int
    dim: int
    batch_size: int


def _check_window(chunk_size: int, overlap: int) -> None:
    if chunk_size <= 0 or not 0 <= overlap < chunk_size:
        raise ValueError(f"exige chunk_size > 0 e 0 <= overlap < chunk_size "
                         f"(chunk_size={chunk_size}, overlap={overlap})")


def count_chunks(size: int, *, chunk_size: int, overlap: int) -> int:
    """Número de chunks que iter_chunks produz para um arquivo de `size` bytes."""
    _check_window(chunk_size, overlap)
    if size <= 0:
        return 0
    if size <= chunk_size:
        return 1
    step = chunk_size - overlap
    return 1 + -(-(size - chunk_size) // step)


def iter_chunks(path: Path, *, chunk_size: int, overlap: int,
                read_mode: str) -> Generator[Chunk, None, int]:
    """Itera os chunks de `path`; ao terminar, retorna os bytes lidos da fonte.

    O valor de retorno sai em StopIteration.value; quem só itera com `for`
    pode ignorá-lo.
    """
    _check_window(chunk_size, overlap)
    step = chunk_size - overlap
    with open_source(path, read_mode) as src:
        offset, seq = 0, 0
        while offset < src.size:
            b = src.slice(offset, chunk_size)
            yield Chunk(doc=path.name, seq=seq, offset=offset, length=len(b),
                        text=b.decode("utf-8", errors="replace"))
            if offset + len(b) >= src.size:
                break
            offset += step
            seq += 1
        return src.bytes_lidos


def embed(texts: list[str], *, dim: int = DEFAULT_DIM) -> np.ndarray:
    """Vetores (N, dim) float32 L2-normalizados por feature hashing.

    Texto sem tokens produz vetor nulo (norma 0).
    """
    if dim <= 0:
        raise ValueError("dim deve ser > 0")
    n = len(texts)
    token_lists = [_TOKEN.findall(t.lower()) for t in texts]
    counts = np.fromiter((len(t) for t in token_lists), dtype=np.int64, count=n)
    all_tokens = [tok for toks in token_lists for tok in toks]

    # code = 2*slot + bit de sinal; crc32 calculado uma vez por token distinto.
    codes_of = {tok: (h % dim) * 2 + (h >> 31)
                for tok in set(all_tokens)
                for h in (zlib.crc32(tok.encode("utf-8")),)}
    codes = np.fromiter(map(codes_of.__getitem__, all_tokens), dtype=np.int64,
                        count=len(all_tokens))
    keys = np.repeat(np.arange(n, dtype=np.int64) * (2 * dim), counts) + codes
    hist = np.bincount(keys, minlength=n * 2 * dim).reshape(n, dim, 2)

    vecs = (hist[:, :, 1] - hist[:, :, 0]).astype(np.float64)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    np.divide(vecs, norms, out=vecs, where=norms > 0)
    return vecs.astype(np.float32)


def ingest(corpus_dir: Path, store: Store, *, chunk_size: int, overlap: int,
           read_mode: str, batch_size: int = DEFAULT_BATCH_SIZE) -> IngestStats:
    """Ingere todos os *.txt de `corpus_dir` (em ordem de nome) em `store`.

    chunk_id = posição global do chunk = linha em vectors.npy. A dimensão
    dos vetores vem de `store.dim`.
    """
    _check_window(chunk_size, overlap)
    if batch_size <= 0:
        raise ValueError("batch_size deve ser > 0")
    t_start = time.perf_counter()

    paths = sorted(corpus_dir.glob("*.txt"))
    if not paths:
        raise FileNotFoundError(f"nenhum .txt em {corpus_dir}")
    sizes = [p.stat().st_size for p in paths]
    expected = [count_chunks(s, chunk_size=chunk_size, overlap=overlap) for s in sizes]
    n_total = sum(expected)

    t_read = t_embed = t_persist = 0.0
    bytes_lidos = 0
    next_id = 0
    batch: list[tuple[int, Chunk]] = []

    t = time.perf_counter()
    writer = store.vector_writer(n_total)
    t_persist += time.perf_counter() - t

    def flush() -> None:
        nonlocal t_embed, t_persist, next_id
        if not batch:
            return
        t0 = time.perf_counter()
        vecs = embed([c.text for _, c in batch], dim=store.dim)
        t1 = time.perf_counter()
        writer.write(vecs)
        store.add_chunks([(next_id + i, doc_id, c.seq, c.offset, c.length, c.text)
                          for i, (doc_id, c) in enumerate(batch)])
        t2 = time.perf_counter()
        t_embed += t1 - t0
        t_persist += t2 - t1
        next_id += len(batch)
        batch.clear()

    for path, size, n_exp in zip(paths, sizes, expected):
        t = time.perf_counter()
        doc_id = store.add_document(path.name, size)
        t_persist += time.perf_counter() - t

        it = iter_chunks(path, chunk_size=chunk_size, overlap=overlap, read_mode=read_mode)
        n_doc = 0
        while True:
            t = time.perf_counter()
            try:
                chunk = next(it)
            except StopIteration as stop:
                t_read += time.perf_counter() - t
                bytes_lidos += stop.value
                break
            t_read += time.perf_counter() - t
            batch.append((doc_id, chunk))
            n_doc += 1
            if len(batch) >= batch_size:
                flush()
        if n_doc != n_exp:
            raise RuntimeError(f"{path.name}: {n_doc} chunks, esperado {n_exp} "
                               "(arquivo mudou durante a ingestão?)")
    flush()

    t = time.perf_counter()
    writer.finish()
    for key, value in (("chunk_size", chunk_size), ("overlap", overlap),
                       ("read_mode", read_mode), ("n_chunks", n_total),
                       ("n_documentos", len(paths))):
        store.set_meta(key, value)
    store.commit()
    t_persist += time.perf_counter() - t

    return IngestStats(
        n_documentos=len(paths),
        n_chunks=n_total,
        bytes_lidos=bytes_lidos,
        tamanho_corpus_bytes=sum(sizes),
        tamanho_indice_bytes=store.size_bytes(),
        tempo_total_s=time.perf_counter() - t_start,
        tempo_leitura_s=t_read,
        tempo_embedding_s=t_embed,
        tempo_persistencia_s=t_persist,
        read_mode=read_mode,
        chunk_size=chunk_size,
        overlap=overlap,
        dim=store.dim,
        batch_size=batch_size,
    )
