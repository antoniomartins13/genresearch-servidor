import os
import csv
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
import psutil


def read_page_faults(pid=None):
    """Linux /proc/PID/stat: minflt (10), majflt (12). None if unavailable."""
    if not Path('/proc').is_dir():
        return None, None
    pid = os.getpid() if pid is None else int(pid)
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        return int(fields[7]), int(fields[9])
    except (OSError, ValueError, IndexError):
        return None, None


class MetricsCollector:
    """Samples a PID while it is running; start/stop provide deltas and samples.

    Stop before the monitored subprocess exits to preserve final counters.
    """
    def __init__(self, sample_interval=0.02, pid=None):
        if sample_interval <= 0:
            raise ValueError('sample_interval deve ser positivo')
        self.process = psutil.Process(os.getpid() if pid is None else pid)
        self.sample_interval = sample_interval
        self.start_time = None
        self.start_io = None
        self.start_cpu = None
        self.start_faults = None
        self.peak_rss = 0
        self.samples = []
        self._stop_event = threading.Event()
        self._sampler = None

    def _snapshot(self):
        try:
            mem = self.process.memory_info()
            vm = psutil.virtual_memory()
            swap = psutil.swap_memory()
            return {
                'elapsed_seconds': round(time.perf_counter() - self.start_time, 6),
                'rss_mb': round(mem.rss / 1048576, 3),
                'vms_mb': round(mem.vms / 1048576, 3),
                'threads': self.process.num_threads(),
                'system_available_mb': round(vm.available / 1048576, 3),
                'swap_used_mb': round(swap.used / 1048576, 3),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

    def _sample_memory(self):
        while not self._stop_event.is_set():
            sample = self._snapshot()
            if sample is None:
                break
            self.samples.append(sample)
            self.peak_rss = max(self.peak_rss, sample['rss_mb'] * 1048576)
            self._stop_event.wait(self.sample_interval)

    def start(self):
        if self.start_time is not None:
            raise RuntimeError('Uma medição já está em andamento.')
        self.start_cpu = self.process.cpu_times()
        self.start_faults = read_page_faults(self.process.pid)
        try:
            self.start_io = self.process.io_counters()
        except (psutil.Error, AttributeError, NotImplementedError):
            self.start_io = None
        self.samples = []
        self.peak_rss = self.process.memory_info().rss
        self._stop_event.clear()
        self.start_time = time.perf_counter()
        self._sampler = threading.Thread(target=self._sample_memory, daemon=True)
        self._sampler.start()

    def stop(self):
        if self.start_time is None:
            raise RuntimeError('Execute start() antes de stop().')
        elapsed = time.perf_counter() - self.start_time
        self._stop_event.set()
        self._sampler.join()
        try:
            memory = self.process.memory_info()
            self.peak_rss = max(self.peak_rss, memory.rss)
            cpu = self.process.cpu_times()
            cpu_time = max(0, cpu.user + cpu.system - self.start_cpu.user - self.start_cpu.system)
            end_faults = read_page_faults(self.process.pid)
            minor = end_faults[0] - self.start_faults[0] if self.start_faults[0] is not None and end_faults[0] is not None else None
            major = end_faults[1] - self.start_faults[1] if self.start_faults[1] is not None and end_faults[1] is not None else None
            result = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'pid': self.process.pid,
                'elapsed_seconds': round(elapsed, 6),
                'rss_mb': round(memory.rss / 1048576, 3),
                'peak_rss_mb': round(self.peak_rss / 1048576, 3),
                'vms_mb': round(memory.vms / 1048576, 3),
                'cpu_time_seconds': round(cpu_time, 6),
                'cpu_percent': round(cpu_time / elapsed * 100, 2) if elapsed else 0,
                'threads': self.process.num_threads(),
                'system_available_mb': round(psutil.virtual_memory().available / 1048576, 3),
                'swap_used_mb': round(psutil.swap_memory().used / 1048576, 3),
                'minor_page_faults': minor, 'major_page_faults': major,
                'read_bytes': None, 'write_bytes': None,
            }
            try:
                end_io = self.process.io_counters()
                if self.start_io is not None:
                    result['read_bytes'] = end_io.read_bytes - self.start_io.read_bytes
                    result['write_bytes'] = end_io.write_bytes - self.start_io.write_bytes
            except (psutil.Error, AttributeError, NotImplementedError):
                pass
            return result
        finally:
            self.start_time = None


def save_metrics_csv(metrics, filename='results/raw/metrics.csv'):
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        with path.open(newline='', encoding='utf-8') as f:
            if next(csv.reader(f), []) != list(metrics):
                raise ValueError('O CSV existente tem colunas diferentes. Use outro nome de arquivo.')
    new = not path.exists() or path.stat().st_size == 0
    with path.open('a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(metrics))
        if new:
            writer.writeheader()
        writer.writerow(metrics)
