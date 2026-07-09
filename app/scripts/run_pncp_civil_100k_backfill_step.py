#!/usr/bin/env python3
"""Executa um passo retomavel do backfill PNCP Civil 100k.

Roda uma janela curta por vez para respeitar rate-limit do PNCP. Se o passo
termina sem erros, avanca o cursor no state file. Se houver erro, repete a
mesma janela na proxima execucao; o captador e idempotente por id_externo.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


STATE_DEFAULT = Path("/app/logs/pncp_civil_100k_backfill_state.json")
START_DEFAULT = date(2025, 1, 1)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_state(path: Path, start: date) -> dict:
    if not path.exists():
        return {"next_start": start.isoformat(), "completed": False, "runs": 0}
    try:
        data = json.loads(path.read_text())
        if "next_start" not in data:
            data["next_start"] = start.isoformat()
        data.setdefault("completed", False)
        data.setdefault("runs", 0)
        return data
    except Exception:
        return {"next_start": start.isoformat(), "completed": False, "runs": 0}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    tmp.replace(path)


def parse_stats(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith("STATS_JSON:"):
            return json.loads(line.split("STATS_JSON:", 1)[1].strip())
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", default=str(STATE_DEFAULT))
    parser.add_argument("--start", type=parse_date, default=START_DEFAULT)
    parser.add_argument("--until", type=parse_date, default=date.today())
    parser.add_argument("--step-days", type=int, default=7)
    parser.add_argument("--max-pages", type=int, default=30)
    parser.add_argument("--valor-min", type=float, default=100_000)
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args()

    state_path = Path(args.state_file)
    state = load_state(state_path, args.start)
    if state.get("completed"):
        print(f"Backfill ja completo: {state_path}")
        return 0

    next_start = parse_date(state["next_start"])
    if next_start > args.until:
        state["completed"] = True
        state["completed_at"] = datetime.utcnow().isoformat() + "Z"
        save_state(state_path, state)
        print("Backfill completo.")
        return 0

    step_end = min(args.until, next_start + timedelta(days=max(1, args.step_days) - 1))
    cmd = [
        sys.executable,
        "-u",
        "/app/scripts/captar_pncp_civil_100k.py",
        "--backfill",
        "--since", next_start.isoformat(),
        "--until", step_end.isoformat(),
        "--chunk-days", str(max(1, args.step_days)),
        "--max-pages", str(args.max_pages),
        "--valor-min", str(args.valor_min),
    ]
    if args.dry:
        cmd.append("--dry")

    print(f"Backfill PNCP civil: {next_start} -> {step_end} | cmd={' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, file=sys.stderr, end="")

    stats = parse_stats(proc.stdout or "")
    state["runs"] = int(state.get("runs") or 0) + 1
    state["last_run_at"] = datetime.utcnow().isoformat() + "Z"
    state["last_window"] = {"since": next_start.isoformat(), "until": step_end.isoformat()}
    state["last_returncode"] = proc.returncode
    state["last_stats"] = stats

    if proc.returncode == 0 and int(stats.get("erros") or 0) == 0:
        state["next_start"] = (step_end + timedelta(days=1)).isoformat()
        state["last_success_at"] = state["last_run_at"]
        if step_end >= args.until:
            state["completed"] = True
            state["completed_at"] = state["last_run_at"]
    else:
        state["last_error"] = "captador retornou erro ou STATS_JSON.erros > 0"

    save_state(state_path, state)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
