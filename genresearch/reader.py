"""Acesso aos bytes de um documento via read() ou mmap.

Este é o ÚNICO módulo que decide o que `read_mode` significa. O resto do
código só enxerga um `ByteSource` com `slice(offset, n)`.

- "read": leitura convencional; cada fatia é um seek(offset) + read(n). O
  arquivo inteiro nunca é lido de uma vez: quem consome o arquivo todo (a
  ingestão) o percorre fatia a fatia.
- "mmap": o arquivo é mapeado e cada fatia copia só `mm[offset:offset+n]`.
  Apenas as páginas tocadas são carregadas. O arquivo inteiro NUNCA é
  materializado (nada de `mm[:]`).

Os dois modos entregam exatamente os mesmos bytes nas mesmas fatias, então
`bytes_lidos` (soma das fatias, sobreposições incluídas) é igual entre eles:
a diferença medida é o mecanismo (syscall + cópia vs falta de página +
cópia), não o volume.
"""

from __future__ import annotations

import mmap
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Protocol

from .config import READ_MODES


class ByteSource(Protocol):
    """Fonte de bytes de um arquivo aberto."""

    size: int
    bytes_lidos: int

    def slice(self, offset: int, n: int) -> bytes:
        """Devolve os bytes [offset, offset+n) do arquivo."""
        ...


class _ReadSource:
    def __init__(self, f) -> None:
        self._f = f
        self.size = os.fstat(f.fileno()).st_size
        self.bytes_lidos = 0

    def slice(self, offset: int, n: int) -> bytes:
        n = max(0, min(n, self.size - offset))
        self._f.seek(offset)
        parts: list[bytes] = []
        while n > 0:
            b = self._f.read(n)  # FileIO sem buffer: read pode voltar curto
            if not b:
                break
            parts.append(b)
            n -= len(b)
        data = parts[0] if len(parts) == 1 else b"".join(parts)
        self.bytes_lidos += len(data)
        return data


class _MmapSource:
    def __init__(self, f) -> None:
        self.size = os.fstat(f.fileno()).st_size
        self.bytes_lidos = 0
        # mmap não aceita arquivo vazio; sem bytes, não há o que mapear.
        self._mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) if self.size else None

    def slice(self, offset: int, n: int) -> bytes:
        if self._mm is None:
            return b""
        b = self._mm[offset:offset + n]
        self.bytes_lidos += len(b)
        return b

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None


@contextmanager
def open_source(path: Path, mode: str) -> Iterator[ByteSource]:
    """Abre `path` no modo de leitura dado ("read" ou "mmap")."""
    if mode not in READ_MODES:
        raise ValueError(f"read_mode inválido: {mode!r} (use {READ_MODES})")
    with open(path, "rb", buffering=0) as f:
        if mode == "mmap":
            src = _MmapSource(f)
            try:
                yield src
            finally:
                src.close()
        else:
            yield _ReadSource(f)
