"""Gráficos e tabelas de uma execução de ``scripts/run-experimentos.sh``.

    python -m scripts.graficos resultados/20261017-101500
    python -m scripts.graficos            # usa a execução mais recente em resultados/

Lê só os arquivos brutos da execução; nada é estimado nem preenchido. Gera:

- ``<RUN>/graficos/*.png``: figuras por experimento;
- ``<RUN>/agregados/*.csv``: média, desvio-padrão e n por configuração;
- ``<RUN>/tabelas.md``: tabelas prontas para o relatório, evidências e o mapa
  das questões de análise (seção 11 do enunciado) para as figuras/tabelas.

Média ± desvio-padrão são calculados entre repetições (uma medida por
repetição). Partes ausentes (etapa não executada ou com falha) são puladas
com aviso no próprio ``tabelas.md``.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

MIB = 1024 * 1024
KIB_POR_MIB = 1024
CORES = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]


# --------------------------------------------------------------------- leitura
def ler_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for linha in p.read_text(encoding="utf-8", errors="replace").splitlines():
        linha = linha.strip()
        if linha:
            try:
                out.append(json.loads(linha))
            except json.JSONDecodeError:
                pass
    return out


def ler_csv(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ler_resumos(base: Path) -> list[dict]:
    """resumo.csv do scripts.benchmark (fica em <dir>/real/resumo.csv)."""
    return ler_csv(base / "real" / "resumo.csv")


def num(x: Any) -> float | None:
    if x is None or x == "" or x == "None":
        return None
    if isinstance(x, bool):
        return float(x)
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def campo_json(r: dict, coluna: str, chave: str) -> float | None:
    """Campo de uma coluna do resumo.csv que guarda um dict em JSON."""
    try:
        return num(json.loads(r[coluna])[chave])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def obter(d: dict, caminho: str) -> Any:
    """Acesso aninhado: obter(d, 'anomalias.contagens_perdidas')."""
    for parte in caminho.split("."):
        if not isinstance(d, dict) or parte not in d:
            return None
        d = d[parte]
    return d


# --------------------------------------------------------------------- agregação
def mdp(vals: Iterable[float | None]) -> tuple[float | None, float | None, int]:
    v = [x for x in vals if x is not None and not math.isnan(x)]
    if not v:
        return None, None, 0
    return statistics.fmean(v), (statistics.stdev(v) if len(v) > 1 else 0.0), len(v)


ORDEM = {v: i for i, v in enumerate([
    "read", "mmap", "frio", "quente", "frio-consulta",
    "geração 20 ms", "geração 0 ms",
    "sem cache", "lock (safe)", "unsafe", "safe", "single-flight",
    "capacidade 4, LRU", "capacidade 4, FIFO", "repetida", "mista", "distinta",
    "vetores na RAM (np.load)", "vetores via mmap",
    "none", "lock", "queue", "janela 50 µs", "sem janela artificial"])}


def _chave_ordem(k: tuple) -> tuple:
    return tuple((0, v, "") if isinstance(v, (int, float)) and not isinstance(v, bool)
                 else (1, ORDEM.get(str(v), 999), str(v)) for v in k)


def agrega(rows: list[dict], chaves: list[str],
           metricas: dict[str, Callable[[dict], float | None]]) -> list[dict]:
    """Agrupa por ``chaves`` e calcula média/dp/n de cada métrica."""
    grupos: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        grupos[tuple(r.get(k) for k in chaves)].append(r)
    out = []
    for key in sorted(grupos, key=_chave_ordem):
        rs = grupos[key]
        d: dict = dict(zip(chaves, key))
        d["n"] = len(rs)
        for nome, f in metricas.items():
            vals = []
            for r in rs:
                try:
                    vals.append(f(r))
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    vals.append(None)
            m, s, _ = mdp(vals)
            d[nome + "_media"], d[nome + "_dp"] = m, s
        out.append(d)
    return out


def achar(ag: list[dict], **filtro) -> dict | None:
    for d in ag:
        if all(d.get(k) == v for k, v in filtro.items()):
            return d
    return None


def val(d: dict | None, metrica: str) -> tuple[float, float]:
    if d is None:
        return math.nan, 0.0
    m, s = d.get(metrica + "_media"), d.get(metrica + "_dp")
    return (math.nan if m is None else m), (0.0 if s is None else s)


# --------------------------------------------------------------------- formatação
def escala(x: Any, fator: float) -> float | None:
    v = num(x)
    return None if v is None else v * fator


def fmt(v: float | None, casas: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    s = f"{v:,.{casas}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def fmt_mdp(d: dict, metrica: str, casas: int = 2) -> str:
    m, s = d.get(metrica + "_media"), d.get(metrica + "_dp")
    if m is None:
        return "—"
    if d.get("n", 1) > 1 and s is not None:
        return f"{fmt(m, casas)} ± {fmt(s, casas)}"
    return fmt(m, casas)


def tabela_md(cab: list[str], linhas: list[list[str]]) -> str:
    out = ["| " + " | ".join(cab) + " |", "|" + "|".join("---" for _ in cab) + "|"]
    out += ["| " + " | ".join(str(c) for c in ln) + " |" for ln in linhas]
    return "\n".join(out)


def tabela_ag(ag: list[dict], colunas: list[tuple[str, str, int | None]]) -> str:
    """colunas: (título, chave_ou_métrica, casas); casas=None = chave de grupo."""
    linhas = []
    for d in ag:
        ln = []
        for _, chave, casas in colunas:
            ln.append(str(d.get(chave, "—")) if casas is None else fmt_mdp(d, chave, casas))
        linhas.append(ln)
    # métricas sem nenhum valor neste ambiente (ex.: /proc/self/io ausente no WSL) saem da tabela
    manter = [j for j, c in enumerate(colunas) if c[2] is None or any(ln[j] != "—" for ln in linhas)]
    return tabela_md([colunas[j][0] for j in manter], [[ln[j] for j in manter] for ln in linhas])


def virgula(ax, eixos: str = "y") -> None:
    f = FuncFormatter(lambda v, _: f"{v:g}".replace(".", ","))
    if "y" in eixos:
        ax.yaxis.set_major_formatter(f)
    if "x" in eixos:
        ax.xaxis.set_major_formatter(f)


def barras(ax, categorias: list[str], series: dict[str, list[tuple[float, float]]],
           ylabel: str, titulo: str, cores: list[str] | None = None) -> None:
    n = max(1, len(series))
    larg = 0.8 / n
    paleta = cores or CORES
    for i, (nome, vals) in enumerate(series.items()):
        xs = [j + (i - (n - 1) / 2) * larg for j in range(len(categorias))]
        ax.bar(xs, [v[0] for v in vals], larg, yerr=[v[1] for v in vals], capsize=3,
               label=nome, color=paleta[i % len(paleta)])
    ax.set_xticks(range(len(categorias)))
    ax.set_xticklabels(categorias)
    ax.set_ylabel(ylabel)
    ax.set_title(titulo, fontsize=10)
    if len(series) > 1:
        ax.legend(fontsize=8)
    virgula(ax)


def linhas(ax, series: dict[str, tuple[list[float], list[float], list[float]]],
           xlabel: str, ylabel: str, titulo: str, log2x: bool = False) -> None:
    todos_x: set = set()
    for i, (nome, (xs, ms, ds)) in enumerate(series.items()):
        ax.errorbar(xs, ms, yerr=ds, marker="o", capsize=3, label=nome,
                    color=CORES[i % len(CORES)])
        todos_x.update(xs)
    if log2x:
        ax.set_xscale("log", base=2)
    if todos_x:
        ax.set_xticks(sorted(todos_x))
        ax.set_xticklabels([f"{x:g}".replace(".", ",") for x in sorted(todos_x)])
        ax.minorticks_off()
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(titulo, fontsize=10)
    ax.grid(alpha=0.3)
    if len(series) > 1:
        ax.legend(fontsize=8)
    virgula(ax)


class Relatorio:
    """Acumula o markdown, os CSVs agregados e as figuras de uma execução."""

    def __init__(self, run: Path) -> None:
        self.run = run
        self.md: list[str] = []
        self.dir_fig = run / "graficos"
        self.dir_ag = run / "agregados"
        self.dir_fig.mkdir(exist_ok=True)
        self.dir_ag.mkdir(exist_ok=True)
        self.figuras: list[str] = []
        self.n_reps: int | None = None

    def h(self, texto: str, nivel: int = 2) -> None:
        self.md.append("\n" + "#" * nivel + " " + texto + "\n")

    def p(self, texto: str) -> None:
        self.md.append(texto + "\n")

    def salvar_ag(self, nome: str, ag: list[dict]) -> None:
        if not ag:
            return
        campos = list(dict.fromkeys(k for d in ag for k in d))
        with open(self.dir_ag / f"{nome}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=campos)
            w.writeheader()
            w.writerows(ag)

    def salvar_fig(self, fig, nome: str, nota: str = "") -> None:
        nota = nota or ("barras/pontos: média; traços: desvio-padrão entre repetições"
                        + (f" (n = {self.n_reps})" if self.n_reps else ""))
        fig.text(0.01, 0.005, nota, fontsize=7, color="#555")
        fig.tight_layout(rect=(0, 0.03, 1, 1))
        caminho = self.dir_fig / f"{nome}.png"
        fig.savefig(caminho, dpi=150)
        plt.close(fig)
        self.figuras.append(nome)
        self.p(f"![{nome}](graficos/{nome}.png)")


# --------------------------------------------------------------------- seções
def secao_cabecalho(R: Relatorio) -> None:
    meta = json.loads((R.run / "meta.json").read_text()) if (R.run / "meta.json").exists() else {}
    amb_p = R.run / "ambiente" / "ambiente.json"
    amb = json.loads(amb_p.read_text()) if amb_p.exists() else {}
    R.n_reps = meta.get("reps")
    R.md.append(f"# Resultados da execução `{R.run.name}`\n")
    if meta.get("rapido"):
        R.p("> **PILOTO (`--rapido`)**: parâmetros reduzidos. Não use estes números no relatório.\n")
    alt = R.run / "git_alteracoes_locais.txt"
    alteracoes = [ln for ln in (alt.read_text().splitlines() if alt.exists() else [])
                  if ln.strip() and not ln[3:].startswith("resultados/")]
    if alteracoes:
        R.p("> Aviso: havia alterações de código não commitadas no momento da execução "
            "(ver `git_alteracoes_locais.txt`).\n")
    if amb.get("wsl"):
        R.p("> Ambiente WSL2: o Linux roda numa VM. O disco é um arquivo virtual do Windows (VHDX), "
            "então o \"cache frio\" esvazia o page cache do Linux, mas o Windows pode manter o disco "
            "virtual no cache dele; não há sensor de temperatura nem `/proc/self/io`, e a frequência "
            "em `/proc/cpuinfo` é fixa.\n")
    linhas_amb = [
        ["Comando", f"`{meta.get('comando', '—')}`"],
        ["Commit", f"`{meta.get('commit', '—')}`"],
        ["Início / fim", f"{meta.get('inicio', '—')} / {meta.get('fim', '—')}"],
        ["Repetições por configuração", str(meta.get("reps", "—"))],
        ["Distribuição / kernel", f"{amb.get('distribuicao', '—')} / {amb.get('kernel', meta.get('kernel', '—'))}"],
        ["Virtualização", f"{amb.get('virtualizacao') or 'nenhuma detectada'}" + (" (WSL)" if amb.get("wsl") else "")],
        ["CPU", f"{amb.get('cpu_modelo', '—')} ({amb.get('cpus_logicas', '—')} CPUs lógicas)"],
        ["RAM / swap", f"{amb.get('mem_total_mib', meta.get('mem_total_mib', '—'))} MiB / {amb.get('swap_total_mib', '—')} MiB (swappiness {amb.get('swappiness', '—')})"],
        ["Sistema de arquivos / tipo de disco", f"{amb.get('sistema_arquivos', '—')} / "
         + ("disco virtual do WSL (o tipo físico não é visível no Linux)" if amb.get("wsl") else
            {"1": "rotacional (HDD)", "0": "não rotacional (SSD/NVMe)"}.get(str(amb.get("disco_rotacional")), "—"))],
        ["GPU", str(amb.get("gpu") or "nenhuma")],
        ["Python / numpy", f"{amb.get('python', meta.get('python', '—'))} / {amb.get('numpy', meta.get('numpy', '—'))}"],
        ["Corpus base", f"{meta.get('corpus_mb', '—')} MiB em {meta.get('docs', '—')} documentos (seed {meta.get('seed_corpus', '—')})"],
    ]
    if meta.get("openblas_num_threads") or meta.get("omp_num_threads"):
        linhas_amb.append(["Threads do BLAS (numpy)", f"OPENBLAS_NUM_THREADS={meta.get('openblas_num_threads') or '—'}, "
                                                      f"OMP_NUM_THREADS={meta.get('omp_num_threads') or '—'}"])
    R.h("Ambiente e parâmetros (Parte A)")
    R.p(tabela_md(["Item", "Valor"], linhas_amb))
    st = R.run / "status.txt"
    if st.exists():
        R.p("\nStatus das etapas:\n\n```\n" + st.read_text().strip() + "\n```")


def secao_c12(R: Relatorio) -> None:
    D = R.run / "c12"
    R.h("C1/C2 — leitura convencional (read) vs mapeamento (mmap)")
    ing = [r for r in ler_jsonl(D / "ingestao.jsonl")]
    if not ing:
        R.p("_Sem dados de C1/C2 (etapa não executada)._")
        return
    met_ing = {
        "tempo_leitura_s": lambda r: num(r["tempo_leitura_s"]),
        "tempo_total_s": lambda r: num(r["tempo_total_s"]),
        "tempo_embedding_s": lambda r: num(r["tempo_embedding_s"]),
        "pico_anon_mib": lambda r: num(r["pico_rss_anon_kb"]) / KIB_POR_MIB,
        "pico_file_mib": lambda r: num(r["pico_rss_file_kb"]) / KIB_POR_MIB,
        "majflt": lambda r: num(r["majflt"]),
        "minflt": lambda r: num(r["minflt"]),
        "disco_lido_mib": lambda r: num(r.get("io_read_bytes")) / MIB,
        "syscalls_leitura": lambda r: num(r.get("io_syscr")),
    }
    ag = agrega(ing, ["read_mode", "cache"], met_ing)
    R.salvar_ag("c12_ingestao", ag)
    caches = ["frio", "quente"]
    modos = ["read", "mmap"]
    fig, axs = plt.subplots(2, 2, figsize=(11, 8))
    barras(axs[0, 0], caches, {m: [val(achar(ag, read_mode=m, cache=c), "tempo_leitura_s") for c in caches] for m in modos},
           "segundos", "Ingestão: tempo de leitura do corpus")
    barras(axs[0, 1], caches, {m: [val(achar(ag, read_mode=m, cache=c), "tempo_total_s") for c in caches] for m in modos},
           "segundos", "Ingestão: tempo total (leitura + embedding + persistência)")
    barras(axs[1, 0], modos, {"pico RssAnon": [val(achar(ag, read_mode=m, cache="frio"), "pico_anon_mib") for m in modos],
                              "pico RssFile": [val(achar(ag, read_mode=m, cache="frio"), "pico_file_mib") for m in modos]},
           "MiB", "Ingestão a frio: pico de memória residente", cores=[CORES[2], CORES[3]])
    if any(d.get("disco_lido_mib_media") is not None for d in ag):
        barras(axs[1, 1], caches, {m: [val(achar(ag, read_mode=m, cache=c), "disco_lido_mib") for c in caches] for m in modos},
               "MiB lidos do dispositivo", "Ingestão: E/S real de disco (/proc/self/io)")
    else:
        barras(axs[1, 1], caches, {m: [val(achar(ag, read_mode=m, cache=c), "majflt") for c in caches] for m in modos},
               "faltas de página maiores", "Ingestão: faltas de página maiores")
    R.salvar_fig(fig, "c12_ingestao")
    R.p(tabela_ag(ag, [("Modo", "read_mode", None), ("Cache", "cache", None),
                       ("Leitura (s)", "tempo_leitura_s", 3), ("Total (s)", "tempo_total_s", 2),
                       ("Embedding (s)", "tempo_embedding_s", 2), ("Pico RssAnon (MiB)", "pico_anon_mib", 1),
                       ("Pico RssFile (MiB)", "pico_file_mib", 1), ("Faltas maiores", "majflt", 0),
                       ("Faltas menores", "minflt", 0), ("Disco lido (MiB)", "disco_lido_mib", 1),
                       ("Syscalls de leitura", "syscalls_leitura", 0)]))

    pr = ler_jsonl(D / "probe_rss.jsonl")
    if pr:
        R.h("Perfil de memória durante o chunking de um documento grande (tools.probe_rss)", 3)
        met = {
            "anon_fim_mib": lambda r: num(r["no_fim"]["RssAnon_kb"]) / KIB_POR_MIB,
            "file_fim_mib": lambda r: num(r["no_fim"]["RssFile_kb"]) / KIB_POR_MIB,
            "delta_file_mib": lambda r: (num(r["no_fim"]["RssFile_kb"]) - num(r["antes"]["RssFile_kb"])) / KIB_POR_MIB,
            "delta_anon_mib": lambda r: (num(r["no_fim"]["RssAnon_kb"]) - num(r["antes"]["RssAnon_kb"])) / KIB_POR_MIB,
            "majflt": lambda r: num(r["no_fim"]["ru_majflt"]) - num(r["antes"]["ru_majflt"]),
            "minflt": lambda r: num(r["no_fim"]["ru_minflt"]) - num(r["antes"]["ru_minflt"]),
        }
        agp = agrega(pr, ["read_mode", "cache"], met)
        R.salvar_ag("c12_probe_rss", agp)
        doc = pr[0].get("doc", "")
        R.p(f"Documento: `{doc}` ({pr[0].get('n_chunks', '—')} chunks). Valores no último chunk, "
            "com a fonte ainda aberta (antes do munmap/close).\n")
        R.p(tabela_ag(agp, [("Modo", "read_mode", None), ("Cache", "cache", None),
                            ("RssAnon no fim (MiB)", "anon_fim_mib", 1), ("RssFile no fim (MiB)", "file_fim_mib", 1),
                            ("Δ RssAnon (MiB)", "delta_anon_mib", 1), ("Δ RssFile (MiB)", "delta_file_mib", 1),
                            ("Faltas maiores", "majflt", 0), ("Faltas menores", "minflt", 0)]))

    cons = [r for r in ler_jsonl(D / "consulta.jsonl")]
    if cons:
        R.h("Consulta: releitura do corpus por read vs mmap (scripts.exp_consulta)", 3)
        met = {
            "leitura_mediana_ms": lambda r: num(r["tempo_leitura_s_mediana"]) * 1000,
            "leitura_p95_ms": lambda r: num(r["tempo_leitura_s_p95"]) * 1000,
            "total_mediana_ms": lambda r: num(r["tempo_s_mediana"]) * 1000,
            "total_p95_ms": lambda r: num(r["tempo_s_p95"]) * 1000,
            "majflt_consulta": lambda r: num(r["majflt_por_consulta"]),
            "minflt_consulta": lambda r: num(r["minflt_por_consulta"]),
        }
        agc = agrega(cons, ["read_mode", "cache"], met)
        R.salvar_ag("c12_consulta", agc)
        cs = ["quente", "frio-consulta"]
        fig, axs = plt.subplots(1, 3, figsize=(13, 4))
        barras(axs[0], cs, {m: [val(achar(agc, read_mode=m, cache=c), "leitura_mediana_ms") for c in cs] for m in modos},
               "ms", "Leitura do contexto por consulta (mediana)")
        barras(axs[1], cs, {m: [val(achar(agc, read_mode=m, cache=c), "leitura_p95_ms") for c in cs] for m in modos},
               "ms", "Leitura do contexto por consulta (p95)")
        barras(axs[2], cs, {m: [val(achar(agc, read_mode=m, cache=c), "majflt_consulta") for c in cs] for m in modos},
               "faltas por consulta", "Faltas de página maiores por consulta")
        R.salvar_fig(fig, "c12_consulta")
        R.p(tabela_ag(agc, [("Modo", "read_mode", None), ("Cache", "cache", None),
                            ("Leitura mediana (ms)", "leitura_mediana_ms", 3), ("Leitura p95 (ms)", "leitura_p95_ms", 3),
                            ("Recuperação mediana (ms)", "total_mediana_ms", 2), ("Recuperação p95 (ms)", "total_p95_ms", 2),
                            ("Faltas maiores/consulta", "majflt_consulta", 2),
                            ("Faltas menores/consulta", "minflt_consulta", 2)]))
        R.p("\n`frio-consulta`: corpus despejado do page cache antes de cada consulta "
            "(vetores e SQLite quentes). Mesma semântica de `tools.bench_query`.")

    bq = ler_jsonl(D / "bench_query.jsonl")
    if bq:
        agb = agrega(bq, ["read_mode", "cache"], {
            "leitura_mediana_ms": lambda r: num(r["tempo_leitura_mediana_s"]) * 1000,
            "majflt_consulta": lambda r: num(r["majflt_por_consulta"])})
        R.salvar_ag("c12_bench_query", agb)
        R.p("\nConferência com `tools.bench_query` (ferramenta do núcleo citada no CLAUDE.md):\n")
        R.p(tabela_ag(agb, [("Modo", "read_mode", None), ("Cache", "cache", None),
                            ("Leitura mediana (ms)", "leitura_mediana_ms", 3),
                            ("Faltas maiores/consulta", "majflt_consulta", 2)]))

    st = {m: ler_strace(D / f"strace_ingestao_{m}.txt") for m in modos}
    if any(st.values()):
        R.h("Chamadas de sistema na ingestão (strace -c -f)", 3)
        nomes = sorted({n for s in st.values() for n in s}, key=lambda n: -max(s.get(n, 0) for s in st.values()))[:12]
        R.p(tabela_md(["Syscall", "read (chamadas)", "mmap (chamadas)"],
                      [[f"`{n}`", fmt(st["read"].get(n), 0), fmt(st["mmap"].get(n), 0)] for n in nomes]))
        R.p("\nExecução separada sob strace; seus tempos não entram nas tabelas acima.")


def ler_strace(p: Path) -> dict[str, float]:
    """Contagem de chamadas por syscall de um arquivo ``strace -c``."""
    out: dict[str, float] = {}
    if not p.exists():
        return out
    for linha in p.read_text(errors="replace").splitlines():
        c = linha.split()
        if len(c) in (5, 6) and c[-1] != "total" and c[3].isdigit():
            try:
                float(c[0])
            except ValueError:
                continue
            out[c[-1]] = float(c[3])
    return out


def ler_etapas(p: Path) -> tuple[list[tuple[str, float, float, str]], list[tuple[float, float]]]:
    """Intervalos (nome, início, fim, código) das etapas e das pausas para esfriar."""
    etapas, pausas, abertos = [], [], {}
    p0 = None
    if not p.exists():
        return etapas, pausas
    for linha in p.read_text().splitlines():
        c = linha.split("\t")
        if len(c) < 3:
            continue
        tipo, nome, ep = c[0], c[1], float(c[2])
        if tipo == "inicio":
            abertos[nome] = ep
        elif tipo == "fim" and nome in abertos:
            etapas.append((nome, abertos.pop(nome), ep, c[3] if len(c) > 3 else ""))
        elif tipo == "pausa":
            p0 = ep
        elif tipo == "fim_pausa" and p0 is not None:
            pausas.append((p0, ep))
            p0 = None
    return etapas, pausas


def secao_termico(R: Relatorio) -> None:
    D = R.run / "termico"
    arquivos = sorted(D.glob("termico_*.csv"))
    if not arquivos:
        return
    R.h("Condições térmicas durante a execução")
    for arq in arquivos:
        ts = arq.stem.replace("termico_", "")
        ip = D / f"info_{ts}.json"
        info = json.loads(ip.read_text()) if ip.exists() else {}
        rows = [r for r in ler_csv(arq) if num(r.get("epoch")) is not None]
        if len(rows) < 2:
            continue
        etapas, pausas = ler_etapas(D / f"etapas_{ts}.tsv")
        ep = [num(r["epoch"]) for r in rows]
        t0 = ep[0]
        mins = [(e - t0) / 60 for e in ep]
        temp = [num(r["temp_c"]) for r in rows]
        freq = [num(r["freq_media_mhz"]) for r in rows]
        fmin = [num(r["freq_min_mhz"]) for r in rows]
        uso = [num(r["uso_cpu_pct"]) for r in rows]
        thr = [None if num(r["throttle_core"]) is None and num(r["throttle_pkg"]) is None
               else (num(r["throttle_core"]) or 0) + (num(r["throttle_pkg"]) or 0) for r in rows]
        tem_temp = any(v is not None for v in temp)
        crit = info.get("temp_critica_c")
        ref = crit or 95.0
        nan = float("nan")

        def serie(v: list) -> list:
            return [nan if x is None else x for x in v]

        if len(arquivos) > 1:
            R.h(f"Execução {ts}", 3)
        fig, axs = plt.subplots(3, 1, figsize=(12, 8), sharex=True)
        total = max(mins[-1], 1e-9)
        for i, (nome, a, b, _) in enumerate(etapas):
            for ax in axs:
                ax.axvspan((a - t0) / 60, (b - t0) / 60, color="#000", alpha=0.09 if i % 2 == 0 else 0.03, lw=0)
            if (b - a) / 60 >= 0.015 * total:
                axs[0].text(((a + b) / 2 - t0) / 60, 1.01, nome, transform=axs[0].get_xaxis_transform(),
                            ha="center", va="bottom", fontsize=7)
        for a, b in pausas:
            for ax in axs:
                ax.axvspan((a - t0) / 60, (b - t0) / 60, color=CORES[3], alpha=0.3, lw=0)
        if tem_temp:
            axs[0].plot(mins, serie(temp), color=CORES[1], lw=1)
            if crit:
                axs[0].axhline(crit, ls="--", color="#900", lw=1, label=f"limite informado pelo sensor ({fmt(crit, 0)} °C)")
                axs[0].legend(fontsize=7, loc="lower right")
            axs[0].set_ylabel("°C")
        else:
            axs[0].text(0.5, 0.5, "sem sensor de temperatura neste ambiente", ha="center", va="center",
                        transform=axs[0].transAxes, color="#777")
        axs[0].set_title("Temperatura da CPU", fontsize=10, pad=14)
        axs[1].plot(mins, serie(freq), color=CORES[0], lw=1, label="média entre núcleos")
        axs[1].plot(mins, serie(fmin), color=CORES[0], lw=0.6, alpha=0.5, label="núcleo mais lento")
        if info.get("freq_max_nominal_mhz"):
            axs[1].axhline(info["freq_max_nominal_mhz"], ls="--", color="#555", lw=1, label="máxima nominal")
        axs[1].set_ylabel("MHz")
        axs[1].set_title(f"Frequência da CPU (fonte: {info.get('fonte_frequencia') or '—'})", fontsize=10)
        axs[1].legend(fontsize=7, loc="lower right")
        axs[2].plot(mins, serie(uso), color=CORES[2], lw=1)
        axs[2].set_ylabel("%")
        axs[2].set_ylim(0, 105)
        axs[2].set_title("Uso total da CPU (todas as CPUs)", fontsize=10)
        axs[2].set_xlabel("minutos desde o início da bateria")
        for ax in axs:
            ax.grid(alpha=0.3)
            virgula(ax, "xy")
        nome_fig = "termico" if len(arquivos) == 1 else f"termico_{ts}"
        R.salvar_fig(fig, nome_fig, nota="faixas cinza: etapas; faixas laranja: pausas para esfriar; "
                                          "série bruta, uma amostra por segundo")

        linhas_t, ag = [], []
        for nome, a, b, rc in [("bateria inteira", ep[0], ep[-1], "")] + etapas:
            idx = [i for i, e in enumerate(ep) if a <= e <= b]
            if not idx:
                continue
            ts_ = [temp[i] for i in idx if temp[i] is not None]
            fs = [freq[i] for i in idx if freq[i] is not None]
            fo = [freq[i] for i in idx if freq[i] is not None and (uso[i] or 0) > 50]
            us = [uso[i] for i in idx if uso[i] is not None]
            th = [thr[i] for i in idx if thr[i] is not None]
            dthr = (max(th) - min(th)) if th else None
            d = {"etapa": nome, "duracao_min": (b - a) / 60,
                 "temp_media_c": statistics.fmean(ts_) if ts_ else None, "temp_max_c": max(ts_) if ts_ else None,
                 "freq_media_mhz": statistics.fmean(fs) if fs else None,
                 "freq_ocupada_mhz": statistics.fmean(fo) if fo else None,
                 "uso_medio_pct": statistics.fmean(us) if us else None, "eventos_throttling": dthr}
            ag.append(d)
            linhas_t.append([nome, fmt(d["duracao_min"], 1), fmt(d["temp_media_c"], 1), fmt(d["temp_max_c"], 1),
                             fmt(d["freq_media_mhz"], 0), fmt(d["freq_ocupada_mhz"], 0), fmt(d["uso_medio_pct"], 1),
                             "n/d" if dthr is None else fmt(dthr, 0)])
        R.salvar_ag("termico_etapas" if len(arquivos) == 1 else f"termico_etapas_{ts}", ag)
        R.p(tabela_md(["Etapa", "Duração (min)", "Temp. média (°C)", "Temp. máx. (°C)", "Freq. média (MHz)",
                       "Freq. com CPU ocupada > 50% (MHz)", "Uso médio da CPU (%)", "Eventos de throttling"],
                      linhas_t))

        obs = []
        if info.get("sensor"):
            obs.append(f"Sensor: `{info['sensor']}` ({info.get('fonte_temperatura')}).")
        geral = ag[0] if ag else {}
        if geral.get("eventos_throttling") is not None:
            com = [d["etapa"] for d in ag[1:] if (d["eventos_throttling"] or 0) > 0]
            if geral["eventos_throttling"] > 0:
                obs.append(f"O processador registrou {fmt(geral['eventos_throttling'], 0)} evento(s) de redução de "
                           f"clock por temperatura (etapas: {', '.join(com) or '—'}). Os tempos dessas etapas podem "
                           "estar inflados; confira se o desvio-padrão delas é maior que o das demais.")
            else:
                obs.append("O processador não registrou nenhum evento de redução de clock por temperatura.")
        else:
            obs.append("Contadores de throttling indisponíveis neste processador/ambiente (existem só em Intel).")
        if geral.get("temp_max_c") is not None:
            ref_txt = f"limite informado pelo sensor, {fmt(crit, 0)} °C" if crit else "referência de 95 °C"
            if geral["temp_max_c"] >= ref - 5:
                obs.append(f"A temperatura chegou a {fmt(geral['temp_max_c'], 1)} °C, a menos de 5 °C do limite "
                           f"({ref_txt}).")
            else:
                obs.append(f"A temperatura máxima foi {fmt(geral['temp_max_c'], 1)} °C, mais de 5 °C abaixo do "
                           f"limite ({ref_txt}).")
        elif info.get("observacao"):
            obs.append(info["observacao"][0].upper() + info["observacao"][1:] + ".")
        if pausas:
            obs.append(f"Houve {len(pausas)} pausa(s) para esfriar, somando {fmt(sum(b - a for a, b in pausas), 0)} s, "
                       "fora da duração das etapas.")
        if info.get("fonte_frequencia") == "/proc/cpuinfo":
            obs.append("A frequência vem de /proc/cpuinfo; em VMs e no WSL costuma ser um valor fixo, não a real.")
        R.p("\n" + " ".join(obs))


def _w(r: dict) -> int | None:
    v = num(r.get("workers"))
    return int(v) if v is not None else None


def secao_c3(R: Relatorio) -> None:
    D = R.run / "c3"
    R.h("C3 — concorrência e fila (2 produtores, fila de capacidade 4)")
    rows = []
    for gen in ("gen20", "gen0"):
        for r in ler_resumos(D / gen):
            r["_gen"] = "geração 20 ms" if gen == "gen20" else "geração 0 ms"
            r["_w"] = _w(r)
            rows.append(r)
    if not rows:
        R.p("_Sem dados de C3._")
        return

    def consultas_min_max(r: dict) -> float | None:
        try:
            v = json.loads(r["server_consultas_por_worker"])
            return (max(v) - min(v)) / max(1, sum(v)) * 100
        except (KeyError, ValueError, TypeError):
            return None

    met = {
        "vazao": lambda r: num(r["server_vazao_rps"]),
        "lat_mediana_ms": lambda r: campo_json(r, "server_latencia_s", "mediana") * 1000,
        "lat_p95_ms": lambda r: campo_json(r, "server_latencia_s", "p95") * 1000,
        "proc_mediana_ms": lambda r: campo_json(r, "server_proc_s", "mediana") * 1000,
        "espera_media_ms": lambda r: campo_json(r, "server_espera_s", "media") * 1000,
        "fila_media": lambda r: num(r["server_fila_media"]),
        "fila_max": lambda r: num(r["server_fila_max"]),
        "utilizacao": lambda r: num(r["server_utilizacao_media"]) * 100,
        "cpu_pct": lambda r: num(r["process_cpu_percent"]),
        "bloqueio_prod_s": lambda r: num(r["server_bloqueio_produtores_s"]),
        "threads": lambda r: num(r["process_threads"]),
        "pico_rss_mib": lambda r: num(r["process_peak_rss_mb"]),
        "desbalanceamento_pct": consultas_min_max,
    }
    ag = agrega(rows, ["_gen", "_w"], met)
    R.salvar_ag("c3", ag)
    gens = [g for g in ("geração 20 ms", "geração 0 ms") if any(d["_gen"] == g for d in ag)]

    def serie(g: str, m: str) -> tuple[list, list, list]:
        ds = [d for d in ag if d["_gen"] == g]
        return [d["_w"] for d in ds], [val(d, m)[0] for d in ds], [val(d, m)[1] for d in ds]

    fig, axs = plt.subplots(1, 2, figsize=(12, 4.5))
    s = {g: serie(g, "vazao") for g in gens}
    linhas(axs[0], s, "workers (threads consumidoras)", "consultas/s", "Vazão", log2x=True)
    ganho = {}
    for g, (xs, ms, ds) in s.items():
        if xs and ms[0] and not math.isnan(ms[0]):
            ganho[g] = (xs, [m / ms[0] for m in ms], [d / ms[0] for d in ds])
    linhas(axs[1], ganho, "workers (threads consumidoras)", "vazão / vazão com 1 worker",
           "Speedup em relação a 1 worker", log2x=True)
    xs_all = sorted({x for xs, _, _ in ganho.values() for x in xs})
    if xs_all:
        axs[1].plot(xs_all, [x / xs_all[0] for x in xs_all], ls="--", color="#888", label="ideal (linear)")
        axs[1].legend(fontsize=8)
    R.salvar_fig(fig, "c3_vazao")

    fig, axs = plt.subplots(2, 2, figsize=(11, 8))
    linhas(axs[0, 0], {g: serie(g, "lat_p95_ms") for g in gens}, "workers", "ms", "Latência p95 (espera + processamento)", log2x=True)
    linhas(axs[0, 1], {g: serie(g, "espera_media_ms") for g in gens}, "workers", "ms", "Espera média na fila", log2x=True)
    linhas(axs[1, 0], {g: serie(g, "utilizacao") for g in gens}, "workers", "%", "Utilização média dos workers", log2x=True)
    linhas(axs[1, 1], {g: serie(g, "cpu_pct") for g in gens}, "workers", "% de 1 CPU", "CPU do processo (user + sys)", log2x=True)
    R.salvar_fig(fig, "c3_latencia_recursos")

    for g in gens:
        R.p(f"\n**{g}**\n")
        R.p(tabela_ag([d for d in ag if d["_gen"] == g], [
            ("Workers", "_w", None), ("Vazão (req/s)", "vazao", 1), ("Latência mediana (ms)", "lat_mediana_ms", 1),
            ("Latência p95 (ms)", "lat_p95_ms", 1), ("Processamento mediano (ms)", "proc_mediana_ms", 1),
            ("Espera média (ms)", "espera_media_ms", 1), ("Fila média", "fila_media", 2), ("Fila máx.", "fila_max", 0),
            ("Utilização (%)", "utilizacao", 1), ("CPU (%)", "cpu_pct", 1),
            ("Bloqueio dos produtores (s)", "bloqueio_prod_s", 2), ("Threads", "threads", 0),
            ("Pico RSS (MiB)", "pico_rss_mib", 1), ("Desbalanceamento entre workers (%)", "desbalanceamento_pct", 1)]))
    R.p("\nDesbalanceamento = (máx − mín de consultas atendidas por worker) / total. "
        "Valores altos indicam workers ociosos ou starvation. Threads = threads do processo ao final "
        "da execução, depois que os workers terminaram; inclui threads criadas por bibliotecas "
        "(o numpy usa um pool próprio de threads do BLAS). CPU (%) é relativa a 1 CPU e inclui o "
        "tempo dessas threads de biblioteca.")

    t = D / "threads" / "tarefas.txt"
    if t.exists() and t.read_text().strip():
        R.h("Evidência de threads (servidor com 4 workers e 2 produtores, /proc/PID/task)", 3)
        R.p("Colunas: TID, comando, estado, função do kernel em que a thread espera (wchan).\n")
        R.p("```\n" + t.read_text().strip() + "\n```")
    stv = ler_strace(D / "strace_servidor_w4.txt")
    if stv:
        top = sorted(stv.items(), key=lambda kv: -kv[1])[:10]
        R.h("Chamadas de sistema do servidor (strace -c -f, 4 workers)", 3)
        R.p(tabela_md(["Syscall", "Chamadas"], [[f"`{n}`", fmt(c, 0)] for n, c in top]))


def secao_c4(R: Relatorio) -> None:
    D = R.run / "c4"
    R.h("C4 — cache compartilhado")
    nomes = {"sem_cache": "sem cache", "safe": "lock (safe)", "single_flight": "single-flight",
             "cap4_lru": "capacidade 4, LRU", "cap4_fifo": "capacidade 4, FIFO"}
    rows = []
    for b in nomes:
        for r in ler_resumos(D / b):
            r["_bat"] = nomes[b]
            r["_carga"] = r["config"].replace("C4_", "")
            rows.append(r)
    if not rows:
        R.p("_Sem dados de C4._")
        return
    met = {
        "taxa_acerto": lambda r: num(r["cache_taxa_acerto"]) * 100,
        "acertos": lambda r: num(r["cache_acertos"]),
        "erros": lambda r: num(r["cache_erros"]),
        "substituicoes": lambda r: num(r["cache_substituicoes"]),
        "calculos": lambda r: num(r["cache_calculos"]),
        "coalescidas": lambda r: num(r["cache_coalescidas"]),
        "economizado_s": lambda r: num(r["cache_tempo_economizado_s"]),
        "espera_lock_ms": lambda r: num(r["cache_espera_lock_s"]) * 1000,
        "lat_media_ms": lambda r: campo_json(r, "server_latencia_s", "media") * 1000,
        "lat_p95_ms": lambda r: campo_json(r, "server_latencia_s", "p95") * 1000,
        "vazao": lambda r: num(r["server_vazao_rps"]),
        "log_corrompidas": lambda r: num(json.loads(r["server_event_log"])["verificacao"]["corrompidas"]),
    }
    ag = agrega(rows, ["_bat", "_carga"], met)
    R.salvar_ag("c4", ag)
    cargas = [c for c in ("repetida", "mista", "distinta") if any(d["_carga"] == c for d in ag)]
    com_cache = [b for b in nomes.values() if b != "sem cache" and any(d["_bat"] == b for d in ag)]
    todas = [b for b in nomes.values() if any(d["_bat"] == b for d in ag)]
    cor = {b: CORES[i] for i, b in enumerate(nomes.values())}  # mesma cor em todos os painéis
    fig, axs = plt.subplots(1, 3, figsize=(15, 4.5))
    barras(axs[0], cargas, {b: [val(achar(ag, _bat=b, _carga=c), "taxa_acerto") for c in cargas] for b in com_cache},
           "%", "Taxa de acerto do cache", cores=[cor[b] for b in com_cache])
    barras(axs[1], cargas, {b: [val(achar(ag, _bat=b, _carga=c), "lat_media_ms") for c in cargas] for b in todas},
           "ms", "Latência média por consulta", cores=[cor[b] for b in todas])
    barras(axs[2], cargas, {b: [val(achar(ag, _bat=b, _carga=c), "calculos") for c in cargas] for b in com_cache},
           "recuperações executadas", "Recuperações realmente calculadas", cores=[cor[b] for b in com_cache])
    R.salvar_fig(fig, "c4_cache")
    R.p(tabela_ag(ag, [("Configuração", "_bat", None), ("Carga", "_carga", None),
                       ("Acerto (%)", "taxa_acerto", 1), ("Acertos", "acertos", 1), ("Erros", "erros", 1),
                       ("Substituições", "substituicoes", 1), ("Cálculos", "calculos", 1),
                       ("Coalescidas", "coalescidas", 1), ("Tempo economizado (s)", "economizado_s", 2),
                       ("Espera no lock (ms)", "espera_lock_ms", 2), ("Latência média (ms)", "lat_media_ms", 1),
                       ("Latência p95 (ms)", "lat_p95_ms", 1), ("Vazão (req/s)", "vazao", 1),
                       ("Linhas de log corrompidas", "log_corrompidas", 0)]))
    R.p("\nCarga `repetida`: consultas de um conjunto fixo (parâmetro `--cache-distinct` "
        "do benchmark, padrão 8). Com capacidade ≥ esse número, cada chave deveria ser calculada uma vez; "
        "cálculos acima disso na configuração `lock (safe)` são recálculos concorrentes da mesma chave "
        "(dois workers erram ao mesmo tempo), que o `single-flight` elimina (coluna Coalescidas).")


def secao_contexto(R: Relatorio) -> None:
    rows = ler_resumos(R.run / "contexto")
    if not rows:
        return
    R.h("Fator contexto (max_context_bytes 2048 vs 8192)")
    for r in rows:
        r["_ctx"] = int(num(r["context_bytes"]))
    ag = agrega(rows, ["_ctx"], {
        "lat_mediana_ms": lambda r: campo_json(r, "server_latencia_s", "mediana") * 1000,
        "lat_p95_ms": lambda r: campo_json(r, "server_latencia_s", "p95") * 1000,
        "vazao": lambda r: num(r["server_vazao_rps"]),
        "pico_rss_mib": lambda r: num(r["process_peak_rss_mb"]),
        "minflt": lambda r: num(r["process_minor_page_faults"]),
    })
    R.salvar_ag("contexto", ag)
    R.p(tabela_ag(ag, [("Contexto (bytes)", "_ctx", None), ("Latência mediana (ms)", "lat_mediana_ms", 2),
                       ("Latência p95 (ms)", "lat_p95_ms", 2), ("Vazão (req/s)", "vazao", 1),
                       ("Pico RSS (MiB)", "pico_rss_mib", 1), ("Faltas menores", "minflt", 0)]))


def ler_vmstat(p: Path) -> list[dict]:
    if not p.exists():
        return []
    cab, out = None, []
    for linha in p.read_text(errors="replace").splitlines():
        c = linha.split()
        if not c:
            continue
        if c[0] == "r" and "free" in c:
            cab = c
            continue
        if cab and c[0].isdigit():
            out.append({k: num(v) for k, v in zip(cab, c)})
    return out


def secao_c5(R: Relatorio) -> None:
    D = R.run / "c5"
    R.h("C5 — pressão de memória: tamanho do corpus e índice em RAM vs mmap")
    ing = ler_jsonl(D / "ingestao.jsonl")
    cons = ler_jsonl(D / "consulta.jsonl")
    if not ing and not cons:
        R.p("_Sem dados de C5._")
        return
    for r in ing + cons:
        r["_mib"] = round(num(r.get("tamanho_corpus_bytes")) / MIB)
    if ing:
        agi = agrega(ing, ["_mib"], {
            "tempo_total_s": lambda r: num(r["tempo_total_s"]),
            "tempo_leitura_s": lambda r: num(r["tempo_leitura_s"]),
            "indice_mib": lambda r: num(r["tamanho_indice_bytes"]) / MIB,
            "pico_anon_mib": lambda r: num(r["pico_rss_anon_kb"]) / KIB_POR_MIB,
            "pico_file_mib": lambda r: num(r["pico_rss_file_kb"]) / KIB_POR_MIB,
            "disco_lido_mib": lambda r: num(r.get("io_read_bytes")) / MIB,
            "disco_escrito_mib": lambda r: num(r.get("io_write_bytes")) / MIB,
        })
        R.salvar_ag("c5_ingestao", agi)
        R.h("Ingestão por tamanho de corpus (cache frio)", 3)
        R.p(tabela_ag(agi, [("Corpus (MiB)", "_mib", None), ("Índice (MiB)", "indice_mib", 1),
                            ("Tempo total (s)", "tempo_total_s", 2), ("Leitura (s)", "tempo_leitura_s", 2),
                            ("Pico RssAnon (MiB)", "pico_anon_mib", 1), ("Pico RssFile (MiB)", "pico_file_mib", 1),
                            ("Disco lido (MiB)", "disco_lido_mib", 1), ("Disco escrito (MiB)", "disco_escrito_mib", 1)]))
    if cons:
        for r in cons:
            r["_vet"] = "vetores via mmap" if r.get("vectors_mmap") else "vetores na RAM (np.load)"
        met = {
            "abertura_s": lambda r: num(r["tempo_abertura_s"]),
            "abertura_disco_mib": lambda r: num(r.get("abertura_io_read_bytes")) / MIB,
            "abertura_majflt": lambda r: num(r["abertura_majflt"]),
            "anon_fim_mib": lambda r: num(r["fim_RssAnon_kb"]) / KIB_POR_MIB,
            "file_fim_mib": lambda r: num(r["fim_RssFile_kb"]) / KIB_POR_MIB,
            "vsz_fim_mib": lambda r: num(r["fim_VmSize_kb"]) / KIB_POR_MIB,
            "hwm_mib": lambda r: num(r["fim_VmHWM_kb"]) / KIB_POR_MIB,
            "busca_p95_ms": lambda r: num(r["tempo_busca_s_p95"]) * 1000,
            "busca_mediana_ms": lambda r: num(r["tempo_busca_s_mediana"]) * 1000,
            "total_p95_ms": lambda r: num(r["tempo_s_p95"]) * 1000,
            "total_mediana_ms": lambda r: num(r["tempo_s_mediana"]) * 1000,
            "majflt_consulta": lambda r: num(r["majflt_por_consulta"]),
            "consultas_disco_mib": lambda r: num(r.get("consultas_io_read_bytes")) / MIB,
            "memavail_delta_mib": lambda r: (num(r["depois_MemAvailable_kb"]) - num(r["antes_MemAvailable_kb"])) / KIB_POR_MIB,
            "swap_usado_mib": lambda r: (num(r["depois_SwapTotal_kb"]) - num(r["depois_SwapFree_kb"])) / KIB_POR_MIB,
        }
        agc = agrega(cons, ["_mib", "_vet", "cache"], met)
        R.salvar_ag("c5_consulta", agc)
        vets = ["vetores na RAM (np.load)", "vetores via mmap"]

        def serie(v: str, c: str, m: str) -> tuple[list, list, list]:
            ds = [d for d in agc if d["_vet"] == v and d["cache"] == c]
            return [d["_mib"] for d in ds], [val(d, m)[0] for d in ds], [val(d, m)[1] for d in ds]

        fig, axs = plt.subplots(2, 2, figsize=(12, 8))
        linhas(axs[0, 0], {v: serie(v, "quente", "anon_fim_mib") for v in vets}, "corpus (MiB)", "MiB",
               "RssAnon (memória anônima) após as consultas")
        linhas(axs[0, 1], {v: serie(v, "quente", "file_fim_mib") for v in vets}, "corpus (MiB)", "MiB",
               "RssFile (páginas de arquivo mapeadas)")
        linhas(axs[1, 0], {v: serie(v, "quente", "vsz_fim_mib") for v in vets}, "corpus (MiB)", "MiB",
               "VmSize (memória virtual, VSZ)")
        linhas(axs[1, 1], {v: serie(v, "quente", "memavail_delta_mib") for v in vets}, "corpus (MiB)", "MiB",
               "Variação de MemAvailable do sistema durante a execução")
        axs[1, 1].axhline(0, color="#888", lw=0.8)
        R.salvar_fig(fig, "c5_memoria")

        fig, axs = plt.subplots(1, 3, figsize=(15, 4.5))
        linhas(axs[0], {f"{v}, {c}": serie(v, c, "abertura_s") for v in vets for c in ("frio", "quente")},
               "corpus (MiB)", "segundos", "Abertura do índice (carga de vectors.npy)")
        linhas(axs[1], {v: serie(v, "quente", "busca_p95_ms") for v in vets}, "corpus (MiB)", "ms",
               "Busca vetorial p95 (cache quente)")
        linhas(axs[2], {v: serie(v, "frio", "total_p95_ms") for v in vets}, "corpus (MiB)", "ms",
               "Recuperação p95 a frio (inclui primeiros acessos)")
        R.salvar_fig(fig, "c5_latencia")

        R.h("Consultas por tamanho de corpus", 3)
        R.p(tabela_ag(agc, [("Corpus (MiB)", "_mib", None), ("Vetores", "_vet", None), ("Cache", "cache", None),
                            ("Abertura (s)", "abertura_s", 3), ("Disco lido na abertura (MiB)", "abertura_disco_mib", 1),
                            ("RssAnon (MiB)", "anon_fim_mib", 1), ("RssFile (MiB)", "file_fim_mib", 1),
                            ("VSZ (MiB)", "vsz_fim_mib", 1), ("Pico RSS (MiB)", "hwm_mib", 1),
                            ("Busca mediana (ms)", "busca_mediana_ms", 2), ("Busca p95 (ms)", "busca_p95_ms", 2),
                            ("Recuperação p95 (ms)", "total_p95_ms", 2), ("Faltas maiores/consulta", "majflt_consulta", 2),
                            ("Disco lido nas consultas (MiB)", "consultas_disco_mib", 1),
                            ("Δ MemAvailable (MiB)", "memavail_delta_mib", 1), ("Swap em uso (MiB)", "swap_usado_mib", 1)]))
        sm = sorted(D.glob("smaps_*mb_vm1.txt"))
        if sm:
            R.p(f"\nMapeamento do `vectors.npy` em `/proc/self/smaps` (vetores via mmap, `{sm[-1].name}`):\n")
            R.p("```\n" + "\n".join(sm[-1].read_text().splitlines()[:9]) + "\n```")
    secao_c5_limite(R, D)


ROTULO_SAIDA = {0: "concluiu", 3: "falha controlada (MemoryError/ENOMEM)", 124: "interrompido por timeout",
                137: "morto por SIGKILL (OOM killer do cgroup)"}


def secao_c5_limite(R: Relatorio, D: Path) -> None:
    status = ler_jsonl(D / "limite_status.jsonl")
    if not status:
        return
    R.h("Execução com limite de memória sobre o maior índice", 3)
    res = {(r["rep"], bool(r["vectors_mmap"])): r for r in ler_jsonl(D / "limite.jsonl")}
    linhas_t = []
    for s in status:
        r = res.get((s["rep"], bool(s["vectors_mmap"])), {})
        cod = s["codigo_saida"]
        linhas_t.append([
            str(s["rep"]), "mmap" if s["vectors_mmap"] else "RAM (np.load)", s["mecanismo"],
            f"{s['limite_mb']} MiB", f"{cod} — {ROTULO_SAIDA.get(cod, 'outro')}", str(s["duracao_s"]),
            str(r.get("consultas_ok", 0)), fmt(num(r.get("tempo_abertura_s")), 2),
            fmt(escala(r.get("tempo_s_mediana"), 1000), 1),
            fmt(num(r.get("majflt_por_consulta")), 1),
            fmt(escala(r.get("fim_VmSwap_kb"), 1 / KIB_POR_MIB), 1),
            (r.get("erro") or "")[:80]])
    R.p(f"Índice de {status[0]['tamanho_mb']} MiB de corpus; mecanismo `{status[0]['mecanismo']}` "
        "(`systemd` = cgroup MemoryMax, pode paginar/usar swap; `rlimit` = RLIMIT_AS, só falha por "
        "falta de espaço de endereçamento).\n")
    R.p(tabela_md(["Rep", "Vetores", "Mecanismo", "Limite", "Saída", "Duração (s)", "Consultas ok",
                   "Abertura (s)", "Recuperação mediana (ms)", "Faltas maiores/consulta", "VmSwap (MiB)", "Erro"],
                  linhas_t))
    req = ler_csv(D / "limite_req.csv")
    vm = ler_vmstat(D / "vmstat_limite.txt")
    if req or vm:
        fig, axs = plt.subplots(1, 2, figsize=(13, 4.5))
        for i, v in enumerate(("0", "1")):
            pts = [(int(r["i"]), num(r["tempo_s"]) * 1000) for r in req if r["vectors_mmap"] == v and r["rep"] == "1"]
            if pts:
                axs[0].plot([p[0] for p in pts], [p[1] for p in pts], marker="o", color=CORES[i],
                            label="vetores via mmap" if v == "1" else "vetores na RAM")
        axs[0].set_xlabel("consulta (ordem)")
        axs[0].set_ylabel("ms")
        axs[0].set_title("Latência por consulta sob limite (repetição 1)", fontsize=10)
        axs[0].legend(fontsize=8)
        axs[0].grid(alpha=0.3)
        virgula(axs[0])
        if vm:
            t = list(range(len(vm)))
            outras = [max(0.0, ((r.get("bi") or 0) - (r.get("si") or 0)) / 1024) for r in vm]
            axs[1].plot(t, outras, label="outras leituras de bloco, bi − si (MiB/s)", color=CORES[0], lw=0.8, alpha=0.7)
            axs[1].plot(t, [(r.get("si") or 0) / 1024 for r in vm], label="swap in (MiB/s)", color=CORES[1], lw=1.4)
            axs[1].plot(t, [(r.get("so") or 0) / 1024 for r in vm], label="swap out (MiB/s)", color=CORES[3], lw=1.4)
            axs[1].set_xlabel("segundos desde o início da etapa")
            axs[1].set_title("vmstat durante as execuções com limite", fontsize=10)
            axs[1].legend(fontsize=8)
            axs[1].grid(alpha=0.3)
            virgula(axs[1])
        R.salvar_fig(fig, "c5_limite", nota="série temporal bruta (sem média entre repetições)")
        if vm:
            si = sum((r.get("si") or 0) for r in vm) / 1024 / 1024
            so = sum((r.get("so") or 0) for r in vm) / 1024 / 1024
            pico = max((r.get("si") or 0) for r in vm) / 1024
            R.p(f"\nSoma bruta das amostras do vmstat durante as execuções com limite ({len(vm)} s): "
                f"{fmt(si, 1)} GiB de swap-in e {fmt(so, 1)} GiB de swap-out (maior amostra de swap-in: "
                f"{fmt(pico, 0)} MiB/s). Amostras isoladas muito acima das vizinhas devem ser tratadas com "
                "cautela; os campos por processo (VmSwap e faltas maiores) são a evidência mais direta.")


def secao_c6(R: Relatorio) -> None:
    D = R.run / "c6"
    R.h("C6 — falhas controladas")
    rows = ler_resumos(D / "fila")
    if rows:
        ag = agrega(rows, ["config"], {
            "total": lambda r: num(r["server_consultas_total"]),
            "processadas": lambda r: num(r["server_processadas"]),
            "rejeitadas": lambda r: num(r["server_rejeitadas"]),
            "erros": lambda r: num(r["server_erros"]),
            "vazao": lambda r: num(r["server_vazao_rps"]),
            "bloqueio_prod_s": lambda r: num(r["server_bloqueio_produtores_s"]),
            "fila_max": lambda r: num(r["server_fila_max"]),
        })
        R.salvar_ag("c6_fila", ag)
        r0 = rows[0]
        R.p(f"Fila saturada: {r0.get('workers')} worker, fila de capacidade {r0.get('queue_capacity')}, "
            f"timeout de inserção {r0.get('put_timeout')} s, geração simulada {r0.get('gen_ms')} ms.\n")
        R.p(tabela_ag(ag, [("Configuração", "config", None), ("Consultas", "total", 0),
                           ("Processadas", "processadas", 1), ("Rejeitadas (fila cheia)", "rejeitadas", 1),
                           ("Erros", "erros", 0), ("Vazão (req/s)", "vazao", 2),
                           ("Bloqueio dos produtores (s)", "bloqueio_prod_s", 3), ("Fila máx.", "fila_max", 0)]))
    fal = ler_jsonl(D / "falhas.jsonl")
    if fal:
        por: dict[str, list[dict]] = defaultdict(list)
        for f in fal:
            por[f["caso"]].append(f)
        R.p("\nFalhas de armazenamento (`python -m genresearch query` sobre stores preparados):\n")
        R.p(tabela_md(["Caso", "Repetições", "Códigos de saída", "Mensagem (última linha do stderr)"],
                      [[c, str(len(v)), ", ".join(sorted({str(x["codigo_saida"]) for x in v})),
                        f"`{v[0]['mensagem'][:140]}`"] for c, v in por.items()]))
        R.p("\nA falha por limite de memória está na seção C5 (execução com limite).")


def secao_d(R: Relatorio) -> None:
    D = R.run / "parte_d"
    R.h("Parte D — sincronização: falha demonstrada sem proteção e corrigida")
    cache = ler_jsonl(D / "cache.jsonl")
    if cache:
        for r in cache:
            r["_jan"] = "janela 50 µs" if r.get("janela") == "50" else "sem janela artificial"
        ag = agrega(cache, ["modo", "_jan"], {
            "perdidas": lambda r: num(obter(r, "anomalias.contagens_perdidas")),
            "estouro": lambda r: num(obter(r, "anomalias.estouro_de_capacidade")),
            "calc_diff": lambda r: num(obter(r, "anomalias.calculos_reportados_menos_reais")),
            "duplicadas": lambda r: num(obter(r, "verdade.computacoes_concorrentes_duplicadas")),
            "reais": lambda r: num(obter(r, "verdade.computacoes_reais")),
            "falhas_conc": lambda r: num(obter(r, "reportado.falhas_concorrencia")),
            "tam_max": lambda r: num(obter(r, "reportado.tamanho_max_observado")),
            "coalescidas": lambda r: num(obter(r, "reportado.coalescidas")),
            "espera_lock_s": lambda r: num(obter(r, "reportado.espera_lock_s")),
            "vazao": lambda r: num(r.get("vazao_ops_s")),
        })
        R.salvar_ag("d_cache", ag)
        modos = [m for m in ("unsafe", "safe", "single-flight") if any(d["modo"] == m for d in ag)]
        jans = ["janela 50 µs", "sem janela artificial"]
        fig, axs = plt.subplots(1, 3, figsize=(15, 4.5))
        barras(axs[0], modos, {j: [val(achar(ag, modo=m, _jan=j), "perdidas") for m in modos] for j in jans},
               "contagens", "Atualizações perdidas nos contadores")
        barras(axs[1], modos, {j: [val(achar(ag, modo=m, _jan=j), "duplicadas") for m in modos] for j in jans},
               "cálculos", "Cálculos concorrentes duplicados da mesma chave")
        barras(axs[2], modos, {j: [val(achar(ag, modo=m, _jan=j), "vazao") for m in modos] for j in jans},
               "operações/s", "Vazão (custo da sincronização)")
        R.salvar_fig(fig, "d_cache")
        c0 = cache[0]
        R.p(f"Cache: {c0.get('threads')} threads × {c0.get('ops_por_thread')} operações, {c0.get('chaves')} chaves, "
            f"capacidade {c0.get('capacidade')}, cálculo de {c0.get('custo_calculo_ms')} ms. "
            "A \"verdade\" é contada fora do cache pelo harness (`tools.sync_demo`).\n")
        R.p(tabela_ag(ag, [("Modo", "modo", None), ("Janela", "_jan", None),
                           ("Atualizações perdidas", "perdidas", 1), ("Estouro de capacidade", "estouro", 1),
                           ("Tamanho máx. observado", "tam_max", 1), ("Cálculos reportados − reais", "calc_diff", 1),
                           ("Cálculos duplicados", "duplicadas", 1), ("Coalescidas", "coalescidas", 1),
                           ("Exceções internas", "falhas_conc", 1), ("Espera no lock (s)", "espera_lock_s", 2),
                           ("Vazão (op/s)", "vazao", 0)]))
    log = ler_jsonl(D / "log.jsonl")
    if log:
        for r in log:
            r["_jan"] = "janela 50 µs" if r.get("janela") == "50" else "sem janela artificial"
        ag = agrega(log, ["modo", "_jan"], {
            "esperadas": lambda r: num(r.get("esperadas")),
            "validas": lambda r: num(obter(r, "verificacao.validas")),
            "corrompidas": lambda r: num(obter(r, "verificacao.corrompidas")),
            "perdidos": lambda r: num(obter(r, "verificacao.registros_perdidos")),
            "fora_ordem": lambda r: num(obter(r, "verificacao.fora_de_ordem")),
            "espera_lock_ms": lambda r: num(obter(r, "reportado.espera_lock_s")) * 1000,
            "fila_max": lambda r: num(obter(r, "reportado.fila_max")),
            "vazao": lambda r: num(r.get("vazao_eventos_s")),
        })
        R.salvar_ag("d_log", ag)
        modos = [m for m in ("none", "lock", "queue") if any(d["modo"] == m for d in ag)]
        jans = ["janela 50 µs", "sem janela artificial"]
        fig, axs = plt.subplots(1, 2, figsize=(11, 4.5))
        barras(axs[0], modos, {j: [val(achar(ag, modo=m, _jan=j), "corrompidas") for m in modos] for j in jans},
               "linhas", "Linhas de log corrompidas")
        barras(axs[1], modos, {j: [val(achar(ag, modo=m, _jan=j), "vazao") for m in modos] for j in jans},
               "eventos/s", "Vazão do logger")
        R.salvar_fig(fig, "d_log")
        R.p(tabela_ag(ag, [("Modo", "modo", None), ("Janela", "_jan", None), ("Esperadas", "esperadas", 0),
                           ("Válidas", "validas", 1), ("Corrompidas", "corrompidas", 1), ("Perdidas", "perdidos", 1),
                           ("Fora de ordem", "fora_ordem", 1), ("Espera no lock (ms)", "espera_lock_ms", 2),
                           ("Fila máx.", "fila_max", 0), ("Vazão (eventos/s)", "vazao", 0)]))
        ex = D / "logs" / "log_none_janela50_rep1.jsonl"
        if ex.exists():
            ruins = []
            for linha in ex.read_text(errors="replace").splitlines():
                try:
                    json.loads(linha)
                except json.JSONDecodeError:
                    ruins.append(linha[:110])
                if len(ruins) == 4:
                    break
            if ruins:
                R.p(f"\nExemplo de linhas corrompidas (`{ex.relative_to(R.run)}`):\n")
                R.p("```\n" + "\n".join(ruins) + "\n```")
    if cache or log:
        R.p("\nA janela de 50 µs é artificial (`--window-us`): amplia o intervalo entre leitura e escrita "
            "para tornar a corrida reprodutível e deve ser declarada no relatório. As linhas \"sem janela "
            "artificial\" mostram o comportamento natural no CPython.")


QUESTOES = [
    ("Como tamanho de corpus, índice, lote ou contexto afetou RAM, paginação e latência?",
     "C5 (`c5_memoria`, `c5_latencia`, tabelas de C5) e fator contexto"),
    ("Que diferença foi observada entre leitura convencional e mmap?",
     "C1/C2 (`c12_ingestao`, `c12_consulta`, perfil de RSS, strace) e vetores RAM × mmap no C5"),
    ("O cache reduziu tempo ou operações de recuperação? Qual foi a taxa de acerto?",
     "C4 (`c4_cache`, colunas Acerto, Cálculos e Tempo economizado)"),
    ("A concorrência aumentou vazão? Em que ponto gerou contenção, fila ou degradação?",
     "C3 (`c3_vazao` contra a reta ideal, `c3_latencia_recursos`, espera e fila)"),
    ("Que recurso compartilhado exigiu exclusão mútua ou sincronização de eventos?",
     "Parte D (cache e log), fila do C3 (bloqueio dos produtores, futex no strace)"),
    ("Que condição de corrida, risco de deadlock, starvation ou falha de consistência foi demonstrada?",
     "Parte D (`d_cache`, `d_log`), C4 (cálculos duplicados) e desbalanceamento entre workers no C3"),
    ("Qual mecanismo de sincronização foi adotado e quais trade-offs introduziu?",
     "Parte D: vazão e espera no lock de unsafe × safe × single-flight; none × lock × queue"),
    ("Houve evidência de paginação intensa, swap, faltas de página ou pressão de memória?",
     "C5 (execução com limite, `c5_limite`, vmstat, faltas maiores) e C1/C2 a frio"),
    ("Como os resultados se relacionam a RAG, embeddings, checkpoints e caches?",
     "Abertura do índice frio × quente no C5 (análogo a carregar checkpoint), C4, C1/C2"),
    ("Quais limitações impedem generalizar para modelos maiores, múltiplas GPUs ou ambientes distribuídos?",
     "Ambiente (uma máquina, sem GPU), condições térmicas, geração simulada, embeddings por hashing, "
     "mecanismo de limite usado"),
]


def secao_questoes(R: Relatorio) -> None:
    R.h("Mapa das questões de análise (seção 11) para as evidências")
    R.p(tabela_md(["Questão", "Onde está a evidência nesta execução"], [[q, e] for q, e in QUESTOES]))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        run = Path(args[0])
    else:
        cand = sorted(p for p in Path("resultados").glob("2*") if p.is_dir())
        if not cand:
            print("uso: python -m scripts.graficos <diretório da execução>", file=sys.stderr)
            return 2
        run = cand[-1]
    if not run.is_dir():
        print(f"erro: {run} não é um diretório", file=sys.stderr)
        return 2
    R = Relatorio(run)
    plt.rcParams.update({"font.size": 9})
    falhas = []
    for secao in (secao_cabecalho, secao_termico, secao_c12, secao_c3, secao_c4, secao_contexto, secao_c5,
                  secao_c6, secao_d, secao_questoes):
        try:
            secao(R)
        except Exception as e:  # uma seção com problema não impede as demais
            plt.close("all")
            falhas.append(secao.__name__)
            R.p(f"\n> Seção `{secao.__name__}` não gerada: `{type(e).__name__}: {e}`\n")
            traceback.print_exc(file=sys.stderr)
    (run / "tabelas.md").write_text("\n".join(R.md) + "\n", encoding="utf-8")
    print(f"{len(R.figuras)} figura(s) em {R.dir_fig}, tabelas em {run / 'tabelas.md'}", file=sys.stderr)
    if falhas:
        print(f"seções com erro: {', '.join(falhas)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
