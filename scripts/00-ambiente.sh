#!/usr/bin/env bash
# Parte A — inventário do ambiente de execução.
#
#   scripts/00-ambiente.sh [DIR_SAIDA]      (padrão: resultados/ambiente_<data>)
#
# Grava ambiente.txt (saída bruta de cada comando), ambiente.json (resumo para
# tabelas), meminfo.txt, vmstat.txt e iostat.txt. Comando ausente não aborta:
# a seção registra "(indisponível)".
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=${1:-resultados/ambiente_$(date +%Y%m%d-%H%M%S)}
PY=${PY:-$([[ -x venv/bin/python ]] && echo venv/bin/python || echo python3)}
mkdir -p "$OUT"
TXT="$OUT/ambiente.txt"
: > "$TXT"

secao() {  # secao TÍTULO comando [args...]
  local titulo=$1
  shift
  {
    echo "### $titulo"
    echo "\$ $*"
    "$@" 2>&1 || echo "(indisponível: código $?)"
    echo
  } >> "$TXT"
}

secao "Data da coleta" date --iso-8601=seconds
secao "Commit do repositório" git rev-parse HEAD
secao "Alterações locais não commitadas (fora de resultados/)" git status --short -- . ':(exclude)resultados'
secao "Kernel" uname -a
secao "Distribuição" cat /etc/os-release
secao "Virtualização" sh -c 'command -v systemd-detect-virt >/dev/null && systemd-detect-virt || echo "systemd-detect-virt ausente"'
secao "WSL" sh -c 'grep -qi microsoft /proc/version && echo "sim (WSL)" || echo "não"'
secao "Contêiner" sh -c '[ -f /.dockerenv ] && echo "docker" || echo "não detectado"'
secao "CPU" lscpu
secao "Memória" free -h
secao "Swap" sh -c 'swapon --show; echo; echo "swappiness: $(cat /proc/sys/vm/swappiness)"'
secao "cgroup v2" sh -c 'stat -fc %T /sys/fs/cgroup'
secao "Discos" lsblk -o NAME,SIZE,TYPE,ROTA,FSTYPE,MODEL,MOUNTPOINT
secao "Sistema de arquivos do repositório" df -hT .
secao "Ponto de montagem do repositório" findmnt -T .
secao "Uso de disco" df -h
secao "GPU" sh -c 'if command -v nvidia-smi >/dev/null; then nvidia-smi; else lspci 2>/dev/null | grep -iE "vga|3d|display" || echo "nenhuma GPU detectada (ou lspci ausente)"; fi'
secao "Python" "$PY" -VV
secao "Pacotes Python" "$PY" -c 'import importlib.metadata as m; [print(d.metadata["Name"], d.version) for d in sorted(m.distributions(), key=lambda d: d.metadata["Name"].lower())]'
secao "Limites do shell" sh -c 'ulimit -a'
secao "Ferramentas" sh -c 'for c in strace vmstat iostat systemd-run timeout fincore; do printf "%-12s %s\n" "$c" "$(command -v $c || echo ausente)"; done'

cat /proc/meminfo > "$OUT/meminfo.txt"
if command -v vmstat >/dev/null; then vmstat 1 5 > "$OUT/vmstat.txt" 2>&1 || true; fi
if command -v iostat >/dev/null; then
  iostat -xz 1 3 > "$OUT/iostat.txt" 2>&1 || true
else
  echo "iostat indisponível (instale o pacote sysstat)" > "$OUT/iostat.txt"
fi

"$PY" - "$OUT/ambiente.json" <<'EOF'
import json, os, platform, subprocess, sys
from datetime import datetime

def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""

def versao(mod):
    try:
        return __import__(mod).__version__
    except Exception:
        return None

mem = {}
with open("/proc/meminfo") as f:
    for line in f:
        k, _, v = line.partition(":")
        mem[k] = int(v.split()[0])
os_release = {}
try:
    with open("/etc/os-release") as f:
        for line in f:
            if "=" in line:
                k, v = line.rstrip().split("=", 1)
                os_release[k] = v.strip('"')
except OSError:
    pass
cpu = sh("lscpu | grep -m1 'Model name' | cut -d: -f2").strip()
wsl = "microsoft" in open("/proc/version").read().lower()
dados = {
    "data": datetime.now().astimezone().isoformat(timespec="seconds"),
    "commit": sh("git rev-parse HEAD"),
    "alteracoes_locais": bool(sh("git status --porcelain -- . ':(exclude)resultados'")),
    "kernel": platform.release(),
    "distribuicao": os_release.get("PRETTY_NAME"),
    "virtualizacao": sh("systemd-detect-virt 2>/dev/null") or ("wsl" if wsl else None),
    "wsl": wsl,
    "cpu_modelo": cpu,
    "cpus_logicas": os.cpu_count(),
    "mem_total_mib": round(mem.get("MemTotal", 0) / 1024),
    "swap_total_mib": round(mem.get("SwapTotal", 0) / 1024),
    "swappiness": sh("cat /proc/sys/vm/swappiness"),
    "sistema_arquivos": sh("df -T . | awk 'NR==2 {print $2}'"),
    "disco_rotacional": sh("lsblk -dno ROTA $(df . | awk 'NR==2 {print $1}') 2>/dev/null"),
    "python": platform.python_version(),
    "numpy": versao("numpy"),
    "psutil": versao("psutil"),
    "matplotlib": versao("matplotlib"),
    "gpu": sh("nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null") or None,
}
with open(sys.argv[1], "w") as f:
    json.dump(dados, f, ensure_ascii=False, indent=2)
EOF

echo "ambiente registrado em $OUT" >&2
