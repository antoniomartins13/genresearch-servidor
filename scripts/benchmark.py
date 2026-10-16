"""Instrumentação externa do GenResearch. Run from repository root.

Real mode uses the repository core via genresearch.server and tools.run_c4.
Simulation is opt-in and outputs are segregated from real results.
"""
import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.metrics import MetricsCollector

ROOT = Path(__file__).resolve().parents[1]


def append_csv(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with path.open(encoding='utf-8', newline='') as f:
            if next(csv.reader(f), []) != list(row):
                raise ValueError(f'Cabeçalho incompatível: {path}; escolha outro diretório de saída')
    with path.open('a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if new:
            writer.writeheader()
        writer.writerow(row)


def configurations(selected, workers, requests, max_requests, gen_ms):
    """Only C1-C3 and C6 are runnable with Antônio's server and an available core.
    C4 needs actual cache instrumentation; C5 is a bounded corpus/context variation.
    """
    configs = []
    for name in selected:
        if name == 'C1':
            configs.append(('C1_read', dict(read_mode='read', workers=1)))
        elif name == 'C2':
            configs.append(('C2_mmap', dict(read_mode='mmap', workers=1)))
        elif name == 'C3':
            for w in workers:
                configs.append((f'C3_w{w}', dict(workers=w)))
        elif name == 'C4':
            for workload in ('repetida', 'distinta', 'mista'):
                configs.append((f'C4_{workload}', dict(cache_workload=workload)))
        elif name == 'C5':
            for context in (2048, 8192):
                configs.append((f'C5_context{context}', dict(max_context_bytes=context)))
        elif name == 'C6':
            configs.append(('C6_fila_saturada', dict(workers=1, queue_capacity=1, put_timeout=0.001, gen_ms=max(100, gen_ms), requests=min(requests, max_requests))))
    return configs


def run_one(args, label, rep, changes):
    settings = dict(workers=2, producers=2, queue_capacity=4, requests=args.requests,
                    k=5, max_context_bytes=8192, gen_ms=args.gen_ms,
                    read_mode=None, put_timeout=None)
    settings.update(changes)
    is_cache = 'cache_workload' in settings
    module = 'scripts.cache_runner' if is_cache else 'scripts.server_runner'
    cmd = [sys.executable, '-m', module, '--workers', str(settings['workers']),
           '--producers', str(settings['producers']), '--queue-capacity', str(settings['queue_capacity']),
           '--n-queries', str(settings['requests']), '--k', str(settings['k']),
           '--max-context-bytes', str(settings['max_context_bytes']), '--gen-ms', str(settings['gen_ms']),
           '--config', label, '--rep', str(rep)]
    if args.demo:
        cmd.append('--demo')
    else:
        cmd += ['--store', str(args.store)]
    if settings['read_mode']:
        cmd += ['--read-mode', settings['read_mode']]
    if settings['put_timeout'] is not None:
        cmd += ['--put-timeout', str(settings['put_timeout'])]
    if is_cache:
        cmd += ['--workload', settings['cache_workload'], '--distinct', str(args.cache_distinct),
                '--repeat-frac', str(args.cache_repeat_frac), '--seed', str(args.seed),
                '--cache-capacity', str(args.cache_capacity), '--cache-policy', args.cache_policy,
                '--cache-mode', args.cache_mode]
        if args.event_logs:
            outlog = args.output / ('demo' if args.demo else 'real') / 'logs'
            outlog.mkdir(parents=True, exist_ok=True)
            cmd += ['--event-log', str((outlog / f'{label}_rep{rep}.jsonl').resolve()), '--log-mode', args.log_mode]
    else:
        if args.queries_file:
            cmd += ['--queries-file', str(args.queries_file)]
        if not args.demo:
            raw_dir = args.output / 'real' / 'requisicoes'
            raw_dir.mkdir(parents=True, exist_ok=True)
            cmd += ['--requests-csv', str((raw_dir / f'{label}_rep{rep}.csv').resolve())]

    proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    collector = MetricsCollector(pid=proc.pid, sample_interval=args.interval)
    collector.start()
    # stdout line arrives when server finished but process remains alive for final counters.
    try:
        line = proc.stdout.readline()
        metrics = collector.stop()
        if not line:
            err = proc.stderr.read()
            raise RuntimeError(f'Servidor encerrou sem JSON: {err[-3000:]}')
        server = json.loads(line)
        proc.stdin.write('\n')
        proc.stdin.flush()
        _, err = proc.communicate(timeout=10)
        if proc.returncode not in (0, 1):
            raise RuntimeError(f'Servidor retornou {proc.returncode}: {err[-3000:]}')
        if proc.returncode == 1 and server.get('erros', 0) == 0:
            raise RuntimeError(f'Execução terminou com erro não explicado: {err[-3000:]}')
        # Código 1 é aceito somente se houver erros de consulta registrados.
    except Exception:
        if collector.start_time is not None:
            try: collector.stop()
            except Exception: pass
        if proc.poll() is None:
            proc.kill()
        proc.communicate()
        raise
    stamp = datetime.now(timezone.utc).isoformat()
    base = {'config': label, 'rep': rep, 'timestamp': stamp, 'mode': 'demo' if args.demo else 'real',
            'python': platform.python_version(), 'os': platform.platform(),
            'workers': settings['workers'], 'producers': settings['producers'],
            'queue_capacity': settings['queue_capacity'], 'requests': settings['requests'],
            'read_mode': settings['read_mode'], 'context_bytes': settings['max_context_bytes'],
            'gen_ms': settings['gen_ms'], 'put_timeout': settings['put_timeout'],
            'store': str(args.store) if not args.demo else None,
            'cache_workload': settings.get('cache_workload'), 'cache_capacity': args.cache_capacity if is_cache else None,
            'cache_policy': args.cache_policy if is_cache else None, 'cache_mode': args.cache_mode if is_cache else None,
            'seed': args.seed if is_cache else None}
    # Fixed schema across server and cache runs: no CSV header mismatch.
    server_fields = ('workers', 'producers', 'queue_capacity', 'consultas_total', 'processadas',
                     'erros', 'rejeitadas', 'tempo_total_s', 'vazao_rps', 'espera_s', 'proc_s',
                     'latencia_s', 'bloqueio_produtores_s', 'fila_max', 'fila_media',
                     'utilizacao_workers', 'utilizacao_media', 'consultas_por_worker',
                     'workload', 'event_log')
    cache_fields = ('politica', 'protegido', 'single_flight', 'capacidade', 'tamanho',
                    'tamanho_max_observado', 'acertos', 'erros', 'taxa_acerto', 'taxa_erro',
                    'substituicoes', 'calculos', 'coalescidas', 'tempo_economizado_s',
                    'falhas_concorrencia', 'espera_lock_s')
    def cell(value):
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
    row = {**base, **{f'process_{k}': v for k, v in metrics.items()},
           **{f'server_{k}': cell(server.get(k)) for k in server_fields},
           **{f'cache_{k}': cell(server.get('cache', {}).get(k)) for k in cache_fields}}
    # Stable header for every configuration; always preserve same server keys.
    out = args.output / ('demo' if args.demo else 'real')
    append_csv(out / 'resumo.csv', row)
    for i, sample in enumerate(collector.samples):
        append_csv(out / 'amostras.csv', {**base, 'sample': i, **sample})
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description='Benchmarks instrumentados do GenResearch')
    parser.add_argument('--configs', nargs='+', choices=['C1', 'C2', 'C3', 'C4', 'C5', 'C6'], default=['C1', 'C2', 'C3', 'C6'])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--requests', type=int, default=40)
    parser.add_argument('--workers', type=int, nargs='+', default=[1, 2, 4])
    parser.add_argument('--gen-ms', type=float, default=20)
    parser.add_argument('--interval', type=float, default=0.02)
    parser.add_argument('--store', type=Path, default=Path('data/store'))
    parser.add_argument('--queries-file', type=Path)
    parser.add_argument('--output', type=Path, default=Path('results/instrumentation'))
    parser.add_argument('--cache-capacity', type=int, default=16)
    parser.add_argument('--cache-policy', choices=['lru', 'fifo'], default='lru')
    parser.add_argument('--cache-mode', choices=['safe', 'unsafe', 'single-flight'], default='safe')
    parser.add_argument('--cache-distinct', type=int, default=8)
    parser.add_argument('--cache-repeat-frac', type=float, default=0.7)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--event-logs', action='store_true')
    parser.add_argument('--log-mode', choices=['none', 'lock', 'queue'], default='lock')
    parser.add_argument('--demo', action='store_true', help='Testa a integração com carga simulada; não é evidência experimental')
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.requests < 1 or args.interval <= 0 or any(w < 1 for w in args.workers) or args.cache_capacity < 0 or args.cache_distinct < 1 or not 0 <= args.cache_repeat_frac <= 1:
        parser.error('repeats, requests, workers e interval devem ser positivos')
    if not args.demo:
        try:
            import genresearch.retrieve  # noqa
            import genresearch.store  # noqa
        except ImportError as exc:
            parser.error(f'Núcleo real indisponível: {exc}. Não é permitido substituí-lo por simulação silenciosa.')
        if not args.store.is_dir():
            parser.error(f'Store não encontrado: {args.store}')
        from genresearch.store import Store
        try:
            check_store = Store.open(args.store)
            check_store.close()
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(f'Store/corpus inválido: {exc}')
    try:
        configs = configurations(args.configs, args.workers, args.requests, 40, args.gen_ms)
    except RuntimeError as exc:
        parser.error(str(exc))
    if 'C4' in args.configs and args.cache_capacity < 1:
        print('AVISO: C4 sem cache (linha de base).', file=sys.stderr)
    if args.demo and 'C4' in args.configs:
        print('AVISO: C4 em modo demo usa sleep, não recuperação real.', file=sys.stderr)
    for label, settings in configs:
        for rep in range(1, args.repeats + 1):
            result = run_one(args, label, rep, settings)
            print(f"{label} rep={rep}: {result['process_elapsed_seconds']}s, pid={result['process_pid']}")
    print('Resultados:', args.output / ('demo' if args.demo else 'real'))


if __name__ == '__main__':
    main()
