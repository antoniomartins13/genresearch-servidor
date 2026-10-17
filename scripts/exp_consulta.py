"""Consultas instrumentadas (C1/C2 e C5): dados brutos por consulta + resumo.

    python -m scripts.exp_consulta --store data/exp/store_200mb_100d_read \\
        --read-mode mmap --cache frio-consulta --n-queries 50 --rep 1 \\
        --rotulo C12 --csv resultados/X/c12/consulta_req.csv

Uma linha JSON de resumo em stdout; uma linha por consulta em ``--csv``
(gravada e descarregada a cada consulta, então sobrevive a um OOM-kill).

Estados de cache:

- ``quente``: uma passada de aquecimento (fora da medição) antes de medir;
- ``frio``: ``sync`` + despejo do corpus, de ``vectors.npy`` e do SQLite ANTES
  de abrir o store, sem aquecimento. Mede a "primeira execução": carga do
  índice do disco e primeiros acessos;
- ``frio-consulta``: aquece e depois despeja o corpus antes de CADA consulta
  (mesma semântica de ``tools.bench_query``): só a releitura do corpus é fria.

``--vectors-mmap`` abre ``vectors.npy`` com ``np.load(mmap_mode='r')`` em vez
de carregá-lo inteiro na RAM. ``--limite-as-mb`` aplica RLIMIT_AS (espaço de
endereçamento) = VmSize atual + N MiB antes de abrir o store; é o mecanismo
alternativo de limite de memória quando o cgroup (systemd-run) não está
disponível. Falta de memória vira falha controlada: JSON com ``erro`` e
código de saída 3.

As consultas são as de ``tools.bench_query.make_queries`` (trechos do próprio
corpus, seed fixa), para ficarem comparáveis com aquela ferramenta.
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
import os
import resource
import sys
import time
from datetime import datetime
from pathlib import Path

from genresearch.retrieve import retrieve
from genresearch.store import Store
from scripts import medidas_proc as mp
from tools.bench_query import make_queries
from tools.evict import evict_files

MIB = 1024 * 1024
CAMPOS_CSV = ["rotulo", "rep", "cache", "read_mode", "vectors_mmap", "i", "tempo_s",
              "tempo_embedding_s", "tempo_busca_s", "tempo_leitura_s", "tempo_montagem_s",
              "bytes_lidos", "n_trechos", "minflt", "majflt"]


def salvar_maps(destino: Path, filtros: list[str]) -> None:
    """Grava as entradas de /proc/self/smaps cujo caminho contém algum filtro
    (mapeamentos do vectors.npy e do corpus): evidência de memória virtual."""
    blocos, atual = [], None
    with open("/proc/self/smaps") as f:
        for line in f:
            campos = line.split()
            eh_cabecalho = len(campos) >= 5 and "-" in campos[0] and ":" not in campos[0]
            if eh_cabecalho:
                atual = [line] if any(fl in line for fl in filtros) else None
                if atual is not None:
                    blocos.append(atual)
            elif atual is not None and campos and campos[0] in ("Size:", "Rss:", "Pss:",
                                                                   "Shared_Clean:",
                                                                   "Private_Clean:",
                                                                   "Anonymous:", "Swap:"):
                atual.append(line)
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "w") as f:
        f.write("# trechos de /proc/self/smaps (mapeamentos do índice e do corpus)\n")
        for b in blocos:
            f.writelines(b)
        with open("/proc/self/status") as st:
            f.write("\n# /proc/self/status\n")
            f.writelines(line for line in st if line.startswith(("Vm", "Rss")))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.exp_consulta")
    ap.add_argument("--store", type=Path, required=True)
    ap.add_argument("--read-mode", choices=("read", "mmap"), default=None,
                    help="padrão: o modo usado na ingestão")
    ap.add_argument("--vectors-mmap", action="store_true")
    ap.add_argument("--cache", choices=("quente", "frio", "frio-consulta"), default="quente")
    ap.add_argument("--n-queries", type=int, default=50)
    ap.add_argument("--seed", type=int, default=123)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--max-context-bytes", type=int, default=8192)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--rotulo", default="")
    ap.add_argument("--csv", type=Path, help="acrescenta uma linha por consulta")
    ap.add_argument("--limite-as-mb", type=float, default=None)
    ap.add_argument("--salvar-maps", type=Path, default=None,
                    help="arquivo para gravar os mapeamentos de /proc/self/smaps ao final")
    a = ap.parse_args(argv)

    # 1) Consultas e metadados, antes de qualquer medição ou despejo.
    st = Store.open(a.store, vectors_mmap=True)
    docs = sorted(st.corpus_dir.glob("*.txt"))
    n_vetores, dim = (int(x) for x in st.vectors.shape)
    modo_ingestao = st.read_mode
    st.close()
    del st  # Store.close não desfaz o mapeamento; sem isto o VmSize medido
    gc.collect()  # incluiria um segundo mapeamento do vectors.npy
    vec_path = a.store / "vectors.npy"
    queries = make_queries(docs, a.n_queries, a.seed)
    base = {
        "experimento": "consulta", "rotulo": a.rotulo, "rep": a.rep, "cache": a.cache,
        "read_mode": a.read_mode or modo_ingestao, "vectors_mmap": a.vectors_mmap,
        "n_consultas": a.n_queries, "k": a.k, "max_context_bytes": a.max_context_bytes,
        "seed": a.seed, "store": str(a.store), "n_vetores": n_vetores, "dim": dim,
        "tamanho_vetores_bytes": vec_path.stat().st_size,
        "tamanho_corpus_bytes": sum(p.stat().st_size for p in docs),
        "limite_as_mb": a.limite_as_mb,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }

    if a.cache == "frio":
        os.sync()
        evict_files(docs + [vec_path, a.store / "meta.sqlite"])
    if a.limite_as_mb is not None:
        lim = mp.status().get("VmSize_kb", 0) * 1024 + int(a.limite_as_mb * MIB)
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))

    def falha(fase: str, e: BaseException, extra: dict) -> int:
        out = {**base, **extra, "erro": f"{type(e).__name__}: {e}", "fase_erro": fase,
               **mp.com_prefixo(mp.status(), "fim_")}
        print(json.dumps(out, ensure_ascii=False), flush=True)
        return 3

    # 2) Abertura do store (carga do índice).
    m0, s0, r0, i0 = mp.meminfo(), mp.status(), mp.rusage(), mp.io()
    t0 = time.perf_counter()
    try:
        store = Store.open(a.store, vectors_mmap=a.vectors_mmap)
    except (MemoryError, OSError, ValueError) as e:
        return falha("abertura", e, {})
    t_abertura = time.perf_counter() - t0
    s_ab, r_ab, i_ab = mp.status(), mp.rusage(), mp.io()

    # 3) Aquecimento (fora da medição).
    if a.cache in ("quente", "frio-consulta"):
        try:
            for q in queries:
                retrieve(store, q, a.k, a.max_context_bytes, read_mode=a.read_mode)
        except (MemoryError, OSError) as e:
            return falha("aquecimento", e, {"tempo_abertura_s": t_abertura})

    # 4) Consultas medidas.
    f_csv, w = None, None
    if a.csv:
        a.csv.parent.mkdir(parents=True, exist_ok=True)
        novo = not a.csv.exists() or a.csv.stat().st_size == 0
        f_csv = open(a.csv, "a", newline="", encoding="utf-8")
        w = csv.DictWriter(f_csv, fieldnames=CAMPOS_CSV)
        if novo:
            w.writeheader()
    r_q0, i_q0 = mp.rusage(), mp.io()
    tempos, busca, leitura, mins, majs = [], [], [], [], []
    erro = None
    try:
        for i, q in enumerate(queries):
            if a.cache == "frio-consulta":
                evict_files(docs)
            ru0 = resource.getrusage(resource.RUSAGE_SELF)
            res = retrieve(store, q, a.k, a.max_context_bytes, read_mode=a.read_mode)
            ru1 = resource.getrusage(resource.RUSAGE_SELF)
            tempos.append(res.tempo_s)
            busca.append(res.tempo_busca_s)
            leitura.append(res.tempo_leitura_s)
            mins.append(ru1.ru_minflt - ru0.ru_minflt)
            majs.append(ru1.ru_majflt - ru0.ru_majflt)
            if w is not None:
                w.writerow({"rotulo": a.rotulo, "rep": a.rep, "cache": a.cache,
                            "read_mode": res.read_mode, "vectors_mmap": int(a.vectors_mmap),
                            "i": i, "tempo_s": res.tempo_s,
                            "tempo_embedding_s": res.tempo_embedding_s,
                            "tempo_busca_s": res.tempo_busca_s,
                            "tempo_leitura_s": res.tempo_leitura_s,
                            "tempo_montagem_s": res.tempo_montagem_s,
                            "bytes_lidos": res.bytes_lidos, "n_trechos": res.n_trechos,
                            "minflt": mins[-1], "majflt": majs[-1]})
                f_csv.flush()
    except (MemoryError, OSError) as e:
        erro = f"{type(e).__name__}: {e}"
    r_q1, i_q1 = mp.rusage(), mp.io()
    if f_csv is not None:
        f_csv.close()
    if a.salvar_maps is not None:
        salvar_maps(a.salvar_maps, [str(vec_path.resolve()), str(docs[0].parent.resolve())])
    s1, m1 = mp.status(), mp.meminfo()
    store.close()

    n = len(tempos)
    out = {
        **base,
        "consultas_ok": n,
        "tempo_abertura_s": t_abertura,
        **mp.com_prefixo(mp.delta(r0, r_ab), "abertura_"),
        **mp.com_prefixo(mp.delta(i0, i_ab), "abertura_"),
        **mp.com_prefixo(s0, "antes_abrir_"),
        **mp.com_prefixo(s_ab, "apos_abrir_"),
        **mp.com_prefixo(mp.resumo(tempos), "tempo_s_"),
        **mp.com_prefixo(mp.resumo(busca), "tempo_busca_s_"),
        **mp.com_prefixo(mp.resumo(leitura), "tempo_leitura_s_"),
        "minflt_por_consulta": sum(mins) / n if n else None,
        "majflt_por_consulta": sum(majs) / n if n else None,
        **mp.com_prefixo(mp.delta(r_q0, r_q1), "consultas_"),
        **mp.com_prefixo(mp.delta(i_q0, i_q1), "consultas_"),
        **mp.com_prefixo(s1, "fim_"),
        **mp.com_prefixo(m0, "antes_"), **mp.com_prefixo(m1, "depois_"),
    }
    if erro:
        out.update(erro=erro, fase_erro="consultas")
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return 3 if erro else 0


if __name__ == "__main__":
    sys.exit(main())
