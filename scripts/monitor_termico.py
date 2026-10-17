"""Monitor de temperatura, frequência e uso da CPU durante a bateria.

    python -m scripts.monitor_termico --out T.csv --info T.json [--intervalo 1] [--pai PID]
    python -m scripts.monitor_termico --agora      # imprime a temperatura atual (°C) ou nada

Só stdlib e leitura de /sys e /proc, uma amostra por segundo: custo
desprezível perto do que está sendo medido. Cada amostra vira uma linha do CSV
(gravada na hora). Encerra sozinho quando o processo ``--pai`` deixa de existir.

Fontes, em ordem de preferência:

- temperatura: hwmon (coretemp "Package id 0" na Intel; k10temp "Tdie"/"Tctl"
  na AMD; cpu_thermal em ARM) e, na falta dele, /sys/class/thermal
  (x86_pkg_temp, zonas de CPU, acpitz). Em WSL e na maioria das VMs não há
  sensor exposto: a coluna fica vazia e isso é registrado no ``--info``;
- frequência: cpufreq/scaling_cur_freq de cada CPU; senão "cpu MHz" do
  /proc/cpuinfo (em VMs costuma ser um valor fixo, não a frequência real);
- uso da CPU: diferença dos contadores de /proc/stat entre amostras;
- throttling: contadores thermal_throttle do kernel (só Intel), que contam
  quantas vezes o próprio processador reduziu o clock por temperatura.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
import time
from pathlib import Path

RAIZ = Path("/")
CAMPOS = ["epoch", "temp_c", "freq_media_mhz", "freq_min_mhz", "freq_max_mhz", "uso_cpu_pct",
          "carga_1min", "throttle_core", "throttle_pkg"]


def _ler(p: Path | str) -> str | None:
    try:
        return Path(p).read_text().strip()
    except OSError:
        return None


def _ler_int(p: Path | str) -> int | None:
    v = _ler(p)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def escolher_sensor() -> dict | None:
    """Escolhe o sensor de temperatura da CPU; None se não houver nenhum."""
    hw: dict[str, list[str]] = {}
    for d in sorted(glob.glob(str(RAIZ / "sys/class/hwmon/hwmon*"))):
        nome = _ler(Path(d) / "name")
        entradas = sorted(glob.glob(str(Path(d) / "temp*_input")))
        if nome and entradas and nome not in hw:
            hw[nome] = entradas
    for nome in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal"):
        if nome not in hw:
            continue
        entradas = hw[nome]
        rotulos = {e: _ler(e.replace("_input", "_label")) for e in entradas}
        escolha, rotulo = entradas, "máximo dos sensores"
        for alvo in ("Package id 0", "Tdie", "Tctl"):
            achou = [e for e, r in rotulos.items() if r == alvo]
            if achou:
                escolha, rotulo = achou, alvo
                break
        crits = [c for c in (_ler_int(e.replace("_input", "_crit")) for e in escolha) if c]
        return {"fonte": "hwmon", "sensor": f"{nome} / {rotulo}", "arquivos": escolha,
                "temp_critica_c": max(crits) / 1000 if crits else None}
    zonas = []
    for z in sorted(glob.glob(str(RAIZ / "sys/class/thermal/thermal_zone*"))):
        tipo = _ler(Path(z) / "type")
        if tipo and Path(z, "temp").exists():
            zonas.append((tipo, z))
    for alvo in ("x86_pkg_temp", "cpu", "soc", "tcpu", "acpitz"):
        cand = [(t, z) for t, z in zonas if alvo in t.lower()]
        if not cand:
            continue
        crit = None
        for _, z in cand:
            for tp in glob.glob(str(Path(z) / "trip_point_*_type")):
                if _ler(tp) in ("critical", "hot"):
                    v = _ler_int(tp.replace("_type", "_temp"))
                    if v and v > 0:
                        crit = v / 1000 if crit is None else min(crit, v / 1000)
        return {"fonte": "thermal_zone", "sensor": ", ".join(t for t, _ in cand),
                "arquivos": [str(Path(z) / "temp") for _, z in cand], "temp_critica_c": crit}
    return None


def ler_temp(sensor: dict | None) -> float | None:
    if sensor is None:
        return None
    vals = [v for v in (_ler_int(a) for a in sensor["arquivos"]) if v is not None]
    return max(vals) / 1000 if vals else None


def ler_freqs() -> tuple[list[float], str | None]:
    """Frequências atuais (MHz) de cada CPU e a fonte usada."""
    arqs = glob.glob(str(RAIZ / "sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"))
    vals = [v / 1000 for v in (_ler_int(a) for a in arqs) if v]
    if vals:
        return vals, "scaling_cur_freq"
    txt = _ler(RAIZ / "proc/cpuinfo") or ""
    vals = []
    for linha in txt.splitlines():
        if linha.lower().startswith("cpu mhz"):
            try:
                vals.append(float(linha.split(":")[1]))
            except (IndexError, ValueError):
                pass
    return vals, ("/proc/cpuinfo" if vals else None)


def freq_max_nominal() -> float | None:
    v = _ler_int(RAIZ / "sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    return v / 1000 if v else None


def ler_cpu_stat() -> tuple[int, int] | None:
    """(ocupado, total) em jiffies, somando todas as CPUs."""
    linha = (_ler(RAIZ / "proc/stat") or "").splitlines()[:1]
    if not linha or not linha[0].startswith("cpu "):
        return None
    v = [int(x) for x in linha[0].split()[1:9]]
    user, nice, system, idle, iowait, irq, softirq, steal = (v + [0] * 8)[:8]
    ocupado = user + nice + system + irq + softirq + steal
    return ocupado, ocupado + idle + iowait


def ler_throttle() -> tuple[int | None, int | None]:
    core = [_ler_int(a) for a in glob.glob(str(RAIZ / "sys/devices/system/cpu/cpu[0-9]*/thermal_throttle/core_throttle_count"))]
    pkg = _ler_int(RAIZ / "sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count")
    core = [c for c in core if c is not None]
    return (sum(core) if core else None), pkg


def carga_1min() -> float | None:
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def monitorar(out: Path, info: Path, intervalo: float, pai: int | None) -> None:
    sensor = escolher_sensor()
    freqs, fonte_freq = ler_freqs()
    tc, tp = ler_throttle()
    dados_info = {
        "sensor": sensor["sensor"] if sensor else None,
        "fonte_temperatura": sensor["fonte"] if sensor else None,
        "temp_critica_c": sensor["temp_critica_c"] if sensor else None,
        "fonte_frequencia": fonte_freq,
        "freq_max_nominal_mhz": freq_max_nominal(),
        "throttle_disponivel": tc is not None or tp is not None,
        "cpus": os.cpu_count(),
        "intervalo_s": intervalo,
        "observacao": None if sensor else
        "sem sensor de temperatura exposto (comum em WSL e VMs); acompanhe pelo sistema hospedeiro",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(json.dumps(dados_info, ensure_ascii=False, indent=2))
    novo = not out.exists() or out.stat().st_size == 0
    ant = ler_cpu_stat()
    with open(out, "a", newline="") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(CAMPOS)
        while True:
            time.sleep(intervalo)
            if pai is not None and not Path(f"/proc/{pai}").exists():
                return
            atual = ler_cpu_stat()
            uso = None
            if ant and atual and atual[1] > ant[1]:
                uso = 100 * (atual[0] - ant[0]) / (atual[1] - ant[1])
            ant = atual
            freqs, _ = ler_freqs()
            tc, tp = ler_throttle()
            temp = ler_temp(sensor)
            carga = carga_1min()
            w.writerow([f"{time.time():.3f}",
                        "" if temp is None else f"{temp:.1f}",
                        f"{sum(freqs) / len(freqs):.0f}" if freqs else "",
                        f"{min(freqs):.0f}" if freqs else "",
                        f"{max(freqs):.0f}" if freqs else "",
                        "" if uso is None else f"{uso:.1f}",
                        "" if carga is None else f"{carga:.2f}",
                        "" if tc is None else tc, "" if tp is None else tp])
            f.flush()


def main(argv: list[str] | None = None) -> int:
    global RAIZ
    ap = argparse.ArgumentParser(prog="python -m scripts.monitor_termico")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--info", type=Path)
    ap.add_argument("--intervalo", type=float, default=1.0)
    ap.add_argument("--pai", type=int, default=None, help="encerra quando este PID terminar")
    ap.add_argument("--agora", action="store_true", help="imprime a temperatura atual e sai")
    ap.add_argument("--raiz", type=Path, default=Path("/"), help="raiz alternativa (testes)")
    a = ap.parse_args(argv)
    RAIZ = a.raiz
    if a.agora:
        t = ler_temp(escolher_sensor())
        if t is not None:
            print(f"{t:.0f}")
        return 0
    if a.out is None or a.info is None:
        ap.error("informe --out e --info (ou use --agora)")
    try:
        monitorar(a.out, a.info, a.intervalo, a.pai)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
