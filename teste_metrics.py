import time

from src.metrics import MetricsCollector, save_metrics_csv

collector = MetricsCollector()

print("Iniciando medição...")
collector.start()

# Simulação de uma operação do GenResearch
dados = [i**2 for i in range(500_000)]
time.sleep(1)

resultado = collector.stop()

print("\nResultado das métricas:")

for chave, valor in resultado.items():
    print(f"{chave}: {valor}")

save_metrics_csv(resultado)

print("\nMétricas salvas em results/raw/metrics.csv")
