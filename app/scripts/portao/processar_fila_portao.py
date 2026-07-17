#!/usr/bin/env python3
"""Processa wins_v2.portao_fila (sem timer embutido — chamar via cron/orchestrator)."""
import json, os, sys
sys.path.insert(0, "/app/services")
sys.path.insert(0, "/app/scripts/portao")
from portao_gate_v5 import processar_fila

if __name__ == "__main__":
    limit = int(os.environ.get("PORTAO_FILA_LIMIT", "50"))
    print(json.dumps(processar_fila(limit=limit), ensure_ascii=False, indent=2))
