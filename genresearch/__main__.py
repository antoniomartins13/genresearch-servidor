"""Entrypoint: `python -m genresearch <subcomando>`.

Cada subcomando imprime seu objeto de stats como uma linha JSON em stdout;
mensagens humanas vão para stderr.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from typing import Any

from .config import GenCorpusConfig, IngestConfig, QueryConfig, parse_args


def _to_json(obj: Any) -> str:
    """Serializa um dataclass de stats, omitindo campos None (não medidos)."""
    def drop_none(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: drop_none(v) for k, v in x.items() if v is not None}
        if isinstance(x, list):
            return [drop_none(v) for v in x]
        return x
    return json.dumps(drop_none(dataclasses.asdict(obj)), ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    """Despacha o subcomando e imprime as stats em JSON."""
    cfg = parse_args(argv)

    if isinstance(cfg, GenCorpusConfig):
        from .corpus import generate_corpus
        stats = generate_corpus(cfg.out, docs=cfg.docs, size_mb=cfg.size_mb,
                                seed=cfg.seed, force=cfg.force)
        print(f"corpus gerado em {cfg.out}", file=sys.stderr)
        print(_to_json(stats))

    elif isinstance(cfg, IngestConfig):
        from .ingest import ingest
        from .store import Store
        store = Store.create(cfg.store, dim=cfg.dim)
        try:
            stats = ingest(cfg.corpus, store, chunk_size=cfg.chunk_size,
                           overlap=cfg.overlap, read_mode=cfg.read_mode,
                           batch_size=cfg.batch_size)
        finally:
            store.close()
        print(f"índice gravado em {cfg.store}", file=sys.stderr)
        print(_to_json(stats))

    elif isinstance(cfg, QueryConfig):
        from .retrieve import retrieve
        from .store import Store
        store = Store.open(cfg.store, vectors_mmap=cfg.vectors_mmap)
        try:
            result = retrieve(store, cfg.q, cfg.k, cfg.max_context_bytes)
        finally:
            store.close()
        # O texto do contexto não vai para a linha JSON (ela é para CSV).
        out = dataclasses.replace(
            result, contexto=None,
            chunks=[dataclasses.replace(c, text=None) for c in result.chunks],
        )
        print(f"{result.n_trechos} trecho(s), {result.bytes_contexto} bytes de contexto",
              file=sys.stderr)
        print(_to_json(out))

    return 0


if __name__ == "__main__":
    sys.exit(main())
