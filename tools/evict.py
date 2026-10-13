"""Despejo do page cache por arquivo via posix_fadvise(POSIX_FADV_DONTNEED).

Uso como módulo:  from tools.evict import evict_files
Uso na CLI:       python -m tools.evict data/corpus/*.txt

Não precisa de root e só afeta os arquivos passados (ver tools/README.md).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable


def evict_files(paths: Iterable[Path]) -> None:
    """Pede ao kernel que descarte as páginas limpas desses arquivos do cache."""
    for p in paths:
        fd = os.open(p, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)


if __name__ == "__main__":
    evict_files(Path(a) for a in sys.argv[1:])
