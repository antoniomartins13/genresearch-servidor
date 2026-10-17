# Bateria de experimentos (C1–C6, Partes A, D e E)

Tudo é executado por um único script. Cada etapa grava dados brutos, e no final
`scripts/graficos.py` gera os gráficos, as tabelas agregadas e o `tabelas.md`.

## Pré-requisitos (uma vez, na máquina oficial)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt -r requirements-analise.txt
sudo apt install strace sysstat      # opcional: evidência de syscalls e iostat
```

Rode em Linux nativo ou numa VM. No WSL funciona, mas o C5 com limite de
memória normalmente cai no mecanismo alternativo (ver abaixo).

## Como rodar

```bash
# 1) piloto (~2 min): confere se tudo funciona nesta máquina; saída em data/ (fora do git)
scripts/run-experimentos.sh --rapido

# 2) bateria oficial (30–60 min): saída em resultados/<data-hora>/
scripts/run-experimentos.sh

# 3) versionar os resultados
git add resultados/<data-hora> && git commit -m "resultados: bateria <data-hora>"
```

Recomendações para a bateria oficial:

- commitar os scripts antes, para o commit registrado no `meta.json` reproduzir a execução;
- notebook na tomada, sem outros programas pesados abertos;
- não usar a máquina durante a execução.

Se uma etapa falhar, as demais continuam, e o resumo final mostra o comando
para refazer só a etapa que falhou na mesma pasta:

```bash
RUN=resultados/<data-hora> scripts/run-experimentos.sh --so "C5"
```

Para gerar só os gráficos de novo: `venv/bin/python -m scripts.graficos resultados/<data-hora>`.

## O que cada etapa faz

| Etapa | Item do enunciado | O que roda | Saída principal |
|---|---|---|---|
| AMBIENTE | Parte A | `scripts/00-ambiente.sh` (uname, lscpu, free, meminfo, lsblk, df, swapon, vmstat, iostat, versões, commit) | `ambiente/` |
| C12 | C1, C2, Parte E | Ingestão read × mmap, a frio e a quente (`exp_ingest`); perfil RssAnon/RssFile de um documento grande (`tools.probe_rss`); consultas com dados por consulta (`exp_consulta`); conferência com `tools.bench_query`; `strace -c` | `c12/` |
| C3 | C3 | `scripts.benchmark` com 1, 2, 4, 8 e 16 workers e 2 produtores, com geração simulada de 20 ms e de 0 ms; captura das threads do servidor em `/proc/PID/task`; `strace -c` do servidor (futex) | `c3/` |
| C4 | C4 | Benchmark C4 (cargas repetida, mista e distinta) sem cache, com lock, com single-flight e com capacidade 4 (LRU e FIFO) | `c4/` |
| CONTEXTO | fator experimental | Benchmark com contexto de 2048 e de 8192 bytes | `contexto/` |
| C5 | C5, Parte E | Corpora de tamanhos crescentes; ingestão; consultas com vetores na RAM × via mmap, a frio e a quente; `vmstat`; execução com limite de memória sobre o maior índice | `c5/` |
| C6 | C6 | Fila saturada (benchmark C6); store inexistente, corpus ausente, índice indisponível e corpus alterado | `c6/` |
| D | Parte D | `tools.sync_demo` para o cache (unsafe/safe/single-flight) e o log (none/lock/queue), com e sem janela artificial | `parte_d/` |
| GRAFICOS | resultados | `scripts/graficos.py` (inclui as condições térmicas) | `graficos/`, `agregados/`, `tabelas.md` |

Cada configuração é repetida `REPS` vezes (padrão 3).

Fatores variados:

- número de workers;
- tamanho do corpus;
- modo de leitura (read × mmap);
- vetores na RAM × mmap;
- contexto;
- capacidade e política do cache;
- tempo de geração.

## Temperatura e pausas automáticas

Durante toda a bateria, `scripts/monitor_termico.py` registra a cada segundo:

- a temperatura da CPU;
- a frequência média e a do núcleo mais lento;
- o uso total da CPU;
- os contadores de throttling do kernel (só existem em Intel).

O custo é desprezível, porque o monitor só lê `/sys` e `/proc`. As marcas de
início e fim de cada etapa também são gravadas, e o `graficos.py` usa tudo isso
para gerar `graficos/termico.png` e uma tabela por etapa, junto com um
parecer: se houve redução de clock por temperatura e em quais etapas.

Antes de cada etapa, se a CPU estiver a `TEMP_PAUSA` °C ou mais (padrão 85),
o script espera ela esfriar até `TEMP_RETOMA` (padrão 70). A espera dura no
máximo `PAUSA_MAX_S` segundos (padrão 600). O tempo de pausa não entra na
duração das etapas e aparece em laranja no gráfico.

Em WSL e na maioria das VMs não há sensor de temperatura exposto. Nesse caso
ficam registrados só a frequência e o uso da CPU, e a pausa automática não
funciona. Acompanhe a temperatura pelo Windows (por exemplo, HWiNFO) e, se
precisar, rode a bateria em partes com `--so`.

Antes de rodar, feche o que esquenta a máquina, principalmente o serviço do
Ollama: `sudo systemctl stop ollama`. Confira com `ollama ps` e `free -h`. Além
de aquecer a CPU, ele mantém o modelo na RAM e distorce o C5.

## Parâmetros

Os padrões estão no topo de `run-experimentos.sh` e podem ser alterados por
variável de ambiente, por exemplo `REPS=5 scripts/run-experimentos.sh`.

O C5 escolhe os tamanhos sozinho (`C5_TAMANHOS=auto`): parte de 200, 1000 e
2000 MiB e descarta os que fariam o índice passar de 40% da RAM ou que não
caberiam no disco livre.

O limite de memória padrão é 150 MiB + metade do maior índice. Fica abaixo do
tamanho do índice, então há pressão de memória real, mas só dentro do processo
limitado. Cada execução limitada tem timeout (`C5_TIMEOUT`, padrão 300 s).

## O que declarar no relatório

- **Mecanismo de limite do C5**, registrado em `c5/limite_status.jsonl`:
  - `systemd`: cgroup v2 com `MemoryMax`. O processo pode paginar, usar swap ou ser morto pelo OOM killer (código 137).
  - `rlimit`: alternativa quando o cgroup não está disponível. Limita o espaço de endereçamento (RLIMIT_AS), e o resultado é uma falha controlada por falta de memória (código 3), não paginação.
- **Janela de corrida da Parte D:** a `janela 50 µs` é artificial (`--window-us`). As linhas "sem janela artificial" mostram o comportamento natural do CPython.
- **Cache frio:** obtido por `posix_fadvise(DONTNEED)` arquivo por arquivo (ver `tools/README.md`), sem `drop_caches`. O restante do sistema continua quente.
- **Geração simulada:** a geração é simulada com `time.sleep` (`gen-ms`), e os embeddings vêm de feature hashing. Os números medem o sistema operacional, não um LLM.
- **Escopo das medições de C1/C2 no benchmark:** `scripts.benchmark` mede o processo inteiro, incluindo a inicialização do Python. As comparações finas de read × mmap vêm de `exp_ingest` e `exp_consulta`, que medem dentro do processo.

## Estrutura de uma execução

```
resultados/<data-hora>/
  meta.json            comando, commit, parâmetros, início e fim
  status.txt           etapas ok/falha e duração
  termico/             temperatura/frequência/uso por segundo e marcas das etapas
  execucao.log         log completo
  ambiente/            Parte A
  c12/ c3/ c4/ contexto/ c5/ c6/ parte_d/    dados brutos (JSONL/CSV/txt)
  graficos/*.png       figuras
  agregados/*.csv      média, desvio-padrão e n por configuração
  tabelas.md           tabelas prontas + mapa das questões da seção 11
```

Corpora e índices ficam em `data/exp/` (gitignored) e são reaproveitados entre
execuções. Com os padrões, ocupam cerca de 8 GB. Para liberar o espaço depois:
`rm -rf data/exp`.
