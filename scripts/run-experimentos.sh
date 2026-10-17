#!/usr/bin/env bash
# Bateria completa de experimentos do GenResearch-Servidor (C1–C6 + Parte D) e gráficos.
#
#   scripts/run-experimentos.sh                # bateria oficial → resultados/<data-hora>/
#   scripts/run-experimentos.sh --rapido       # piloto pequeno → data/resultados_piloto/ (fora do git)
#   scripts/run-experimentos.sh --so "C3 C4"   # só algumas etapas (o preparo sempre roda)
#
# Etapas: AMBIENTE C12 C3 C4 CONTEXTO C5 C6 D GRAFICOS
#
# Parâmetros por variável de ambiente (padrão da bateria oficial entre parênteses):
#   REPS (3)  REQ (200)  WORKERS ("1 2 4 8 16")  CORPUS_MB (200)  DOCS (100)
#   NQ (50)  C5_TAMANHOS ("auto" = 200 1000 2000 filtrados por RAM e disco)
#   C5_NQ (30)  C5_NQ_LIMITE (10)  C5_LIMITE_MB (auto)  C5_TIMEOUT (300)  PY
#   TEMP_PAUSA (85) TEMP_RETOMA (70) PAUSA_MAX_S (600): antes de cada etapa, se a CPU estiver
#   a TEMP_PAUSA °C ou mais, espera esfriar até TEMP_RETOMA (no máximo PAUSA_MAX_S segundos).
#   Temperatura, frequência e uso da CPU são registrados a cada segundo em termico/.
#
# Uma etapa que falha é registrada em status.txt e a bateria segue para a próxima.
# Corpora e índices ficam em data/exp/ (gitignored) e são reaproveitados entre execuções.
set -euo pipefail
shopt -s inherit_errexit  # falha dentro de $(...) também interrompe
cd "$(dirname "$0")/.."

RAPIDO=0
SO=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --rapido) RAPIDO=1 ;;
    --so) SO=$2; shift ;;
    -h | --help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "opção desconhecida: $1 (use --help)" >&2; exit 2 ;;
  esac
  shift
done

PY=${PY:-$([[ -x venv/bin/python ]] && echo venv/bin/python || echo python3)}
TS=$(date +%Y%m%d-%H%M%S)
SEED=42
EXP=data/exp
if ((RAPIDO)); then
  REPS=${REPS:-2}; REQ=${REQ:-40}; WORKERS=${WORKERS:-"1 2 4"}
  CORPUS_MB=${CORPUS_MB:-20}; DOCS=${DOCS:-10}; NQ=${NQ:-15}
  C5_TAMANHOS=${C5_TAMANHOS:-"20 60"}; C5_NQ=${C5_NQ:-10}; C5_NQ_LIMITE=${C5_NQ_LIMITE:-5}
  RUN=${RUN:-data/resultados_piloto/$TS}
else
  REPS=${REPS:-3}; REQ=${REQ:-200}; WORKERS=${WORKERS:-"1 2 4 8 16"}
  CORPUS_MB=${CORPUS_MB:-200}; DOCS=${DOCS:-100}; NQ=${NQ:-50}
  C5_TAMANHOS=${C5_TAMANHOS:-auto}; C5_NQ=${C5_NQ:-30}; C5_NQ_LIMITE=${C5_NQ_LIMITE:-10}
  RUN=${RUN:-resultados/$TS}
fi
C5_TIMEOUT=${C5_TIMEOUT:-300}
C5_LIMITE_MB=${C5_LIMITE_MB:-auto}
TEMP_PAUSA=${TEMP_PAUSA:-85}
TEMP_RETOMA=${TEMP_RETOMA:-70}
PAUSA_MAX_S=${PAUSA_MAX_S:-600}

mkdir -p "$RUN" "$EXP"
exec > >(tee -a "$RUN/execucao.log") 2>&1

msg() { echo "[$(date +%H:%M:%S)] $*" >&2; }

# Acrescenta campos fixos (JSON) a cada linha JSON lida da entrada.
anota() {
  "$PY" -c '
import json, sys
extra = json.loads(sys.argv[1])
for linha in sys.stdin:
    linha = linha.strip()
    if linha:
        d = json.loads(linha)
        d.update(extra)
        print(json.dumps(d, ensure_ascii=False), flush=True)
' "$1"
}

tem() { command -v "$1" > /dev/null 2>&1; }

# ---------------------------------------------------------------- verificações
"$PY" -c 'import numpy, psutil' || { msg "faltam dependências: $PY -m pip install -r requirements.txt"; exit 1; }
NUMPY_VER=$("$PY" -c 'import numpy; print(numpy.__version__)')
NUMPY_REQ=$(sed -n 's/^numpy==//p' requirements.txt)
[[ "$NUMPY_VER" == "$NUMPY_REQ" ]] || msg "AVISO: numpy $NUMPY_VER ≠ $NUMPY_REQ fixado; o corpus pode não ser idêntico byte a byte"
TEM_MPL=1
"$PY" -c 'import matplotlib' 2> /dev/null || {
  TEM_MPL=0
  msg "AVISO: matplotlib ausente; os gráficos não serão gerados. Instale com: $PY -m pip install -r requirements-analise.txt"
}
if [[ -n "$(git status --porcelain 2> /dev/null)" ]]; then
  msg "AVISO: há alterações não commitadas; o commit registrado não reproduz exatamente esta execução"
  git status --short > "$RUN/git_alteracoes_locais.txt" || true
fi
TEM_STRACE=0
if tem strace && strace -o /dev/null true 2> /dev/null; then TEM_STRACE=1; else msg "AVISO: strace indisponível; evidências de syscalls serão puladas (sudo apt install strace)"; fi

MEM_TOTAL_MB=$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)

# ---------------------------------------------------------------- tamanhos do C5
idx_mb() { echo $(($1 * 107 / 100)); }  # vectors.npy ≈ 1,07 × corpus (chunk 2048, overlap 128)
if [[ "$C5_TAMANHOS" == auto ]]; then
  livre_mb=$(df -Pm "$EXP" | awk 'NR==2 {print $4}')
  sel=""
  uso=0
  for s in 200 1000 2000; do
    need=$(($(idx_mb "$s") + s))
    if (($(idx_mb "$s") * 100 > MEM_TOTAL_MB * 40)); then
      msg "C5: tamanho $s MiB descartado (índice passaria de 40% da RAM de $MEM_TOTAL_MB MiB)"
    elif ((uso + need + 2048 > livre_mb)); then
      msg "C5: tamanho $s MiB descartado (disco livre insuficiente: $livre_mb MiB)"
    else
      sel="$sel $s"
      uso=$((uso + need))
    fi
  done
  C5_TAMANHOS=${sel# }
fi
C5_MAIOR=$(echo "$C5_TAMANHOS" | tr ' ' '\n' | sort -n | tail -1)
if [[ "$C5_LIMITE_MB" == auto && -n "$C5_MAIOR" ]]; then
  C5_LIMITE_MB=$((150 + $(idx_mb "$C5_MAIOR") / 2))  # base do interpretador + metade do índice
fi

# ---------------------------------------------------------------- metadados
CMD="scripts/run-experimentos.sh"
((RAPIDO)) && CMD+=" --rapido"
# shellcheck disable=SC2089,SC2090  # CMD é só texto descritivo para o meta.json
[[ -n "$SO" ]] && CMD+=" --so \"$SO\""
# shellcheck disable=SC2090
export RAPIDO REPS REQ WORKERS CORPUS_MB DOCS NQ SEED C5_TAMANHOS C5_NQ C5_NQ_LIMITE \
  C5_LIMITE_MB C5_TIMEOUT NUMPY_VER MEM_TOTAL_MB CMD
# Reexecução numa pasta existente: mantém o meta.json original, grava um meta de
# reexecução e recusa parâmetros diferentes (misturaria configurações).
META="$RUN/meta.json"
[[ -f "$META" ]] && META="$RUN/meta_reexecucao_$TS.json"
"$PY" - "$META" "$RUN/meta.json" << 'EOF'
import json, os, platform, subprocess, sys
from datetime import datetime
e = os.environ
def sh(c):
    return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
if sys.argv[1] != sys.argv[2]:
    orig = json.load(open(sys.argv[2]))
    atual = {"rapido": e["RAPIDO"] == "1", "reps": int(e["REPS"]), "req": int(e["REQ"]),
             "workers": e["WORKERS"], "corpus_mb": int(e["CORPUS_MB"]), "docs": int(e["DOCS"]),
             "nq": int(e["NQ"])}
    dif = {k: (orig.get(k), v) for k, v in atual.items() if orig.get(k) != v}
    if dif:
        sys.exit(f"parâmetros diferentes da execução original em {sys.argv[2]}: {dif}")
json.dump({
    "inicio": datetime.now().astimezone().isoformat(timespec="seconds"),
    "comando": e["CMD"],
    "commit": sh("git rev-parse HEAD"), "branch": sh("git rev-parse --abbrev-ref HEAD"),
    "alteracoes_locais": bool(sh("git status --porcelain")),
    "rapido": e["RAPIDO"] == "1", "reps": int(e["REPS"]), "req": int(e["REQ"]),
    "workers": e["WORKERS"], "corpus_mb": int(e["CORPUS_MB"]), "docs": int(e["DOCS"]),
    "nq": int(e["NQ"]), "seed_corpus": int(e["SEED"]), "c5_tamanhos_mb": e["C5_TAMANHOS"],
    "c5_nq": int(e["C5_NQ"]), "c5_nq_limite": int(e["C5_NQ_LIMITE"]),
    "c5_limite_mb": e["C5_LIMITE_MB"], "c5_timeout_s": int(e["C5_TIMEOUT"]),
    "python": platform.python_version(), "numpy": e["NUMPY_VER"],
    "kernel": platform.release(), "mem_total_mib": int(e["MEM_TOTAL_MB"]),
}, open(sys.argv[1], "w"), ensure_ascii=False, indent=2)
EOF

msg "execução: $RUN"
msg "parâmetros: REPS=$REPS REQ=$REQ WORKERS=\"$WORKERS\" CORPUS=${CORPUS_MB}MiB/${DOCS}docs C5=\"$C5_TAMANHOS\" limite=${C5_LIMITE_MB}MiB"
((RAPIDO)) || msg "duração estimada da bateria oficial: 30–60 min (mais se o disco for lento ou houver swap no C5)"

# ---------------------------------------------------------------- monitor térmico
# Uma amostra por segundo de temperatura, frequência e uso da CPU durante toda a
# bateria, com as fronteiras de cada etapa, para mostrar se houve redução de clock
# por temperatura (o que distorceria os tempos entre repetições).
mkdir -p "$RUN/termico"
TERM_ETAPAS="$RUN/termico/etapas_$TS.tsv"
TERMICO_RAIZ=${TERMICO_RAIZ:-/}  # só para testes com /sys simulado
"$PY" -m scripts.monitor_termico --out "$RUN/termico/termico_$TS.csv" \
  --info "$RUN/termico/info_$TS.json" --pai $$ --raiz "$TERMICO_RAIZ" &
MON_PID=$!
trap 'kill "$MON_PID" 2> /dev/null || true' EXIT

marca() { printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$(date +%s.%N)" "${3:-}" >> "$TERM_ETAPAS"; }
temp_atual() { "$PY" -m scripts.monitor_termico --agora --raiz "$TERMICO_RAIZ" 2> /dev/null || true; }

T0=$(temp_atual)
if [[ -n "$T0" ]]; then
  msg "monitor térmico ativo; CPU a ${T0} °C agora (pausa automática a partir de ${TEMP_PAUSA} °C)"
else
  msg "AVISO: nenhum sensor de temperatura exposto (comum em WSL/VM); só frequência e uso da CPU serão registrados. Acompanhe a temperatura pelo sistema hospedeiro."
fi

esfriar() {  # espera a CPU esfriar antes de começar uma etapa
  local t espera=0
  t=$(temp_atual)
  [[ -n "$t" ]] || return 0
  ((t >= TEMP_PAUSA)) || return 0
  msg "CPU a ${t} °C (≥ ${TEMP_PAUSA} °C): aguardando esfriar até ${TEMP_RETOMA} °C antes da próxima etapa"
  marca pausa esfriar
  while ((t > TEMP_RETOMA && espera < PAUSA_MAX_S)); do
    sleep 10
    espera=$((espera + 10))
    t=$(temp_atual)
    [[ -n "$t" ]] || break
  done
  marca fim_pausa esfriar
  if ((espera >= PAUSA_MAX_S)); then
    msg "AVISO: a CPU não esfriou até ${TEMP_RETOMA} °C em ${PAUSA_MAX_S} s (está a ${t} °C); seguindo mesmo assim"
  else
    msg "retomando com a CPU a ${t} °C após ${espera} s de pausa"
  fi
}

# ---------------------------------------------------------------- corpora e índices
garantir_corpus() {  # garantir_corpus MIB DOCS → imprime o diretório
  local dir="$EXP/corpus_${1}mb_${2}d"
  if [[ ! -f "$dir/.gerado" ]]; then
    msg "gerando corpus ${1} MiB / ${2} documentos em $dir"
    "$PY" -m genresearch gen-corpus --out "$dir" --docs "$2" --size-mb "$1" --seed "$SEED" --force \
      >> "$RUN/corpora.jsonl" || return 1
    sync
    touch "$dir/.gerado"
  fi
  echo "$dir"
}

store_ok() {
  "$PY" -c 'import sys; from pathlib import Path; from genresearch.store import Store
Store.open(Path(sys.argv[1]), vectors_mmap=True).close()' "$1" 2> /dev/null
}

garantir_store() {  # garantir_store CORPUS STORE
  if ! store_ok "$2"; then
    msg "ingerindo $1 → $2"
    "$PY" -m genresearch ingest --corpus "$1" --store "$2" --read-mode read >> "$RUN/corpora.jsonl"
  fi
}

marca inicio PREPARO
BASE_CORPUS=$(garantir_corpus "$CORPUS_MB" "$DOCS")
BASE_STORE="$EXP/store_${CORPUS_MB}mb_${DOCS}d_read"
garantir_store "$BASE_CORPUS" "$BASE_STORE"
marca fim PREPARO 0

# ---------------------------------------------------------------- execução das etapas
FALHAS=()
etapa() {  # etapa NOME função
  local nome=$1 rc t0=$SECONDS
  shift
  if [[ -n "$SO" && " $SO " != *" $nome "* && $nome != GRAFICOS ]]; then return 0; fi
  [[ $nome == GRAFICOS ]] || esfriar
  t0=$SECONDS
  msg "===== $nome ====="
  [[ $nome == GRAFICOS ]] || marca inicio "$nome"
  set +e
  (
    set -euo pipefail
    trap 'kill $(jobs -p) 2> /dev/null || true' EXIT
    "$@"
  )
  rc=$?
  set -e
  [[ $nome == GRAFICOS ]] || marca fim "$nome" "$rc"
  if ((rc == 0)); then
    echo "$nome ok $((SECONDS - t0))s" >> "$RUN/status.txt"
    msg "$nome concluída em $((SECONDS - t0)) s"
  else
    echo "$nome FALHOU (código $rc) $((SECONDS - t0))s" >> "$RUN/status.txt"
    FALHAS+=("$nome")
    msg "ATENÇÃO: $nome falhou (código $rc); seguindo para a próxima etapa"
  fi
}

# Parte A
e_ambiente() { rm -rf "$RUN/ambiente" && PY="$PY" scripts/00-ambiente.sh "$RUN/ambiente"; }

# C1/C2 — leitura convencional vs mmap: ingestão, perfil de memória, consulta, syscalls.
e_c12() {
  local D="$RUN/c12" c2 modos m c
  rm -rf "$D" && mkdir -p "$D"
  c2=$(garantir_corpus "$CORPUS_MB" 2)  # 2 documentos grandes para o perfil de RSS
  for rep in $(seq 1 "$REPS"); do
    if ((rep % 2)); then modos="read mmap"; else modos="mmap read"; fi  # alterna a ordem
    for m in $modos; do
      msg "C1/C2 ingestão rep $rep modo $m (frio e quente)"
      for c in frio quente; do
        "$PY" -m scripts.exp_ingest --corpus "$BASE_CORPUS" --store "$EXP/c12_store_$m" \
          --read-mode "$m" --cache "$c" --rep "$rep" --rotulo C12 >> "$D/ingestao.jsonl"
      done
      for c in frio quente; do
        "$PY" -m tools.probe_rss --doc "$c2/doc_00000.txt" --read-mode "$m" --cache "$c" |
          anota "{\"rep\": $rep, \"rotulo\": \"C12\"}" >> "$D/probe_rss.jsonl"
      done
      for c in quente frio-consulta; do
        "$PY" -m scripts.exp_consulta --store "$BASE_STORE" --read-mode "$m" --cache "$c" \
          --n-queries "$NQ" --rep "$rep" --rotulo C12 --csv "$D/consulta_req.csv" >> "$D/consulta.jsonl"
      done
    done
  done
  msg "C1/C2 conferência com tools.bench_query"
  "$PY" -m tools.bench_query --store "$BASE_STORE" --n-queries "$NQ" --reps "$REPS" > "$D/bench_query.jsonl"
  if ((TEM_STRACE)); then
    for m in read mmap; do
      msg "C1/C2 strace -c da ingestão ($m) — tempos desta execução não são usados"
      strace -f -c -o "$D/strace_ingestao_$m.txt" "$PY" -m genresearch ingest --corpus "$BASE_CORPUS" \
        --store "$EXP/strace_store_$m" --read-mode "$m" > /dev/null 2>&1 || msg "strace ($m) falhou"
    done
  fi
}

# C3 — concorrência e fila: 1..16 workers, geração simulada de 20 ms e de 0 ms.
e_c3() {
  local D="$RUN/c3" pid
  rm -rf "$D" && mkdir -p "$D/threads"
  # shellcheck disable=SC2086
  "$PY" -m scripts.benchmark --configs C3 --workers $WORKERS --repeats "$REPS" --requests "$REQ" \
    --gen-ms 20 --store "$BASE_STORE" --output "$D/gen20"
  # shellcheck disable=SC2086
  "$PY" -m scripts.benchmark --configs C3 --workers $WORKERS --repeats "$REPS" --requests "$REQ" \
    --gen-ms 0 --store "$BASE_STORE" --output "$D/gen0"

  msg "C3 evidência de threads (servidor com 4 workers e 2 produtores)"
  "$PY" -m genresearch.server --store "$BASE_STORE" --workers 4 --producers 2 --queue-capacity 4 \
    --n-queries 400 --gen-ms 20 > "$D/threads/server.json" 2> "$D/threads/server.log" &
  pid=$!
  sleep 1
  if kill -0 "$pid" 2> /dev/null; then
    ps -L -o pid,tid,nlwp,stat,pcpu,rss,vsz,comm -p "$pid" > "$D/threads/ps_threads.txt" || true
    cat "/proc/$pid/status" > "$D/threads/proc_status.txt" || true
    for t in /proc/"$pid"/task/*; do
      echo "$(basename "$t") $(cat "$t/comm") $(awk '/^State/ {print $2, $3}' "$t/status") $(cat "$t/wchan" 2> /dev/null)"
    done > "$D/threads/tarefas.txt" 2> /dev/null || true
  else
    msg "AVISO: o servidor terminou antes da captura de threads"
  fi
  wait "$pid" || true

  if ((TEM_STRACE)); then
    msg "C3 strace -c do servidor (futex = sincronização entre threads)"
    strace -f -c -o "$D/strace_servidor_w4.txt" "$PY" -m genresearch.server --store "$BASE_STORE" \
      --workers 4 --producers 2 --queue-capacity 4 --n-queries "$REQ" --gen-ms 20 > /dev/null 2>&1 ||
      msg "strace do servidor falhou"
  fi
}

# C4 — cache compartilhado: modos, capacidade e política, contra linha de base sem cache.
e_c4() {
  local D="$RUN/c4"
  rm -rf "$D" && mkdir -p "$D"
  "$PY" -m scripts.benchmark --configs C4 --repeats "$REPS" --requests "$REQ" --store "$BASE_STORE" \
    --cache-capacity 0 --output "$D/sem_cache"
  "$PY" -m scripts.benchmark --configs C4 --repeats "$REPS" --requests "$REQ" --store "$BASE_STORE" \
    --cache-mode safe --event-logs --output "$D/safe"
  "$PY" -m scripts.benchmark --configs C4 --repeats "$REPS" --requests "$REQ" --store "$BASE_STORE" \
    --cache-mode single-flight --output "$D/single_flight"
  "$PY" -m scripts.benchmark --configs C4 --repeats "$REPS" --requests "$REQ" --store "$BASE_STORE" \
    --cache-capacity 4 --cache-policy lru --output "$D/cap4_lru"
  "$PY" -m scripts.benchmark --configs C4 --repeats "$REPS" --requests "$REQ" --store "$BASE_STORE" \
    --cache-capacity 4 --cache-policy fifo --output "$D/cap4_fifo"
}

# Fator contexto (configuração C5_context do benchmark: 2048 vs 8192 bytes).
e_contexto() {
  rm -rf "$RUN/contexto"
  "$PY" -m scripts.benchmark --configs C5 --repeats "$REPS" --requests "$REQ" --store "$BASE_STORE" \
    --output "$RUN/contexto"
}

mecanismo_limite() {
  local out
  if tem systemd-run && out=$(systemd-run --user --scope --quiet -p MemoryMax=64M \
    sh -c 'cat /sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/memory.max' 2> /dev/null) &&
    [[ "$out" == 67108864 ]]; then
    echo systemd
  else
    echo rlimit
  fi
}

# C5 — pressão de memória: tamanho do corpus × vetores em RAM ou mmap, e execução com limite.
e_c5() {
  local D="$RUN/c5" s corpus st vm c rep vmpid mec rc t0 args
  rm -rf "$D" && mkdir -p "$D"
  [[ -n "$C5_TAMANHOS" ]] || { msg "C5: nenhum tamanho cabe nesta máquina"; return 1; }
  for s in $C5_TAMANHOS; do
    corpus=$(garantir_corpus "$s" "$DOCS")
    st="$EXP/c5_store_${s}mb"
    cat /proc/meminfo > "$D/meminfo_${s}mb_antes.txt"
    if tem vmstat; then vmstat -n -t 1 > "$D/vmstat_${s}mb.txt" & vmpid=$!; else vmpid=""; fi
    for rep in $(seq 1 "$REPS"); do
      msg "C5 ingestão ${s} MiB rep $rep"
      "$PY" -m scripts.exp_ingest --corpus "$corpus" --store "$st" --read-mode read --cache frio \
        --rep "$rep" --rotulo "C5_${s}mb" >> "$D/ingestao.jsonl"
    done
    for rep in $(seq 1 "$REPS"); do
      for vm in 0 1; do
        for c in frio quente; do
          msg "C5 consultas ${s} MiB rep $rep vectors_mmap=$vm cache=$c"
          args=(-m scripts.exp_consulta --store "$st" --cache "$c" --n-queries "$C5_NQ" --rep "$rep"
            --rotulo "C5_${s}mb" --csv "$D/consulta_req.csv")
          ((vm)) && args+=(--vectors-mmap)
          ((rep == 1)) && [[ $c == quente ]] && args+=(--salvar-maps "$D/smaps_${s}mb_vm${vm}.txt")
          "$PY" "${args[@]}" >> "$D/consulta.jsonl"
        done
      done
    done
    [[ -n "$vmpid" ]] && kill "$vmpid" 2> /dev/null || true
    cat /proc/meminfo > "$D/meminfo_${s}mb_depois.txt"
  done

  # Execução com limite de memória sobre o maior índice.
  st="$EXP/c5_store_${C5_MAIOR}mb"
  mec=$(mecanismo_limite)
  msg "C5 limite de memória: ${C5_LIMITE_MB} MiB via $mec sobre o índice de ${C5_MAIOR} MiB (timeout ${C5_TIMEOUT}s)"
  [[ $mec == rlimit ]] && msg "AVISO: cgroup indisponível; RLIMIT_AS produz falha controlada por falta de memória, não paginação"
  cat /proc/meminfo > "$D/meminfo_limite_antes.txt"
  if tem vmstat; then vmstat -n -t 1 > "$D/vmstat_limite.txt" & vmpid=$!; else vmpid=""; fi
  for rep in $(seq 1 "$REPS"); do
    for vm in 0 1; do
      args=(-m scripts.exp_consulta --store "$st" --cache frio --n-queries "$C5_NQ_LIMITE" --rep "$rep"
        --rotulo "C5_limite" --csv "$D/limite_req.csv")
      ((vm)) && args+=(--vectors-mmap)
      t0=$SECONDS
      set +e
      if [[ $mec == systemd ]]; then
        timeout -k 10 "$C5_TIMEOUT" systemd-run --user --scope --quiet -p "MemoryMax=${C5_LIMITE_MB}M" \
          "$PY" "${args[@]}" >> "$D/limite.jsonl"
      else
        timeout -k 10 "$C5_TIMEOUT" "$PY" "${args[@]}" --limite-as-mb "$C5_LIMITE_MB" >> "$D/limite.jsonl"
      fi
      rc=$?
      set -e
      msg "C5 limite rep $rep vectors_mmap=$vm: código de saída $rc em $((SECONDS - t0)) s"
      echo "{\"rep\": $rep, \"vectors_mmap\": $vm, \"mecanismo\": \"$mec\", \"limite_mb\": $C5_LIMITE_MB, \"tamanho_mb\": $C5_MAIOR, \"codigo_saida\": $rc, \"duracao_s\": $((SECONDS - t0))}" >> "$D/limite_status.jsonl"
    done
  done
  [[ -n "$vmpid" ]] && kill "$vmpid" 2> /dev/null || true
  cat /proc/meminfo > "$D/meminfo_limite_depois.txt"
  dmesg 2> /dev/null | grep -iE "oom|killed process" | tail -20 > "$D/dmesg_oom.txt" || true
}

# C6 — falhas controladas: fila saturada (benchmark) e falhas de armazenamento.
falha_caso() {  # falha_caso NOME REP comando...
  local nome=$1 rep=$2 rc
  shift 2
  set +e
  "$@" > /dev/null 2> "$RUN/c6/falhas/${nome}_rep${rep}.txt"
  rc=$?
  set -e
  "$PY" -c 'import json, sys
linhas = [l for l in open(sys.argv[4], encoding="utf-8", errors="replace").read().splitlines() if l.strip()]
print(json.dumps({"caso": sys.argv[1], "rep": int(sys.argv[2]), "codigo_saida": int(sys.argv[3]),
                  "mensagem": linhas[-1] if linhas else ""}, ensure_ascii=False))' \
    "$nome" "$rep" "$rc" "$RUN/c6/falhas/${nome}_rep${rep}.txt" >> "$RUN/c6/falhas.jsonl"
}

e_c6() {
  local D="$RUN/c6" F="$EXP/falhas" rep
  rm -rf "$D" && mkdir -p "$D/falhas"
  "$PY" -m scripts.benchmark --configs C6 --repeats "$REPS" --store "$BASE_STORE" --output "$D/fila"

  msg "C6 preparando cenários de falha de armazenamento em $F"
  rm -rf "$F"
  mkdir -p "$F"
  "$PY" -m genresearch gen-corpus --out "$F/corpus" --docs 2 --size-mb 1 --seed "$SEED" > /dev/null
  "$PY" -m genresearch ingest --corpus "$F/corpus" --store "$F/store" --read-mode read > /dev/null
  # corpus ausente: store íntegro cujo corpus foi apagado
  cp -r "$F/corpus" "$F/corpus_apagado"
  "$PY" -m genresearch ingest --corpus "$F/corpus_apagado" --store "$F/store_corpus_apagado" --read-mode read > /dev/null
  rm -rf "$F/corpus_apagado"
  # índice indisponível: vectors.npy removido
  cp -r "$F/store" "$F/store_sem_vetores"
  rm "$F/store_sem_vetores/vectors.npy"
  # corpus alterado depois da ingestão
  cp -r "$F/corpus" "$F/corpus_alterado"
  "$PY" -m genresearch ingest --corpus "$F/corpus_alterado" --store "$F/store_corpus_alterado" --read-mode read > /dev/null
  for f in "$F"/corpus_alterado/*.txt; do printf 'bytes extras. ' >> "$f"; done

  for rep in $(seq 1 "$REPS"); do
    falha_caso store_inexistente "$rep" "$PY" -m genresearch query --store "$F/nao_existe" --q "teste"
    falha_caso corpus_ausente "$rep" "$PY" -m genresearch query --store "$F/store_corpus_apagado" --q "teste"
    falha_caso indice_indisponivel "$rep" "$PY" -m genresearch query --store "$F/store_sem_vetores" --q "teste"
    falha_caso corpus_alterado "$rep" "$PY" -m genresearch query --store "$F/store_corpus_alterado" --q "teste" --k 20
  done
}

# Parte D — condição de corrida demonstrada (sem proteção) e corrigida (lock / single-flight / fila).
e_d() {
  local D="$RUN/parte_d" rep jan modo extra
  rm -rf "$D" && mkdir -p "$D/logs"
  for rep in $(seq 1 "$REPS"); do
    for jan in 50 nenhuma; do
      if [[ $jan == nenhuma ]]; then extra=(); else extra=(--window-us "$jan"); fi
      for modo in unsafe safe single-flight; do
        "$PY" -m tools.sync_demo cache --mode "$modo" --seed "$rep" "${extra[@]}" |
          anota "{\"rep\": $rep, \"janela\": \"$jan\"}" >> "$D/cache.jsonl"
      done
      for modo in none lock queue; do
        "$PY" -m tools.sync_demo log --mode "$modo" "${extra[@]}" --out "$D/logs/log_${modo}_janela${jan}_rep${rep}.jsonl" |
          anota "{\"rep\": $rep, \"janela\": \"$jan\"}" >> "$D/log.jsonl"
      done
    done
  done
  # mantém só a 1ª repetição dos logs como evidência (os demais já foram verificados e resumidos)
  find "$D/logs" -name '*_rep*.jsonl' ! -name '*_rep1.jsonl' -delete
}

e_graficos() {
  if ((TEM_MPL)); then
    "$PY" -m scripts.graficos "$RUN"
  else
    msg "matplotlib ausente: instale com '$PY -m pip install -r requirements-analise.txt' e rode '$PY -m scripts.graficos $RUN'"
    return 1
  fi
}

etapa AMBIENTE e_ambiente
etapa C12 e_c12
etapa C3 e_c3
etapa C4 e_c4
etapa CONTEXTO e_contexto
etapa C5 e_c5
etapa C6 e_c6
etapa D e_d
kill "$MON_PID" 2> /dev/null || true  # para o monitor antes dos gráficos
wait "$MON_PID" 2> /dev/null || true
"$PY" - "$META" << 'EOF'
import json, sys
from datetime import datetime
m = json.load(open(sys.argv[1]))
m["fim"] = datetime.now().astimezone().isoformat(timespec="seconds")
json.dump(m, open(sys.argv[1], "w"), ensure_ascii=False, indent=2)
EOF
etapa GRAFICOS e_graficos

msg "================================================================"
msg "resultados em $RUN"
TMAX=$(awk -F, 'NR > 1 && $2 != "" && $2 > m {m = $2} END {if (m != "") print m}' "$RUN/termico/termico_$TS.csv" 2> /dev/null || true)
[[ -n "$TMAX" ]] && msg "temperatura máxima registrada: ${TMAX} °C (detalhes em tabelas.md, seção de condições térmicas)"
if ((${#FALHAS[@]})); then
  msg "etapas com falha: ${FALHAS[*]} (detalhes em $RUN/execucao.log); rode de novo só elas com --so \"${FALHAS[*]}\" e RUN=$RUN"
else
  msg "todas as etapas concluídas"
fi
((RAPIDO)) || msg "para versionar: git add $RUN && git commit -m \"resultados: bateria $TS\""
