import csv
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.metrics import MetricsCollector, read_page_faults, save_metrics_csv
from src.component_metrics import ComponentMetrics
from scripts.benchmark import append_csv


class TestInstrumentation(unittest.TestCase):
    def test_self_pid_and_samples(self):
        m = MetricsCollector(sample_interval=0.005)
        m.start()
        time.sleep(0.04)
        result = m.stop()
        self.assertEqual(result['pid'], os.getpid())
        self.assertGreater(len(m.samples), 1)
        self.assertGreater(result['peak_rss_mb'], 0)

    def test_external_pid(self):
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.3)'])
        try:
            m = MetricsCollector(pid=p.pid, sample_interval=0.01)
            m.start()
            time.sleep(0.06)
            result = m.stop()
            self.assertEqual(result['pid'], p.pid)
            self.assertGreater(len(m.samples), 0)
        finally:
            p.terminate()
            p.wait()

    def test_page_faults(self):
        self.assertEqual(len(read_page_faults(os.getpid())), 2)
        self.assertEqual(read_page_faults(999999999), (None, None))

    def test_component_thread_safety(self):
        m = ComponentMetrics()
        def work():
            for _ in range(1000):
                m.record_enqueue(2)
                m.record_dequeue(0.01)
                m.record_result(ok=True)
                m.record_cache_hit(0.001)
        ts = [threading.Thread(target=work) for _ in range(4)]
        for t in ts: t.start()
        for t in ts: t.join()
        s = m.snapshot()
        self.assertEqual(s['queue_enqueued'], 4000)
        self.assertEqual(s['processed'], 4000)
        self.assertEqual(s['cache_hits'], 4000)

    def test_csv_append_and_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'test.csv'
            append_csv(path, {'a': 1, 'b': 2})
            append_csv(path, {'a': 3, 'b': 4})
            with path.open() as f:
                self.assertEqual(len(list(csv.DictReader(f))), 2)
            with self.assertRaises(ValueError):
                append_csv(path, {'other': 1})
            save_metrics_csv({'x': 1}, Path(d) / 'metrics.csv')


if __name__ == '__main__':
    unittest.main()
