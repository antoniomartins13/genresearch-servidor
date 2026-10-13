"""Configuração dos subcomandos e parsing da linha de comando."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union

ReadMode = Literal["read", "mmap"]
READ_MODES: tuple[str, ...] = ("read", "mmap")

DEFAULT_DIM = 512
DEFAULT_CHUNK_SIZE = 2048
DEFAULT_OVERLAP = 128
DEFAULT_BATCH_SIZE = 256


@dataclass(frozen=True)
class GenCorpusConfig:
    """Parâmetros de `gen-corpus`."""

    out: Path
    docs: int
    size_mb: float
    seed: int
    force: bool = False


@dataclass(frozen=True)
class IngestConfig:
    """Parâmetros de `ingest`."""

    corpus: Path
    store: Path
    chunk_size: int = DEFAULT_CHUNK_SIZE
    overlap: int = DEFAULT_OVERLAP
    read_mode: ReadMode = "read"
    dim: int = DEFAULT_DIM
    batch_size: int = DEFAULT_BATCH_SIZE


@dataclass(frozen=True)
class QueryConfig:
    """Parâmetros de `query`."""

    store: Path
    q: str
    k: int = 5
    max_context_bytes: int = 8192
    vectors_mmap: bool = False
    read_mode: ReadMode | None = None


Config = Union[GenCorpusConfig, IngestConfig, QueryConfig]


def _positive_int(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError(f"esperado inteiro > 0, recebido {value}")
    return n


def _non_negative_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"esperado inteiro >= 0, recebido {value}")
    return n


def _positive_float(value: str) -> float:
    x = float(value)
    if x <= 0:
        raise argparse.ArgumentTypeError(f"esperado número > 0, recebido {value}")
    return x


def build_parser() -> argparse.ArgumentParser:
    """Monta o parser com os subcomandos gen-corpus, ingest e query."""
    parser = argparse.ArgumentParser(
        prog="python -m genresearch",
        description="GenResearch-Servidor: núcleo de ingestão e recuperação.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gen-corpus", help="gera corpus sintético determinístico")
    g.add_argument("--out", type=Path, required=True)
    g.add_argument("--docs", type=_positive_int, required=True)
    g.add_argument("--size-mb", type=_positive_float, required=True,
                   help="tamanho total do corpus em MiB")
    g.add_argument("--seed", type=int, required=True)
    g.add_argument("--force", action="store_true",
                   help="apaga doc_*.txt existentes no diretório de saída")

    i = sub.add_parser("ingest", help="fragmenta, gera vetores e persiste")
    i.add_argument("--corpus", type=Path, required=True)
    i.add_argument("--store", type=Path, required=True)
    i.add_argument("--chunk-size", type=_positive_int, default=DEFAULT_CHUNK_SIZE)
    i.add_argument("--overlap", type=_non_negative_int, default=DEFAULT_OVERLAP)
    i.add_argument("--read-mode", choices=READ_MODES, default="read")
    i.add_argument("--dim", type=_positive_int, default=DEFAULT_DIM)
    i.add_argument("--batch-size", type=_positive_int, default=DEFAULT_BATCH_SIZE)

    q = sub.add_parser("query", help="recupera contexto para uma consulta")
    q.add_argument("--store", type=Path, required=True)
    q.add_argument("--q", required=True)
    q.add_argument("--k", type=_positive_int, default=5)
    q.add_argument("--max-context-bytes", type=_positive_int, default=8192)
    q.add_argument("--vectors-mmap", action="store_true",
                   help="carrega vectors.npy com np.load(mmap_mode='r')")
    q.add_argument("--read-mode", choices=READ_MODES, default=None,
                   help="modo de releitura do corpus (padrão: o da ingestão)")
    return parser


def parse_args(argv: list[str] | None = None) -> Config:
    """Converte argv no dataclass de configuração do subcomando."""
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.command == "gen-corpus":
        return GenCorpusConfig(out=a.out, docs=a.docs, size_mb=a.size_mb,
                               seed=a.seed, force=a.force)
    if a.command == "ingest":
        if a.overlap >= a.chunk_size:
            parser.error("--overlap deve ser menor que --chunk-size")
        return IngestConfig(corpus=a.corpus, store=a.store, chunk_size=a.chunk_size,
                            overlap=a.overlap, read_mode=a.read_mode, dim=a.dim,
                            batch_size=a.batch_size)
    return QueryConfig(store=a.store, q=a.q, k=a.k,
                       max_context_bytes=a.max_context_bytes,
                       vectors_mmap=a.vectors_mmap, read_mode=a.read_mode)
