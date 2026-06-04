#!/usr/bin/env python3
"""SCAFFOLD — Captador BNDES Saúde (R$5,2bi visibilidade pendente).

Status: STUB inicial em 04/06. Para produção, precisa:
1. Definir KEYWORDS_SAUDE em captar_bndes.py
2. Definir EXCLUDE_SAUDE (false positives: laboratório análise clínica, etc)
3. Adicionar flag --saude no captar_bndes.py (similar ao --saneamento)
4. Adicionar entry no orchestrator (run_orchestrator.sh)
5. Adicionar cron entry

Padrão de invocação (após completar):
    python /app/scripts/captar_bndes.py --saude

Wrapper segue convenção dos outros captadores `/app/scripts/{nome}.py`.
"""
import os
import subprocess
import sys


def main() -> int:
    cmd = ["python", "/app/scripts/captar_bndes.py", "--saude"]
    cmd.extend(arg for arg in sys.argv[1:] if arg != "--saude")
    proc = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr, check=False)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
