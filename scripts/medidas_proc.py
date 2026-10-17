"""Leituras de /proc e getrusage do próprio processo, para os scripts de experimento.

Tudo vem do kernel (``/proc/self/status``, ``/proc/self/io``, ``/proc/meminfo``,
``getrusage``); nada é estimado. Campos indisponíveis no ambiente (por exemplo
``/proc/self/io`` em alguns contêineres) simplesmente não aparecem no resultado.
"""

from __future__ import annotations

import math
import resource
import statistics
import threading
from typing import Sequence

_STATUS = ("VmSize", "VmRSS", "VmHWM", "RssAnon", "RssFile", "VmSwap", "Threads")
_MEMINFO = ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached",
            "SwapTotal", "SwapFree", "Dirty")


def status() -> dict:
    """Campos de memória de /proc/self/status (kB) e número de threads."""
    out: dict = {}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in _STATUS:
                    out[k if k == "Threads" else k + "_kb"] = int(v.split()[0])
    except OSError:
        pass
    return out


def io() -> dict:
    """/proc/self/io: bytes que o processo fez o kernel buscar no armazenamento
    (``read_bytes``) versus bytes pedidos em syscalls de leitura (``rchar``)."""
    try:
        with open("/proc/self/io") as f:
            d = dict(line.strip().split(": ") for line in f if ": " in line)
        return {"io_read_bytes": int(d["read_bytes"]), "io_write_bytes": int(d["write_bytes"]),
                "io_rchar": int(d["rchar"]), "io_syscr": int(d["syscr"])}
    except (OSError, KeyError, ValueError):
        return {}


def meminfo() -> dict:
    """Campos de /proc/meminfo (kB) relevantes para pressão de memória."""
    out: dict = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                if k in _MEMINFO:
                    out[k + "_kb"] = int(v.split()[0])
    except OSError:
        pass
    return out


def rusage() -> dict:
    """Faltas de página, blocos de E/S e trocas de contexto (processo inteiro)."""
    r = resource.getrusage(resource.RUSAGE_SELF)
    return {"minflt": r.ru_minflt, "majflt": r.ru_majflt, "inblock": r.ru_inblock,
            "oublock": r.ru_oublock, "nvcsw": r.ru_nvcsw, "nivcsw": r.ru_nivcsw}


def delta(antes: dict, depois: dict, prefixo: str = "") -> dict:
    """Diferença campo a campo (só chaves presentes nos dois)."""
    return {prefixo + k: depois[k] - antes[k] for k in antes if k in depois}


def com_prefixo(d: dict, prefixo: str) -> dict:
    return {prefixo + k: v for k, v in d.items()}


def percentil(valores: Sequence[float], p: float) -> float:
    """Nearest-rank, o mesmo método de ``genresearch.server``."""
    if not valores:
        return float("nan")
    s = sorted(valores)
    return s[max(1, math.ceil(p / 100.0 * len(s))) - 1]


def resumo(valores: Sequence[float]) -> dict:
    """Média, mediana, p95, mínimo e máximo; NaN se vazio."""
    if not valores:
        nan = float("nan")
        return {"media": nan, "mediana": nan, "p95": nan, "min": nan, "max": nan}
    return {"media": statistics.fmean(valores), "mediana": statistics.median(valores),
            "p95": percentil(valores, 95), "min": min(valores), "max": max(valores)}


class PicoRss:
    """Thread que amostra RssAnon/RssFile e guarda os máximos observados.

    Uso: ``with PicoRss() as p: ...``; depois, ``p.anon_kb`` e ``p.file_kb``.
    """

    def __init__(self, intervalo_s: float = 0.02) -> None:
        self.intervalo_s = intervalo_s
        self.anon_kb = 0
        self.file_kb = 0
        self.rss_kb = 0
        self._parar = threading.Event()
        self._t = threading.Thread(target=self._loop, name="pico-rss", daemon=True)

    def _amostra(self) -> None:
        s = status()
        self.anon_kb = max(self.anon_kb, s.get("RssAnon_kb", 0))
        self.file_kb = max(self.file_kb, s.get("RssFile_kb", 0))
        self.rss_kb = max(self.rss_kb, s.get("VmRSS_kb", 0))

    def _loop(self) -> None:
        while not self._parar.wait(self.intervalo_s):
            self._amostra()

    def __enter__(self) -> "PicoRss":
        self._amostra()
        self._t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._parar.set()
        self._t.join()
        self._amostra()
