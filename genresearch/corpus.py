"""Gerador de corpus sintético determinístico.

O texto é ASCII puro (janelas de bytes nunca cortam caractere multibyte) e é
montado em blocos vetorizados com numpy: sorteia índices de palavras e copia
os bytes correspondentes de um buffer plano de vocabulário, sem laço por
caractere nem por palavra.

Mesmo seed + mesmos parâmetros + mesma versão do numpy => mesmo corpus byte
a byte. Cada documento tem seu próprio gerador, derivado de (seed, doc_id).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MIB = 1 << 20
BLOCK_BYTES = 4 * MIB      # saída gerada por bloco (limita memória temporária)
VOCAB_SIZE = 8192
N_TOPICS = 64
TOPIC_WORDS = 96           # palavras exclusivas de cada tópico
P_TOPIC = 0.25             # fração de palavras sorteadas do tópico do documento
P_SENTENCE_END = 1 / 12    # frequência do token ".\n"
ZIPF_S = 1.07

_SYLLABLES = [c + v for c in "bcdfglmnprstvz" for v in "aeiou"]


@dataclass
class CorpusStats:
    """Resultado de uma geração de corpus."""

    n_documentos: int
    bytes_escritos: int
    seed: int
    tempo_total_s: float
    numpy_version: str


def _rng(seed: int, *stream: int) -> np.random.Generator:
    # PCG64 explícito: não depende do que default_rng escolher no futuro.
    return np.random.Generator(np.random.PCG64([seed, *stream]))


class _Vocab:
    """Vocabulário em buffer plano de bytes + distribuição de sorteio."""

    def __init__(self, seed: int) -> None:
        rng = _rng(seed, 0)
        words: list[bytes] = []
        seen: set[bytes] = set()
        syl = np.array(_SYLLABLES)
        # Gera candidatos em lote; deduplica preservando ordem.
        while len(words) < VOCAB_SIZE:
            n_syl = rng.integers(1, 5, size=VOCAB_SIZE)
            picks = rng.integers(0, len(syl), size=(VOCAB_SIZE, 4))
            for k, row in zip(n_syl, picks):
                w = "".join(syl[row[:k]]).encode("ascii")
                if w not in seen:
                    seen.add(w)
                    words.append(w)
                    if len(words) == VOCAB_SIZE:
                        break

        tokens = [w + b" " for w in words] + [b".\n"]
        self.end_token = len(tokens) - 1
        self.lens = np.array([len(t) for t in tokens], dtype=np.int64)
        self.starts = np.concatenate(([0], np.cumsum(self.lens)[:-1]))
        self.flat = np.frombuffer(b"".join(tokens), dtype=np.uint8)
        # Todo byte do corpus sai deste buffer. Corpus ASCII garante que uma
        # fatia por offset de byte (ingest e retrieve) nunca corta uma
        # sequência UTF-8. Verificação explícita: sobrevive a python -O.
        if int(self.flat.max()) >= 0x80:
            raise AssertionError("vocabulário com byte não ASCII")

        # Distribuição global Zipf; tópicos usam faixas da cauda do vocabulário.
        w = 1.0 / np.arange(1, VOCAB_SIZE + 1, dtype=np.float64) ** ZIPF_S
        self.cdf = np.cumsum(w / w.sum())
        self.cdf[-1] = 1.0
        self.topic_base = VOCAB_SIZE - N_TOPICS * TOPIC_WORDS

        p = w / w.sum()
        self.mean_len = float(
            (1 - P_SENTENCE_END) * ((1 - P_TOPIC) * (p @ self.lens[:VOCAB_SIZE])
                                    + P_TOPIC * self.lens[self.topic_base:VOCAB_SIZE].mean())
            + P_SENTENCE_END * self.lens[self.end_token]
        )

    def sample(self, rng: np.random.Generator, topic: int, n: int) -> np.ndarray:
        """Sorteia n índices de token para um documento do tópico dado."""
        idx = np.searchsorted(self.cdf, rng.random(n), side="right")
        is_topic = rng.random(n) < P_TOPIC
        t0 = self.topic_base + topic * TOPIC_WORDS
        idx[is_topic] = t0 + rng.integers(0, TOPIC_WORDS, size=int(is_topic.sum()))
        idx[rng.random(n) < P_SENTENCE_END] = self.end_token
        return idx

    def render(self, idx: np.ndarray) -> np.ndarray:
        """Concatena os bytes dos tokens sorteados (gather vetorizado)."""
        lens = self.lens[idx]
        ends = np.cumsum(lens)
        shift = np.repeat(self.starts[idx] - (ends - lens), lens)
        return self.flat[np.arange(int(ends[-1]), dtype=np.int64) + shift]


def _doc_sizes(total: int, docs: int) -> list[int]:
    base, extra = divmod(total, docs)
    return [base + (1 if i < extra else 0) for i in range(docs)]


def generate_corpus(out_dir: Path, *, docs: int, size_mb: float, seed: int,
                    force: bool = False) -> CorpusStats:
    """Gera `docs` arquivos doc_NNNNN.txt somando `size_mb` MiB em `out_dir`."""
    if docs <= 0 or size_mb <= 0:
        raise ValueError("docs e size_mb devem ser positivos")
    t0 = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    old = sorted(out_dir.glob("doc_*.txt"))
    if old:
        if not force:
            raise FileExistsError(
                f"{out_dir} já contém {len(old)} doc_*.txt; use --force para sobrescrever")
        for p in old:
            p.unlink()

    vocab = _Vocab(seed)
    total = round(size_mb * MIB)
    width = max(5, len(str(docs - 1)))
    written = 0
    for doc_id, size in enumerate(_doc_sizes(total, docs)):
        rng = _rng(seed, 1, doc_id)
        topic = doc_id % N_TOPICS
        with open(out_dir / f"doc_{doc_id:0{width}d}.txt", "wb") as f:
            remaining = size
            while remaining > 0:
                want = min(BLOCK_BYTES, remaining)
                n_tok = int(want / vocab.mean_len * 1.05) + 16
                buf = vocab.render(vocab.sample(rng, topic, n_tok))[:remaining]
                f.write(buf)
                remaining -= len(buf)
        written += size

    return CorpusStats(n_documentos=docs, bytes_escritos=written, seed=seed,
                       tempo_total_s=time.perf_counter() - t0,
                       numpy_version=np.__version__)
