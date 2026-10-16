import threading


class ComponentMetrics:
    """Thread-safe counters. Feed with real events; never infer cache hits from repeated queries."""
    def __init__(self):
        self._lock = threading.Lock()
        self.queue_enqueued = 0
        self.queue_dequeued = 0
        self.queue_max_size = 0
        self.queue_samples = []
        self.queue_wait_total_s = 0.0
        self.queue_wait_count = 0
        self.processed = 0
        self.rejected = 0
        self.errors = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.cache_evictions = 0
        self.cache_saved_seconds = 0.0

    def record_enqueue(self, queue_size):
        with self._lock:
            self.queue_enqueued += 1
            self.queue_max_size = max(self.queue_max_size, queue_size)
            self.queue_samples.append(queue_size)

    def record_dequeue(self, wait_seconds=None):
        with self._lock:
            self.queue_dequeued += 1
            if wait_seconds is not None:
                self.queue_wait_total_s += wait_seconds
                self.queue_wait_count += 1

    def record_result(self, *, ok=False, rejected=False):
        with self._lock:
            if rejected:
                self.rejected += 1
            elif ok:
                self.processed += 1
            else:
                self.errors += 1

    def record_cache_hit(self, saved_seconds=None):
        with self._lock:
            self.cache_hits += 1
            if saved_seconds is not None:
                self.cache_saved_seconds += saved_seconds

    def record_cache_miss(self):
        with self._lock:
            self.cache_misses += 1

    def record_cache_eviction(self):
        with self._lock:
            self.cache_evictions += 1

    def snapshot(self):
        with self._lock:
            total = self.cache_hits + self.cache_misses
            return {
                'queue_enqueued': self.queue_enqueued,
                'queue_dequeued': self.queue_dequeued,
                'queue_max_size': self.queue_max_size,
                'queue_mean_size': sum(self.queue_samples) / len(self.queue_samples) if self.queue_samples else 0,
                'queue_mean_wait_s': self.queue_wait_total_s / self.queue_wait_count if self.queue_wait_count else None,
                'processed': self.processed, 'rejected': self.rejected, 'errors': self.errors,
                'cache_hits': self.cache_hits, 'cache_misses': self.cache_misses,
                'cache_evictions': self.cache_evictions,
                'cache_hit_rate': self.cache_hits / total if total else None,
                'cache_saved_seconds': self.cache_saved_seconds if self.cache_hits else None,
            }
