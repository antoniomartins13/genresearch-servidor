import time

from src.metrics import MetricsCollector, save_metrics_csv
from src.component_metrics import ComponentMetrics

collector = MetricsCollector()
components = ComponentMetrics()

collector.start()

# Simula uma fila recebendo requisições
for i in range(100):
    components.record_enqueue(i + 1)

# Simula o processamento das requisições
dados = [i * i for i in range(500_000)]

for i in range(100):
    components.record_dequeue()

    if i % 4 == 0:
        components.record_cache_miss()
    else:
        components.record_cache_hit()

time.sleep(0.5)

metrics = collector.stop()
metrics.update(components.snapshot())

for key, value in metrics.items():
    print(f"{key}: {value}")

save_metrics_csv(metrics, "results/raw/componentes_teste.csv")

print("\nTeste concluído. CSV gerado.")
