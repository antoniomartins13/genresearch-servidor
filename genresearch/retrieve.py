"""Recuperação: similaridade por produto interno e montagem de contexto.

Com vetores L2-normalizados, cosseno = produto interno: a busca é um único
matvec (N x D) @ (D,) em numpy, sem laço Python. O top-k é ordenado por
(score desc, chunk_id asc), o que torna o resultado determinístico mesmo
com empates.

Montagem do contexto: trechos em ordem de score; o primeiro que não cabe
inteiro em `max_context_bytes` é truncado no limite e encerra a montagem.
O store não guarda texto: os bytes de cada trecho são relidos do corpus via
reader.open_source, no read_mode da ingestão (ou no informado), lendo de
cada trecho só os bytes que entram no contexto. Assim a fase de consulta
também exercita read() contra mmap e depende do page cache.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .ingest import embed
from .reader import open_source
from .store import ChunkRecord, Store


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
    """Resultado e medições de uma consulta (tempos via perf_counter).

    `bytes_contexto` são os bytes do corpus que entraram no contexto;
    `bytes_lidos` são os bytes copiados da fonte pelo reader (no modo read,
    o documento inteiro de cada trecho).
    """

    chunks: list[RetrievedChunk] = field(default_factory=list)
    n_trechos: int = 0
    bytes_contexto: int = 0
    tempo_s: float = 0.0
    contexto: str = ""
    tempo_embedding_s: float = 0.0
    tempo_busca_s: float = 0.0
    tempo_leitura_s: float = 0.0
    tempo_montagem_s: float = 0.0
    bytes_lidos: int = 0
    read_mode: str = ""
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


def _plan(recs: list[ChunkRecord], budget: int) -> list[tuple[ChunkRecord, int]]:
    """Quantos bytes de cada trecho entram no contexto, em ordem de score."""
    plan: list[tuple[ChunkRecord, int]] = []
    for rec in recs:
        if budget <= 0:
            break
        take = min(rec.n_bytes, budget)
        plan.append((rec, take))
        budget -= take
        if take < rec.n_bytes:
            break
    return plan


def _read_plan(store: Store, plan: list[tuple[ChunkRecord, int]],
               mode: str) -> tuple[dict[int, bytes], int]:
    """Relê do corpus os bytes do plano, abrindo cada documento uma vez."""
    by_doc: dict[str, list[tuple[ChunkRecord, int]]] = {}
    for rec, take in plan:
        by_doc.setdefault(rec.doc_path, []).append((rec, take))
    raw: dict[int, bytes] = {}
    bytes_lidos = 0
    for doc, items in by_doc.items():
        with open_source(store.corpus_dir / doc, mode) as src:
            if src.size != items[0][0].doc_size_bytes:
                raise RuntimeError(f"{doc}: tamanho {src.size} difere do ingerido "
                                   f"({items[0][0].doc_size_bytes}); corpus alterado")
            for rec, take in items:
                raw[rec.chunk_id] = src.slice(rec.offset, take)
            bytes_lidos += src.bytes_lidos
    return raw, bytes_lidos


def retrieve(store: Store, query: str, k: int, max_context_bytes: int, *,
             read_mode: str | None = None) -> RetrieveResult:
    """Recupera até k trechos e monta um contexto de até max_context_bytes.

    `read_mode` padrão = o usado na ingestão (store.read_mode). Consulta sem
    nenhum token produz resultado vazio (não há similaridade a medir).
    """
    if k <= 0 or max_context_bytes <= 0:
        raise ValueError("k e max_context_bytes devem ser > 0")
    mode = read_mode or store.read_mode
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
    plan = _plan(store.fetch_chunks(ids.tolist()), max_context_bytes)
    t3 = time.perf_counter()
    raw, bytes_lidos = _read_plan(store, plan, mode)
    t4 = time.perf_counter()

    chunks: list[RetrievedChunk] = []
    for rec, take in plan:
        truncado = take < rec.n_bytes
        # Trecho inteiro: mesma decodificação da ingestão. Truncado: descarta
        # um eventual caractere multibyte cortado no limite.
        text = raw[rec.chunk_id].decode("utf-8", errors="ignore" if truncado else "replace")
        chunks.append(RetrievedChunk(chunk_id=rec.chunk_id, doc=rec.doc_path,
                                     offset=rec.offset, length=rec.length,
                                     score=float(scores[rec.chunk_id]),
                                     text=text, truncado=truncado))
    contexto = "".join(c.text for c in chunks)
    t5 = time.perf_counter()

    return RetrieveResult(
        chunks=chunks,
        n_trechos=len(chunks),
        bytes_contexto=sum(take for _, take in plan),
        tempo_s=t5 - t0,
        contexto=contexto,
        tempo_embedding_s=t1 - t0,
        tempo_busca_s=t2 - t1,
        tempo_leitura_s=t4 - t3,
        tempo_montagem_s=(t3 - t2) + (t5 - t4),
        bytes_lidos=bytes_lidos,
        read_mode=mode,
        k=k,
        max_context_bytes=max_context_bytes,
    )
