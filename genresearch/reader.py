"""Acesso aos bytes de um documento via read() ou mmap.

Este é o ÚNICO módulo que decide o que `read_mode` significa. O resto do
código só enxerga um `ByteSource` com `slice(offset, n)`.

- "read": um f.read() do arquivo inteiro para um buffer do processo; as
  fatias são cópias desse buffer. O RSS cresce com o tamanho do documento.
- "mmap": o arquivo é mapeado e cada fatia copia só `mm[offset:offset+n]`.
  Apenas as páginas tocadas são carregadas. O arquivo inteiro NUNCA é
  materializado (nada de `mm[:]`).

`bytes_lidos` conta os bytes copiados da fonte para o processo: no modo
read, o retorno de read(); no modo mmap, a soma das fatias (inclui as
sobreposições entre chunks).
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
        self._data = f.read()
        self.size = len(self._data)
        self.bytes_lidos = self.size

    def slice(self, offset: int, n: int) -> bytes:
        return self._data[offset:offset + n]


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
