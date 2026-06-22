"""Garante que os módulos em app/ (planos, services, ...) sejam importáveis
nos testes rodando `pytest` a partir de /root/wins_hub/app, espelhando o
PYTHONPATH do container (workdir /app)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
