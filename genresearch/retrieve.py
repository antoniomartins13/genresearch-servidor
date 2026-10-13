"""Recuperação: similaridade por produto interno e montagem de contexto.

Com vetores L2-normalizados, cosseno = produto interno: a busca é um único
matvec (N x D) @ (D,) em numpy, sem laço Python. O top-k é ordenado por
(score desc, chunk_id asc), o que torna o resultado determinístico mesmo
com empates.

Montagem do contexto: trechos em ordem de score; o primeiro que não cabe
inteiro em `max_context_bytes` é truncado no limite e encerra a montagem.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .ingest import embed
from .store import Store


@dataclass(frozen=True)
class RetrievedChunk:
    """Trecho recuperado; `text` pode estar truncado (ver `truncado`)."""

    chunk_id: int
    doc: str
    offset: int
    length: int
    score: float
    text: str
    truncado: bool


@dataclass
class RetrieveResult:
    """Resultado e medições de uma consulta (tempos via perf_counter)."""

    chunks: list[RetrievedChunk] = field(default_factory=list)
    n_trechos: int = 0
    bytes_contexto: int = 0
    tempo_s: float = 0.0
    contexto: str = ""
    tempo_embedding_s: float = 0.0
    tempo_busca_s: float = 0.0
    tempo_montagem_s: float = 0.0
    k: int = 0
    max_context_bytes: int = 0


def _top_k(scores: np.ndarray, k: int) -> np.ndarray:
    """Índices dos k maiores scores, ordenados por (score desc, índice asc)."""
    k = min(k, scores.shape[0])
    if k <= 0:
        return np.empty(0, dtype=np.int64)
    kth = np.partition(scores, scores.shape[0] - k)[scores.shape[0] - k]
    above = np.flatnonzero(scores > kth)
    ties = np.flatnonzero(scores == kth)[: k - above.shape[0]]
    cand = np.concatenate((above, ties))
    return cand[np.lexsort((cand, -scores[cand]))]


def retrieve(store: Store, query: str, k: int, max_context_bytes: int) -> RetrieveResult:
    """Recupera até k trechos e monta um contexto de até max_context_bytes.

    Consulta sem nenhum token produz resultado vazio (não há similaridade a
    medir).
    """
    if k <= 0 or max_context_bytes <= 0:
        raise ValueError("k e max_context_bytes devem ser > 0")
    t0 = time.perf_counter()
    q = embed([query], dim=store.dim)[0]
    t1 = time.perf_counter()
    if not q.any():
        ids = np.empty(0, dtype=np.int64)
        scores = np.empty(0, dtype=np.float32)
    else:
        scores = store.vectors @ q
        ids = _top_k(scores, k)
    t2 = time.perf_counter()

    chunks: list[RetrievedChunk] = []
    parts: list[str] = []
    used = 0
    for rec in store.fetch_chunks(ids.tolist()):
        room = max_context_bytes - used
        if room <= 0:
            break
        raw = rec.text.encode("utf-8")
        truncado = len(raw) > room
        if truncado:
            raw = raw[:room]
        text = raw.decode("utf-8", errors="ignore")
        used += len(text.encode("utf-8"))
        parts.append(text)
        chunks.append(RetrievedChunk(chunk_id=rec.chunk_id, doc=rec.doc,
                                     offset=rec.offset, length=rec.length,
                                     score=float(scores[rec.chunk_id]),
                                     text=text, truncado=truncado))
        if truncado:
            break
    t3 = time.perf_counter()

    return RetrieveResult(
        chunks=chunks,
        n_trechos=len(chunks),
        bytes_contexto=used,
        tempo_s=t3 - t0,
        contexto="".join(parts),
        tempo_embedding_s=t1 - t0,
        tempo_busca_s=t2 - t1,
        tempo_montagem_s=t3 - t2,
        k=k,
        max_context_bytes=max_context_bytes,
    )
