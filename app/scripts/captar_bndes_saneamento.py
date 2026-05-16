#!/usr/bin/env python3
"""Wrapper que invoca captar_bndes.py --saneamento.

Existe pra ser chamado pelo orchestrator/admin via convenção
`/app/scripts/{nome}.py` (sem precisar mudar a infra de subprocess pra aceitar
argumentos explícitos no RUN_CAPTADORES_MANUAL).

Repassa o STATS_JSON do filho como última linha pro orchestrator parsear.
"""
import os
import subprocess
import sys


def main() -> int:
    cmd = ["python", "/app/scripts/captar_bndes.py", "--saneamento"]
    # Repassa argumentos extras (ex: --dry) se o caller passar
    cmd.extend(arg for arg in sys.argv[1:] if arg != "--saneamento")
    proc = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr, check=False)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
