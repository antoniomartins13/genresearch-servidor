"""Cache compartilhado de resultados de consulta (frente Cache e sincronização).

Estrutura compartilhada entre as threads consumidoras (Parte D, opção 2 do
enunciado). Existe em duas versões escolhidas por flag, para poder
*demonstrar* a condição de corrida e a correção, não só descrevê-las:

- ``protected=False``: sem proteção. Verificar-e-agir (check-then-act) sem
  exclusão mútua: contadores perdem atualizações, a capacidade pode ser
  ultrapassada e a mesma chave pode ser calculada por várias threads.
- ``protected=True`` (padrão): um ``threading.Lock`` serializa as seções
  críticas (consulta, inserção, remoção, contadores). O ``compute()`` do
  chamador roda FORA do lock, senão a geração inteira seria serializada.
- ``single_flight=True`` (exige ``protected``): além do lock, só uma thread
  calcula cada chave por vez; as demais esperam o resultado dela. Elimina o
  cálculo duplicado ao custo de bloquear quem pediu a mesma chave.

Observação sobre o GIL: em CPython 3.12 uma thread só é trocada em chamadas
de função e saltos para trás, então ``x += 1`` simples quase nunca perde
atualização. Por isso ``race_window_s`` existe: se não for ``None``, o código
dorme esse tempo entre a leitura e a escrita de cada verificar-e-agir. É uma
ampliação *artificial* da janela de corrida, aplicada nos mesmos pontos nas
duas versões (na protegida, dentro do lock), e deve ser declarada no
relatório. ``None`` = sem janela artificial (corrida só natural).

O cache casa chaves exatas: consultas com o mesmo texto normalizado e mesmos
``k`` e ``max_context_bytes`` acertam; consultas semanticamente parecidas
mas escritas de outro modo erram. Isso é intencional (C4 compara repetidas
contra distintas).
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Literal

Policy = Literal["lru", "fifo"]
POLICIES: tuple[str, ...] = ("lru", "fifo")


def make_key(query: str, k: int, max_context_bytes: int) -> str:
    """Chave determinística: texto normalizado (espaços, caixa) + parâmetros.

    Usa blake2b, nunca o ``hash()`` embutido (aleatorizado por processo).
    """
    norm = " ".join(query.split()).casefold()
    raw = f"{k}\x00{max_context_bytes}\x00{norm}".encode("utf-8")
    return hashlib.blake2b(raw, digest_size=16).hexdigest()


class TimedLock:
    """``Lock`` que acumula o tempo total gasto esperando para adquiri-lo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.espera_s = 0.0

    def __enter__(self) -> None:
        t0 = time.perf_counter()
        self._lock.acquire()
        self.espera_s += time.perf_counter() - t0  # já sob o lock

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


@dataclass
class CacheStats:
    """Medições do cache. Na versão sem proteção os contadores podem estar
    errados (é o que a demonstração mostra); a verdade vem de fora."""

    politica: str
    protegido: bool
    single_flight: bool
    capacidade: int
    tamanho: int
    tamanho_max_observado: int
    acertos: int
    erros: int
    taxa_acerto: float
    taxa_erro: float
    substituicoes: int
    calculos: int
    coalescidas: int
    tempo_economizado_s: float
    falhas_concorrencia: int
    espera_lock_s: float


class _Flight:
    """Cálculo em andamento de uma chave (single-flight)."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None


class SharedCache:
    """Cache em memória de capacidade fixa (em entradas), LRU ou FIFO."""

    def __init__(self, capacity: int, *, policy: Policy = "lru",
                 protected: bool = True, single_flight: bool = False,
                 race_window_s: float | None = None) -> None:
        if capacity < 1:
            raise ValueError("capacity deve ser >= 1")
        if policy not in POLICIES:
            raise ValueError(f"policy deve ser uma de {POLICIES}")
        if single_flight and not protected:
            raise ValueError("single_flight exige protected=True")
        self._capacity = capacity
        self._policy = policy
        self._protected = protected
        self._single_flight = single_flight
        self._race_window_s = race_window_s
        self._guard: TimedLock | Any = TimedLock() if protected else nullcontext()
        self._d: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._inflight: dict[str, _Flight] = {}
        self._reset_counters()

    # ---- internos -------------------------------------------------------

    def _reset_counters(self) -> None:
        self._acertos = 0
        self._erros = 0
        self._substituicoes = 0
        self._calculos = 0
        self._coalescidas = 0
        self._tempo_economizado_s = 0.0
        self._falhas = 0
        self._tamanho_max = 0

    def _window(self) -> None:
        if self._race_window_s is not None:
            time.sleep(self._race_window_s)

    def _bump(self, name: str, delta: float = 1) -> None:
        """Incremento como leitura + (janela) + escrita: a atualização perdida
        só pode acontecer sem lock."""
        v = getattr(self, name)
        self._window()
        setattr(self, name, v + delta)

    def _lookup(self, key: str) -> tuple[bool, Any]:
        """Consulta e atualiza contadores/ordem. Chamar com o guard tomado."""
        d = self._d
        try:
            if key in d:
                self._window()
                value, cost = d[key]  # KeyError se outra thread removeu antes
                if self._policy == "lru":
                    d.move_to_end(key)
                self._bump("_acertos")
                self._bump("_tempo_economizado_s", cost)
                return True, value
        except KeyError:
            self._bump("_falhas")
        self._bump("_erros")
        return False, None

    def _store(self, key: str, value: Any, cost_s: float) -> None:
        """Insere/atualiza, removendo se cheio. Chamar com o guard tomado."""
        d = self._d
        if key in d:
            d[key] = (value, cost_s)
            if self._policy == "lru":
                try:
                    d.move_to_end(key)
                except KeyError:
                    self._bump("_falhas")
            return
        precisa_remover = len(d) >= self._capacity
        self._window()  # verificar-e-agir: entre o teste e a inserção
        if precisa_remover:
            try:
                d.popitem(last=False)  # mais antigo (LRU: menos recente)
                self._bump("_substituicoes")
            except KeyError:
                self._bump("_falhas")
        d[key] = (value, cost_s)
        n = len(d)
        if n > self._tamanho_max:
            self._tamanho_max = n

    # ---- API pública ----------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Devolve o valor em cache (conta acerto) ou ``default`` (conta erro)."""
        with self._guard:
            hit, value = self._lookup(key)
        return value if hit else default

    def put(self, key: str, value: Any, *, cost_s: float = 0.0) -> None:
        """Guarda ``value``; ``cost_s`` é o tempo que custou produzi-lo (é o
        que se economiza a cada acerto futuro)."""
        with self._guard:
            self._store(key, value, cost_s)

    def get_or_compute(self, key: str, compute: Callable[[], Any]) -> tuple[Any, bool]:
        """Devolve ``(valor, acertou)``; no erro chama ``compute()`` fora do lock
        e guarda o resultado. Exceção de ``compute`` propaga (nada é guardado)."""
        flight: _Flight | None = None
        owner = True
        with self._guard:
            hit, value = self._lookup(key)
            if hit:
                return value, True
            if self._single_flight:
                flight = self._inflight.get(key)
                if flight is None:
                    flight = self._inflight[key] = _Flight()
                else:
                    owner = False

        if not owner:
            assert flight is not None
            flight.done.wait()
            with self._guard:
                self._bump("_coalescidas")
            if flight.error is not None:
                raise flight.error
            return flight.value, False

        t0 = time.perf_counter()
        try:
            value = compute()
        except BaseException as exc:
            if flight is not None:
                with self._guard:
                    del self._inflight[key]
                flight.error = exc
                flight.done.set()
            raise
        cost = time.perf_counter() - t0
        with self._guard:
            self._bump("_calculos")
            self._store(key, value, cost)
            if flight is not None:
                del self._inflight[key]
        if flight is not None:
            flight.value = value
            flight.done.set()
        return value, False

    def __len__(self) -> int:
        return len(self._d)

    def clear(self) -> None:
        """Esvazia o cache e zera os contadores."""
        with self._guard:
            self._d.clear()
            self._reset_counters()

    def stats(self) -> CacheStats:
        """Instantâneo das medições."""
        with self._guard:
            total = self._acertos + self._erros
            return CacheStats(
                politica=self._policy, protegido=self._protected,
                single_flight=self._single_flight, capacidade=self._capacity,
                tamanho=len(self._d), tamanho_max_observado=self._tamanho_max,
                acertos=self._acertos, erros=self._erros,
                taxa_acerto=self._acertos / total if total else 0.0,
                taxa_erro=self._erros / total if total else 0.0,
                substituicoes=self._substituicoes, calculos=self._calculos,
                coalescidas=self._coalescidas,
                tempo_economizado_s=self._tempo_economizado_s,
                falhas_concorrencia=self._falhas,
                espera_lock_s=getattr(self._guard, "espera_s", 0.0),
            )


def cached_processor(processador: Callable[[Any], Any], cache: SharedCache, *,
                     k: int, max_context_bytes: int, log: Any = None) -> Callable[[Any], Any]:
    """Envolve o processador de ``server.run_queries`` com o cache.

    Não altera ``server.py``: ``run_queries(cached_processor(proc, cache, ...),
    consultas, ...)``. A chave é ``make_key(consulta, k, max_context_bytes)``.
    Se ``processador`` devolve ``dict``, o resultado ganha ``"cache"`` =
    ``"acerto"``, ``"erro"`` (esta chamada calculou) ou ``"coalescida"``
    (esperou outra thread, só com single_flight); o valor guardado no cache
    não é alterado. ``log`` é qualquer objeto com ``.log(evento, **campos)``
    (ex.: ``EventLog``); se dado, registra consulta, cache, recuperação,
    geração (a partir dos campos que o processador devolve) e falha.
    """
    def _ev(evento: str, **campos: Any) -> None:
        if log is not None:
            log.log(evento, **campos)

    def processar(consulta: Any) -> Any:
        texto = str(consulta)
        key = make_key(texto, k, max_context_bytes)
        executou = False

        def compute() -> Any:
            nonlocal executou
            executou = True
            return processador(consulta)

        _ev("consulta", chave=key[:8], consulta=texto[:80])
        t0 = time.perf_counter()
        try:
            value, hit = cache.get_or_compute(key, compute)
        except Exception as e:
            _ev("falha", chave=key[:8], erro=f"{type(e).__name__}: {e}")
            raise
        dt = time.perf_counter() - t0
        estado = "acerto" if hit else ("erro" if executou else "coalescida")
        _ev("cache", chave=key[:8], estado=estado, tempo_s=round(dt, 6))
        if estado == "erro" and isinstance(value, dict):
            if "tempo_retrieve_s" in value:
                _ev("recuperacao", chave=key[:8], n_trechos=value.get("n_trechos"),
                    bytes_contexto=value.get("bytes_contexto"),
                    tempo_s=value["tempo_retrieve_s"])
            if "tempo_geracao_s" in value:
                _ev("geracao", chave=key[:8], tempo_s=value["tempo_geracao_s"])
        return {**value, "cache": estado} if isinstance(value, dict) else value

    return processar
