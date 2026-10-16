"""Log de eventos protegido contra escrita concorrente (frente Cache e sync).

Um registro por linha, em JSON: ``{"seq", "t", "thr", "evento", "campos"}``.
``t`` = segundos desde a abertura (``perf_counter``). O arquivo é ASCII puro
(``ensure_ascii``), então ``bytes_escritos`` é exato.

Três modos, escolhidos por flag (Parte D, opção 3: serialização, lock ou
fila de escrita):

- ``"none"``: sem proteção. O registro é escrito em *fragmentos* (um
  ``write`` por campo, como faz um logger ingênuo que monta a linha aos
  poucos); fragmentos de threads diferentes se misturam e produzem linhas
  corrompidas. Escrever a linha inteira num único ``write`` esconderia o
  problema, porque o buffer do arquivo já serializa chamadas individuais.
- ``"lock"``: um ``Lock`` envolve a escrita do registro inteiro. Simples,
  mas o chamador espera o I/O (``espera_lock_s`` mede essa contenção).
- ``"queue"``: as threads só enfileiram; uma única thread escritora grava.
  Quem loga quase não espera, ao custo de uma thread a mais e de
  ``close()`` obrigatório para esvaziar a fila.

``seq`` é atribuído por quem grava (dentro do lock, ou na thread escritora),
então a ordem do arquivo coincide com ``seq``; no modo ``none`` é atribuído
antes da escrita e inversões são possíveis. ``race_window_s`` tem o mesmo
sentido que em ``cache.py``: espera artificial entre fragmentos, para ampliar
a janela de corrida (declarar no relatório).
"""

from __future__ import annotations

import itertools
import json
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .cache import TimedLock

LogMode = Literal["none", "lock", "queue"]
LOG_MODES: tuple[str, ...] = ("none", "lock", "queue")


class Evento:
    """Nomes dos eventos exigidos pelo enunciado."""

    INGESTAO = "ingestao"
    CONSULTA = "consulta"
    RECUPERACAO = "recuperacao"
    CACHE = "cache"
    GERACAO = "geracao"
    FALHA = "falha"
    ENCERRAMENTO = "encerramento"


_SENTINEL = object()


@dataclass
class LogStats:
    """Medições do logger. Em ``mode="none"`` os contadores não são confiáveis;
    use ``verify_log`` como verdade."""

    modo: str
    n_eventos: int
    bytes_escritos: int
    espera_lock_s: float
    fila_max: int


@dataclass
class LogCheck:
    """Resultado de ``verify_log``."""

    linhas: int
    validas: int
    corrompidas: int
    duplicadas: int
    fora_de_ordem: int
    registros_perdidos: int | None


class EventLog:
    """Log de eventos thread-safe (conforme ``mode``)."""

    def __init__(self, path: Path | str, *, mode: LogMode = "lock",
                 race_window_s: float | None = None, maxsize: int = 0,
                 flush_each: bool = True, truncate: bool = True) -> None:
        if mode not in LOG_MODES:
            raise ValueError(f"mode deve ser um de {LOG_MODES}")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mode = mode
        self._race_window_s = race_window_s
        self._flush_each = flush_each
        self._f = open(self.path, "w" if truncate else "a",
                       encoding="utf-8", newline="\n")
        self._t0 = time.perf_counter()
        self._seq = itertools.count()
        self._n = 0
        self._bytes = 0
        self._fila_max = 0
        self._closed = False
        self._lock = TimedLock()
        self._q: queue.Queue[Any] | None = None
        self._writer: threading.Thread | None = None
        if mode == "queue":
            self._q = queue.Queue(maxsize=maxsize)
            self._writer = threading.Thread(target=self._drain, name="log-writer",
                                            daemon=True)
            self._writer.start()

    # ---- internos -------------------------------------------------------

    def _window(self) -> None:
        if self._race_window_s is not None:
            time.sleep(self._race_window_s)

    @staticmethod
    def _json(obj: Any) -> str:
        return json.dumps(obj, separators=(",", ":"), default=str)

    def _write_whole(self, seq: int, t: float, thr: str, evento: str,
                     campos: dict[str, Any]) -> None:
        line = self._json({"seq": seq, "t": round(t, 6), "thr": thr,
                           "evento": evento, "campos": campos}) + "\n"
        self._f.write(line)
        if self._flush_each:
            self._f.flush()
        self._n += 1
        self._bytes += len(line)

    def _write_fragmented(self, seq: int, t: float, thr: str, evento: str,
                          campos: dict[str, Any]) -> None:
        parts = (f'{{"seq":{seq},"t":{round(t, 6)},',
                 f'"thr":{self._json(thr)},',
                 f'"evento":{self._json(evento)},',
                 f'"campos":{self._json(campos)}}}\n')
        for p in parts:
            self._f.write(p)
            self._window()
            self._bytes += len(p)
        if self._flush_each:
            self._f.flush()
        self._n += 1

    def _drain(self) -> None:
        assert self._q is not None
        while True:
            item = self._q.get()
            if item is _SENTINEL:
                break
            t, thr, evento, campos = item
            self._write_whole(next(self._seq), t, thr, evento, campos)

    # ---- API pública ----------------------------------------------------

    def log(self, evento: str, **campos: Any) -> None:
        """Registra um evento (use as constantes de ``Evento``)."""
        if self._closed:
            raise RuntimeError("log fechado")
        thr = threading.current_thread().name
        if self._mode == "queue":
            assert self._q is not None
            self._q.put((time.perf_counter() - self._t0, thr, evento, campos))
            n = self._q.qsize()
            if n > self._fila_max:
                self._fila_max = n
        elif self._mode == "lock":
            with self._lock:
                self._write_whole(next(self._seq), time.perf_counter() - self._t0,
                                  thr, evento, campos)
        else:
            self._write_fragmented(next(self._seq), time.perf_counter() - self._t0,
                                   thr, evento, campos)

    def close(self) -> None:
        """Esvazia a fila (modo queue), descarrega e fecha o arquivo."""
        if self._closed:
            return
        self._closed = True
        if self._q is not None and self._writer is not None:
            self._q.put(_SENTINEL)
            self._writer.join()
        self._f.flush()
        self._f.close()

    def stats(self) -> LogStats:
        return LogStats(modo=self._mode, n_eventos=self._n,
                        bytes_escritos=self._bytes,
                        espera_lock_s=self._lock.espera_s,
                        fila_max=self._fila_max)

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def verify_log(path: Path | str, *, esperadas: int | None = None) -> LogCheck:
    """Confere um arquivo de log: linhas corrompidas, ``seq`` duplicado ou
    fora de ordem e, se ``esperadas`` (nº de chamadas a ``log``) for dado,
    quantos registros não aparecem como linha válida."""
    linhas = validas = corrompidas = fora = 0
    vistos: set[int] = set()
    anterior = -1
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            linhas += 1
            try:
                rec = json.loads(line)
                seq = rec["seq"]
                if not isinstance(seq, int) or not isinstance(rec, dict):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                corrompidas += 1
                continue
            validas += 1
            if seq < anterior:
                fora += 1
            anterior = seq
            vistos.add(seq)
    return LogCheck(linhas=linhas, validas=validas, corrompidas=corrompidas,
                    duplicadas=validas - len(vistos), fora_de_ordem=fora,
                    registros_perdidos=None if esperadas is None
                    else esperadas - len(vistos))
