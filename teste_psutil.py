import os
import psutil

processo = psutil.Process(os.getpid())

print("PID:", processo.pid)
print("Memória RSS:", processo.memory_info().rss / (1024 ** 2), "MB")
print("Memória VMS:", processo.memory_info().vms / (1024 ** 2), "MB")
print("CPU do sistema:", psutil.cpu_percent(interval=1), "%")
print("Memória disponível:", psutil.virtual_memory().available / (1024 ** 2), "MB")